"""Opt-in CloudBase classic-storage backup; no deletion or scheduler side effects.

The HTTP API grants an object-specific COS upload, then provides a fresh download
URL. Both the archive and manifest are read back in full. Unique object names are
application-level write-once names, not a claim of provider object-lock support.
PG environments need a different transport; they are deliberately rejected here.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import unquote, urlsplit
import uuid

from adapters.safe_https import open_request, validate_destination
from services.hub.errors import HubError

VERSION = 'cloudbase-backup-v1'
KINDS = frozenset(('database_snapshot', 'tweet_archive', 'log_archive'))
MAX_JSON = 262144
MANIFEST_FIELDS = ('version', 'operation_id', 'env_id', 'kind', 'local_archive', 'archive_key',
                   'bytes', 'sha256', 'created_at', 'remote_verified', 'local_deletion_performed',
                   'archive_file_id', 'archive_readback')


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def hash_file(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def _file_id(config, value, key):
    try:
        parsed = urlsplit(value)
        scheme = getattr(config, 'reference_scheme', 'cloud')
        authority_ok = (parsed.netloc == getattr(config, 'bucket', None) if scheme == 'cos' else
                        parsed.netloc.startswith(config.env_id + '.'))
        if (parsed.scheme != scheme or parsed.query or parsed.fragment or parsed.username or parsed.password or
                not authority_ok or unquote(parsed.path) != '/' + key):
            raise ValueError()
    except (ValueError, TypeError):
        raise HubError('CLOUDBASE_BACKUP_REMOTE_ID_INVALID') from None
    return value


def _private_directory(path):
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):
        raise HubError('CLOUDBASE_BACKUP_PATH_REJECTED')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'posix' and path.stat().st_mode & 0o077:
        raise HubError('CLOUDBASE_BACKUP_RECEIPTS_NOT_PRIVATE')


def _atomic_receipt(path, value):
    _private_directory(path.parent)
    if path.exists() or path.is_symlink():
        raise HubError('CLOUDBASE_BACKUP_RECEIPT_EXISTS')
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.partial')
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, 'wb') as target:
            target.write(encoded(value)); target.flush(); os.fsync(target.fileno())
        os.replace(temp, path)
        if os.name == 'posix':
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(parent)
            finally: os.close(parent)
    finally:
        if temp.exists(): temp.unlink()


@dataclass(frozen=True)
class CloudBaseBackupConfig:
    enabled: bool = False
    env_id: str = ''
    storage_mode: str = 'unconfigured'
    object_prefix: str = 'lighthouse-hub/backups/v1'
    storage_hosts: tuple = ()
    existing_resource_authorized: bool = False
    private_storage_verified: bool = False
    max_archive_bytes: int = 0

    def validate(self):
        if self.enabled is not True:
            raise HubError('CLOUDBASE_BACKUP_DISABLED')
        if (self.existing_resource_authorized is not True or self.private_storage_verified is not True):
            raise HubError('CLOUDBASE_BACKUP_AUTHORIZATION_REQUIRED', review=True)
        if self.storage_mode != 'classic':
            raise HubError('CLOUDBASE_BACKUP_STORAGE_MODE_UNSUPPORTED', review=True)
        if (not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,95}', self.env_id) or
                not re.fullmatch(r'[a-z0-9][a-z0-9_/-]{2,127}', self.object_prefix) or
                any(p in ('', '.', '..') for p in self.object_prefix.split('/')) or
                type(self.max_archive_bytes) is not int or not 0 < self.max_archive_bytes <= 1_000_000_000 or
                not self.storage_hosts or not isinstance(self.storage_hosts, tuple) or
                any(not isinstance(h, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{2,252}', h) for h in self.storage_hosts)):
            raise HubError('CLOUDBASE_BACKUP_CONFIG_INVALID', review=True)


class CloudBaseClassicStorage:
    """HTTP transport; credentials are requested only after explicit activation.

    authorization(method, url, body) returns a documented Bearer access-token or
    per-request TC3 Authorization value. A CloudBase/CAM credential is not assumed
    interchangeable with another credential type. The callback must not log it.
    """
    def __init__(self, config, authorization, *, request=open_request):
        self.config, self.authorization, self.request = config, authorization, request
        self.stats = {'api_requests': 0, 'object_puts': 0, 'object_gets': 0,
                      'uploaded_bytes': 0, 'readback_bytes': 0}

    @staticmethod
    def _header(value):
        if not isinstance(value, str) or not value or len(value) > 16384 or any(ord(c) < 32 for c in value):
            raise HubError('CLOUDBASE_BACKUP_CREDENTIAL_INVALID')
        return value

    @staticmethod
    def _status(status):
        if status == 401: raise HubError('CLOUDBASE_BACKUP_AUTH_EXPIRED', review=True)
        if status == 403: raise HubError('CLOUDBASE_BACKUP_ACCESS_DENIED', review=True)
        if status == 429: raise HubError('CLOUDBASE_BACKUP_RATE_LIMITED', retryable=True)
        if status >= 500: raise HubError('CLOUDBASE_BACKUP_NETWORK_ERROR', retryable=True)
        if status not in (200, 201, 204): raise HubError('CLOUDBASE_BACKUP_HTTP_REJECTED')

    def _json(self, route, value):
        self.config.validate()
        host = self.config.env_id + '.api.tcloudbasegateway.com'
        url = 'https://' + host + '/v1/storages/' + route
        body = encoded(value)
        try: auth = self._header(self.authorization('POST', url, body))
        except HubError: raise
        except Exception:
            raise HubError('CLOUDBASE_BACKUP_CREDENTIAL_UNAVAILABLE', review=True) from None
        connection = None
        try:
            self.stats['api_requests'] += 1
            connection, response = self.request(url, [host], method='POST',
                headers={'Authorization': auth, 'Content-Type': 'application/json', 'Accept': 'application/json',
                         'Accept-Encoding': 'identity'}, body=body)
            self._status(response.status)
            data = response.read(MAX_JSON + 1)
            if len(data) > MAX_JSON: raise HubError('CLOUDBASE_BACKUP_RESPONSE_LIMIT')
            result = json.loads(data)
            if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
                raise HubError('CLOUDBASE_BACKUP_RESPONSE_INVALID')
            item = result[0]
            if item.get('code'):
                code = item['code']
                if code in ('STORAGE_EXCEED_AUTHORITY', 'ACTION_FORBIDDEN'):
                    raise HubError('CLOUDBASE_BACKUP_ACCESS_DENIED', review=True)
                if code == 'OBJECT_NOT_EXIST': raise HubError('CLOUDBASE_BACKUP_REMOTE_NOT_FOUND')
                raise HubError('CLOUDBASE_BACKUP_REMOTE_ERROR')
            return item
        except HubError: raise
        except (OSError, http.client.HTTPException):
            raise HubError('CLOUDBASE_BACKUP_NETWORK_ERROR', retryable=True) from None
        except (ValueError, TypeError, KeyError):
            raise HubError('CLOUDBASE_BACKUP_RESPONSE_INVALID') from None
        finally:
            if connection: connection.close()

    def _file_id(self, value, key):
        return _file_id(self.config, value, key)

    def _object_url(self, value, key):
        parsed = validate_destination(value, self.config.storage_hosts)
        if unquote(parsed.path) != '/' + key:
            raise HubError('CLOUDBASE_BACKUP_REMOTE_PATH_CHANGED')
        return value

    def put(self, key, source, size):
        self.config.validate()
        item = self._json('get-objects-upload-info', [{'objectId': key}])
        if item.get('objectId') != key:
            raise HubError('CLOUDBASE_BACKUP_REMOTE_PATH_CHANGED')
        try:
            file_id = self._file_id(item['cloudObjectId'], key)
            url = self._object_url(item['uploadUrl'], key)
            headers = {'Authorization': self._header(item['authorization']),
                'X-Cos-Security-Token': self._header(item['token']),
                'X-Cos-Meta-Fileid': self._header(item['cloudObjectMeta']),
                'Content-Type': 'application/octet-stream', 'Content-Length': str(size), 'Accept-Encoding': 'identity'}
        except KeyError:
            raise HubError('CLOUDBASE_BACKUP_RESPONSE_INVALID') from None
        connection = None
        try:
            self.stats['object_puts'] += 1
            connection, response = self.request(url, self.config.storage_hosts, method='PUT', headers=headers, body=source)
            self._status(response.status)
            if len(response.read(MAX_JSON + 1)) > MAX_JSON:
                raise HubError('CLOUDBASE_BACKUP_RESPONSE_LIMIT')
            self.stats['uploaded_bytes'] += size
            return file_id
        except HubError: raise
        except (OSError, http.client.HTTPException):
            # Unknown object PUT is not blindly retried. A new operation gets a
            # new UUID; future reconciliation can inspect this retained intent.
            raise HubError('CLOUDBASE_BACKUP_UPLOAD_UNKNOWN', review=True) from None
        finally:
            if connection: connection.close()

    def verify(self, file_id, key, expected_size, expected_sha256):
        self.config.validate(); self._file_id(file_id, key)
        item = self._json('get-objects-download-info', [{'cloudObjectId': file_id}])
        if item.get('cloudObjectId', file_id) != file_id:
            raise HubError('CLOUDBASE_BACKUP_REMOTE_ID_INVALID')
        try: url = self._object_url(item['downloadUrl'], key)
        except KeyError: raise HubError('CLOUDBASE_BACKUP_RESPONSE_INVALID') from None
        connection = None
        try:
            self.stats['object_gets'] += 1
            connection, response = self.request(url, self.config.storage_hosts, method='GET',
                headers={'Accept-Encoding': 'identity', 'Cache-Control': 'no-cache'}, body=None)
            self._status(response.status)
            if response.status != 200 or response.getheader('Content-Encoding', 'identity') not in ('identity', ''):
                raise HubError('CLOUDBASE_BACKUP_READBACK_INVALID')
            digest = hashlib.sha256(); count = 0
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


class CloudBaseBackup:
    """Upload frozen files from backups/, then retain a durable verification receipt.

    This component never deletes local/remote files and never uploads active
    data/, media/, browser profiles, credentials or logs/ directly. A caller must
    first create a consistent DB snapshot or frozen tweet/log archive.
    """
    def __init__(self, root, config, storage):
        self.root, self.config, self.storage = Path(root), config, storage

    def _archive(self, source):
        path = Path(source)
        if (not self.root.is_absolute() or self.root.is_symlink() or any(p.is_symlink() for p in self.root.parents) or
                not path.is_absolute() or path.is_symlink() or any(p.is_symlink() for p in path.parents) or
                not path.is_file() or not path.resolve().is_relative_to((self.root/'backups').resolve()) or
                path.resolve().is_relative_to((self.root/'backups/cloudbase-receipts').resolve()) or
                (self.root/'backups').is_symlink() or path.name.endswith(('-wal', '-shm', '.partial'))):
            raise HubError('CLOUDBASE_BACKUP_PATH_REJECTED', review=True)
        return path

    def upload_archive(self, source, *, kind):
        self.config.validate()
        if kind not in KINDS: raise HubError('CLOUDBASE_BACKUP_KIND_REJECTED')
        source = self._archive(source); size = source.stat().st_size
        if not 0 < size <= self.config.max_archive_bytes:
            raise HubError('CLOUDBASE_BACKUP_SIZE_LIMIT')
        sha256 = hash_file(source); operation = str(uuid.uuid4())
        prefix = self.config.object_prefix + '/' + operation
        key = prefix + '/' + sha256 + '.archive'; manifest_key = prefix + '/manifest.json'
        relative = str(source.relative_to(self.root)).replace('\\', '/')
        intent = {'version': VERSION, 'operation_id': operation, 'env_id': self.config.env_id,
            'kind': kind, 'local_archive': relative, 'archive_key': key, 'bytes': size, 'sha256': sha256,
            'created_at': datetime.now(timezone.utc).isoformat(), 'remote_verified': False, 'local_deletion_performed': False}
        receipts = self.root/'backups/cloudbase-receipts'
        _atomic_receipt(receipts/(operation+'.intent.json'), intent)
        with source.open('rb') as data:
            file_id = self.storage.put(key, data, size)
        archive_check = self.storage.verify(file_id, key, size, sha256)
        manifest = {**intent, 'archive_file_id': file_id,
                    'archive_readback': archive_check, 'remote_verified': True}
        manifest_bytes = encoded(manifest); manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_id = self.storage.put(manifest_key, io.BytesIO(manifest_bytes), len(manifest_bytes))
        manifest_check = self.storage.verify(manifest_id, manifest_key, len(manifest_bytes), manifest_hash)
        if source.stat().st_size != size or hash_file(source) != sha256:
            raise HubError('CLOUDBASE_BACKUP_LOCAL_ARCHIVE_CHANGED', review=True)
        result = {**manifest, 'manifest_key': manifest_key, 'manifest_file_id': manifest_id,
            'manifest_sha256': manifest_hash, 'manifest_readback': manifest_check,
            'verified_at': datetime.now(timezone.utc).isoformat(),
            'verification_method': 'archive_and_manifest_full_remote_get', 'remote_provider_object_lock_claimed': False}
        target = receipts/(operation+'.verified.json'); _atomic_receipt(target, result)
        return {**result, 'receipt_file': str(target)}

    def archive_prune_eligibility(self, source, receipt_file, *, max_age_seconds=86400):
        """Return a candidate only. Caller must preserve required recovery points.

        The boolean is never permission to delete active DBs, original tweet
        files, logs, video material, a pending/unknown job, or the last needed
        local recovery point. Independent policy belongs to the caller.
        """
        self.config.validate(); source = self._archive(source)
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86400:
            raise HubError('CLOUDBASE_BACKUP_RECEIPT_INVALID', review=True)
        receipt = Path(receipt_file); directory = self.root/'backups/cloudbase-receipts'
        if (receipt.is_symlink() or any(p.is_symlink() for p in receipt.parents) or
                receipt.parent != directory or not receipt.is_file() or
                receipt.stat().st_size > MAX_JSON or not receipt.name.endswith('.verified.json') or
                (os.name == 'posix' and receipt.stat().st_mode & 0o077)):
            raise HubError('CLOUDBASE_BACKUP_RECEIPT_INVALID', review=True)
        try:
            value = json.loads(receipt.read_bytes())
            verified_at = datetime.fromisoformat(value['verified_at'])
            if verified_at.tzinfo is None: raise ValueError()
            verified = verified_at.timestamp()
            age = time.time() - verified
            manifest_bytes = encoded({k: value[k] for k in MANIFEST_FIELDS})
            manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
            if (value['version'] != VERSION or value['env_id'] != self.config.env_id or
                    value['kind'] not in KINDS or value['remote_verified'] is not True or
                    value['verification_method'] != 'archive_and_manifest_full_remote_get' or
                    value['local_deletion_performed'] is not False or not 0 <= age <= max_age_seconds or
                    value['local_archive'] != str(source.relative_to(self.root)).replace('\\', '/') or
                    source.stat().st_size != value['bytes'] or hash_file(source) != value['sha256'] or
                    value['archive_readback'] != {'bytes': value['bytes'], 'sha256': value['sha256'], 'method': 'full_remote_get'} or
                    value['manifest_readback']['sha256'] != value['manifest_sha256'] or
                    value['manifest_sha256'] != manifest_sha256 or
                    value['manifest_readback']['bytes'] != len(manifest_bytes) or
                    value['manifest_readback']['method'] != 'full_remote_get'):
                raise ValueError()
            operation = str(uuid.UUID(value['operation_id']))
            if (receipt.name != operation + '.verified.json' or
                    value['archive_key'] != self.config.object_prefix + '/' + operation + '/' + value['sha256'] + '.archive' or
                    value['manifest_key'] != self.config.object_prefix + '/' + operation + '/manifest.json'):
                raise ValueError()
            _file_id(self.config, value['archive_file_id'], value['archive_key'])
            _file_id(self.config, value['manifest_file_id'], value['manifest_key'])
        except (ValueError, KeyError, TypeError, OSError):
            raise HubError('CLOUDBASE_BACKUP_RECEIPT_INVALID', review=True) from None
        return {'archive_candidate': str(source), 'verified_receipt': str(receipt),
                'remote_verified': True, 'additional_retention_policy_required': True,
                'local_deletion_performed': False}
