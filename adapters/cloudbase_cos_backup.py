"""Direct COS access to an explicitly verified existing CloudBase storage bucket.

Uses documented COS object REST APIs and HMAC-SHA1 signatures. No CloudBase
administrator API, bucket creation, bucket listing, ACL edit, or delete method.
The object reference is cos://bucket/key, not a claimed CloudBase SDK fileID.
"""
from dataclasses import dataclass
import hashlib
import hmac
import http.client
import re
import time
from urllib.parse import quote

from adapters.cloudbase_backup import CloudBaseBackupConfig, CloudBaseClassicStorage, _file_id, MAX_JSON
from adapters.safe_https import open_request
from services.hub.errors import HubError


def cos_canonical_request(method, path, headers):
    """The request has no query parameters; all explicit headers are signed."""
    values = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9-]+', key):
            raise HubError('CLOUDBASE_COS_HEADER_INVALID')
        lower = key.lower()
        if lower in values or any(ord(c) < 32 for c in value):
            raise HubError('CLOUDBASE_COS_HEADER_INVALID')
        values[lower] = value
    names = sorted(values)
    canonical = '&'.join(quote(k, safe='-_.~') + '=' + quote(values[k], safe='-_.~') for k in names)
    return method.lower() + '\n' + path + '\n\n' + canonical + '\n', ';'.join(names)


def cos_sign_headers(method, path, headers, credentials, *, now=None, ttl=300):
    """Build the documented COS signature. Credentials never enter a receipt."""
    if method not in ('PUT', 'GET', 'HEAD') or not 30 <= ttl <= 300:
        raise HubError('CLOUDBASE_COS_SIGNING_INPUT_INVALID')
    try:
        secret_id, secret_key = credentials['SecretId'], credentials['SecretKey']
        if any(not isinstance(v, str) or not v or len(v) > 2048 or any(ord(c) < 33 for c in v)
               for v in (secret_id, secret_key)):
            raise ValueError()
        if not re.fullmatch(r'[A-Za-z0-9]{3,256}', secret_id): raise ValueError()
        values = dict(headers); token = credentials.get('Token')
        if token is not None:
            if not isinstance(token, str) or not token or len(token) > 16384 or any(ord(c) < 33 for c in token):
                raise ValueError()
            values['x-cos-security-token'] = token
        current = int(time.time() if now is None else now)
        end = current + ttl
        expiry = credentials.get('ExpirationEpoch')
        if expiry is not None:
            if type(expiry) not in (int, float) or expiry < current + 30:
                raise HubError('CLOUDBASE_BACKUP_AUTH_EXPIRED', review=True)
            end = min(end, int(expiry))
        key_time = str(current - 10) + ';' + str(end)
        canonical, signed_headers = cos_canonical_request(method, path, values)
        signing_key = hmac.new(secret_key.encode(), key_time.encode(), hashlib.sha1).hexdigest()
        to_sign = 'sha1\n' + key_time + '\n' + hashlib.sha1(canonical.encode()).hexdigest() + '\n'
        signature = hmac.new(signing_key.encode(), to_sign.encode(), hashlib.sha1).hexdigest()
        values['Authorization'] = ('q-sign-algorithm=sha1&q-ak=' + secret_id + '&q-sign-time=' + key_time +
            '&q-key-time=' + key_time + '&q-header-list=' + signed_headers + '&q-url-param-list=&q-signature=' + signature)
        return values
    except HubError: raise
    except (ValueError, KeyError, TypeError, OverflowError):
        raise HubError('CLOUDBASE_BACKUP_CREDENTIAL_INVALID', review=True) from None


@dataclass(frozen=True)
class CloudBaseCOSConfig(CloudBaseBackupConfig):
    # storage_mode remains 'classic': this is an existing classic CloudBase COS
    # resource, not an attempt to bypass PG storage metadata or RLS policy.
    reference_scheme: str = 'cos'
    bucket: str = ''
    region: str = ''
    owner_app_id: str = ''
    cloudbase_storage_binding_reference: str = ''

    @property
    def host(self): return self.bucket + '.cos.' + self.region + '.myqcloud.com'

    def validate(self):
        super().validate()
        if (self.reference_scheme != 'cos' or not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,95}-[0-9]{5,20}', self.bucket) or
                not re.fullmatch(r'[0-9]{5,20}', self.owner_app_id) or not self.bucket.endswith('-' + self.owner_app_id) or
                not re.fullmatch(r'[a-z]{2}-[a-z]+(?:-[0-9]+)?', self.region) or self.storage_hosts != (self.host,) or
                not isinstance(self.cloudbase_storage_binding_reference, str) or
                not self.cloudbase_storage_binding_reference.strip() or len(self.cloudbase_storage_binding_reference) > 2048):
            raise HubError('CLOUDBASE_COS_RESOURCE_BINDING_REQUIRED', review=True)


class CloudBaseCOSStorage:
    def __init__(self, config, credentials, *, request=open_request, clock=time.time, request_timeout=900):
        if type(request_timeout) not in (int, float) or not 1 <= request_timeout <= 900:
            raise HubError('CLOUDBASE_COS_TIMEOUT_INVALID')
        self.config, self.credentials, self.request, self.clock = config, credentials, request, clock
        self.request_timeout = request_timeout
        self.stats = {'api_requests': 0, 'object_puts': 0, 'object_gets': 0, 'object_heads': 0,
                      'uploaded_bytes': 0, 'readback_bytes': 0}

    def _key(self, key):
        if (not isinstance(key, str) or len(key) > 1024 or not re.fullmatch(r'[a-zA-Z0-9._/-]+', key) or
                not key.startswith(self.config.object_prefix + '/') or any(p in ('', '.', '..') for p in key.split('/'))):
            raise HubError('CLOUDBASE_COS_OBJECT_KEY_REJECTED')
        return key

    def _open(self, method, key, headers=None, body=None):
        self.config.validate(); self._key(key)
        path = '/' + key
        values = {'Host': self.config.host, 'Accept-Encoding': 'identity', **(headers or {})}
        try: credential = self.credentials()
        except Exception:
            raise HubError('CLOUDBASE_BACKUP_CREDENTIAL_UNAVAILABLE', review=True) from None
        signed = cos_sign_headers(method, path, values, credential, now=self.clock())
        return self.request('https://' + self.config.host + quote(path, safe='/'), [self.config.host],
                            method=method, headers=signed, body=body, timeout=self.request_timeout)

    @staticmethod
    def _check_response(response):
        if response.status in (200,201,204):return
        diagnostic={'http_status':response.status}
        try:
            # Capture only bounded status/Code, never Message, XML expansion,
            # headers, signed URLs, authentication or a raw error response.
            raw=response.read(65537)
            if len(raw)>65536:raise HubError('CLOUDBASE_BACKUP_RESPONSE_LIMIT')
            match=re.search(br'<Code>([A-Za-z_]{1,80})</Code>',raw)
            code=match.group(1).decode() if match else ''
            if code:diagnostic['provider_code']=code
            if response.status==404:raise HubError('CLOUDBASE_BACKUP_REMOTE_NOT_FOUND')
            if response.status in (409,412):raise HubError('CLOUDBASE_COS_OBJECT_EXISTS',review=True)
            if response.status==403:
                if code in ('ExpiredToken','InvalidSecurity','InvalidAccessKeyId','RequestExpired'):
                    raise HubError('CLOUDBASE_BACKUP_AUTH_EXPIRED',review=True)
                if code in ('RequestTimeTooSkewed','SignatureDoesNotMatch'):
                    raise HubError('CLOUDBASE_COS_SIGNATURE_REJECTED',review=True)
            CloudBaseClassicStorage._status(response.status)
        except HubError as exc:
            exc.safe_http=diagnostic;raise

    def put(self, key, source, size):
        self.config.validate(); self._key(key)
        if type(size) is not int or not 0 < size <= self.config.max_archive_bytes:
            raise HubError('CLOUDBASE_BACKUP_SIZE_LIMIT')
        connection = None
        try:
            self.stats['object_puts'] += 1
            connection, response = self._open('PUT', key, headers={
                'Content-Type': 'application/octet-stream', 'Content-Length': str(size),
                'x-cos-forbid-overwrite': 'true', 'x-cos-storage-class': 'STANDARD'}, body=source)
            self._check_response(response)
            if len(response.read(MAX_JSON + 1)) > MAX_JSON: raise HubError('CLOUDBASE_BACKUP_RESPONSE_LIMIT')
            self.stats['uploaded_bytes'] += size
            return 'cos://' + self.config.bucket + '/' + key
        except HubError as exc:
            if exc.code in ('CLOUDBASE_BACKUP_HTTP_REJECTED','CLOUDBASE_BACKUP_NETWORK_ERROR','CLOUDBASE_BACKUP_RESPONSE_LIMIT'):
                unknown=HubError('CLOUDBASE_BACKUP_UPLOAD_UNKNOWN',review=True)
                unknown.cause_code=exc.code
                if hasattr(exc,'safe_http'):unknown.safe_http=exc.safe_http
                raise unknown from None
            raise
        except (OSError, http.client.HTTPException):
            raise HubError('CLOUDBASE_BACKUP_UPLOAD_UNKNOWN', review=True) from None
        finally:
            if connection: connection.close()

    def verify(self, reference, key, expected_size, expected_sha256):
        self.config.validate(); self._key(key); _file_id(self.config, reference, key)
        connection = None
        try:
            self.stats['object_gets'] += 1
            connection, response = self._open('GET', key, headers={'Cache-Control': 'no-cache'})
            self._check_response(response)
            if response.status != 200 or response.getheader('Content-Encoding', 'identity') not in ('identity', ''):
                raise HubError('CLOUDBASE_BACKUP_READBACK_INVALID')
            count = 0; digest = hashlib.sha256()
            while True:
                chunk = response.read(min(1_048_576, expected_size - count + 1))
                if not chunk: break
                count += len(chunk); self.stats['readback_bytes'] += len(chunk); digest.update(chunk)
                if count > expected_size: raise HubError('CLOUDBASE_BACKUP_READBACK_MISMATCH')
            if count != expected_size or digest.hexdigest() != expected_sha256:
                raise HubError('CLOUDBASE_BACKUP_READBACK_MISMATCH')
            return {'bytes': count, 'sha256': digest.hexdigest(), 'method': 'full_remote_get'}
        except HubError: raise
        except (OSError, http.client.HTTPException):
            raise HubError('CLOUDBASE_BACKUP_NETWORK_ERROR', retryable=True) from None
        finally:
            if connection: connection.close()

    def head(self, reference, key):
        """Optional diagnostic. HEAD/ETag never substitutes for full GET proof."""
        self.config.validate(); self._key(key); _file_id(self.config, reference, key)
        connection = None
        try:
            self.stats['object_heads'] += 1
            connection, response = self._open('HEAD', key)
            self._check_response(response)
            try: size = int(response.getheader('Content-Length'))
            except (TypeError, ValueError): raise HubError('CLOUDBASE_BACKUP_READBACK_INVALID') from None
            if size < 0: raise HubError('CLOUDBASE_BACKUP_READBACK_INVALID')
            return {'bytes': size, 'method': 'head_only', 'remote_verified': False}
        except HubError: raise
        except (OSError, http.client.HTTPException):
            raise HubError('CLOUDBASE_BACKUP_NETWORK_ERROR', retryable=True) from None
        finally:
            if connection: connection.close()
