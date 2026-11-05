import json
import os
import hashlib
import re
from urllib.parse import urlsplit
from datetime import datetime, timezone
import httpx
from services.hub.errors import HubError
from services.hub.db import encode


def x_resource_bounds(method, url, params):
    """Independent bounds for the three supported ordinary official read routes."""
    try:
        u = urlsplit(url)
        query = {} if params is None else params
        if (method != 'GET' or u.scheme != 'https' or u.hostname != 'api.x.com' or
                u.username or u.password or u.port is not None or u.query or u.fragment or
                not isinstance(query, dict)):
            raise ValueError()
        expansions = query.get('expansions', '')
        if not isinstance(expansions, str):
            raise ValueError()
        expanded = set(expansions.split(',')) if expansions else set()
        if any(not item for item in expanded):
            raise ValueError()
        if re.fullmatch(r'/2/users/by/username/[A-Za-z0-9_]{1,15}', u.path):
            if not set(query) <= {'user.fields'} or expanded:
                raise ValueError()
            return {'endpoint_kind': 'user_lookup', 'posts': 0, 'users': 1}
        if re.fullmatch(r'/2/users/[0-9]{1,30}/tweets', u.path):
            allowed = {'user.fields', 'tweet.fields', 'expansions', 'media.fields',
                       'max_results', 'since_id', 'pagination_token', 'start_time'}
            count = query.get('max_results')
            if isinstance(count, str) and re.fullmatch(r'[1-9][0-9]{0,2}', count):
                count = int(count)
            if (not set(query) <= allowed or not expanded <= {'attachments.media_keys'} or
                    type(count) is not int or not 5 <= count <= 100):
                raise ValueError()
            return {'endpoint_kind': 'timeline', 'posts': count, 'users': 0}
        if re.fullmatch(r'/2/tweets/[0-9]{1,30}', u.path):
            if (not set(query) <= {'user.fields', 'tweet.fields', 'expansions', 'media.fields'} or
                    not expanded <= {'attachments.media_keys', 'author_id'}):
                raise ValueError()
            return {'endpoint_kind': 'post_detail', 'posts': 1, 'users': int('author_id' in expanded)}
        raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise HubError('X_RESOURCE_REQUEST_NOT_SUPPORTED', review=True) from None


def x_returned_resources(value, bounds):
    """Count returned data + includes; never infer free duplicate resources."""
    if not isinstance(value, dict) or value.get('errors'):
        raise HubError('X_RESOURCE_RESPONSE_INVALID', review=True)
    includes = value.get('includes', {})
    if not isinstance(includes, dict):
        raise HubError('X_RESOURCE_RESPONSE_INVALID', review=True)
    kind = bounds['endpoint_kind']
    data = value.get('data', [] if kind == 'timeline' else None)
    if kind == 'timeline':
        if not isinstance(data, list):
            raise HubError('X_RESOURCE_RESPONSE_INVALID', review=True)
        posts, users = data, []
    else:
        if not isinstance(data, dict):
            raise HubError('X_RESOURCE_RESPONSE_INVALID', review=True)
        posts, users = ([data], []) if kind == 'post_detail' else ([], [data])
    if (any(not isinstance(includes.get(k, []), list) for k in ('tweets', 'users', 'media')) or
            any(not isinstance(item, dict) for item in includes.get('media', []))):
        raise HubError('X_RESOURCE_RESPONSE_INVALID', review=True)
    posts = posts + includes.get('tweets', [])
    users = users + includes.get('users', [])
    if any(not isinstance(item, dict) or not isinstance(item.get('id'), str) or
           not re.fullmatch(r'[0-9]{1,30}', item['id'])
           for item in posts + users):
        raise HubError('X_RESOURCE_RESPONSE_INVALID', review=True)
    counts = {'posts': len(posts), 'users': len(users)}
    if any(counts[key] > bounds[key] for key in counts):
        raise HubError('X_RESOURCE_RESPONSE_BOUND_EXCEEDED', review=True)
    return counts


class ProviderHTTP:
    def __init__(self, budget, transport=None):
        self.budget = budget
        self.transport = transport

    def request(self, provider, method, url, *, job_id=None, headers=None, params=None, body=None,
                content=None, response_check=None, units=1, reserved_cost_usd=None, _receipt=None):
        if body is not None and content is not None:
            raise HubError('API_BODY_ENCODING_CONFLICT', review=True)
        bounds = x_resource_bounds(method, url, params) if provider == 'x' else None
        if provider == 'x' and (body is not None or content is not None):
            raise HubError('X_RESOURCE_REQUEST_NOT_SUPPORTED', review=True)
        call_id = self.budget.reserve(provider, job_id, units=units, reserved_cost_usd=reserved_cost_usd,
                                      x_resource_bounds=bounds)
        try:
            with httpx.Client(timeout=httpx.Timeout(45, connect=10), follow_redirects=False,
                              verify=True, trust_env=False, transport=self.transport) as client:
                with client.stream(method, url, headers=headers, params=params, json=body, content=content) as response:
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 5 * 1024 * 1024:
                            raise HubError('API_RESPONSE_TOO_LARGE', review=True)
                    status = response.status_code
                    if status in (401,):
                        raise HubError('AUTH_EXPIRED', review=True)
                    if status == 402:
                        raise HubError('INSUFFICIENT_BALANCE', review=True)
                    if status == 403:
                        code = 'QUOTA_EXHAUSTED' if b'quotaExceeded' in data or b'dailyLimitExceeded' in data else 'PERMISSION_DENIED'
                        raise HubError(code, review=True)
                    if status == 429:
                        value = response.headers.get('Retry-After', '60')
                        delay = min(max(int(value), 1), 86400) if value.isdigit() else 60
                        raise HubError('API_RATE_LIMITED', retryable=True, retry_after=delay)
                    if 500 <= status:
                        raise HubError('API_SERVER_ERROR', retryable=True)
                    if not 200 <= status < 300:
                        raise HubError('API_REQUEST_REJECTED', review=True)
                    try:
                        value = json.loads(data)
                    except (ValueError, UnicodeDecodeError):
                        raise HubError('API_INVALID_JSON', retryable=True) from None
            if response_check is not None:
                response_check(value)
            counts = x_returned_resources(value, bounds) if bounds else None
            self.budget.finish(call_id, 'done', resource_counts=counts)
            if provider == 'model' and isinstance(value, dict):
                usage = value.get('usage') or {}
                inputs = usage.get('input_tokens',usage.get('prompt_tokens'))
                outputs = usage.get('output_tokens',usage.get('completion_tokens'))
                if not isinstance(inputs,int) or not isinstance(outputs,int) or min(inputs,outputs) < 0:
                    inputs,outputs = None,None
                # Relay pricing can differ from official pricing; missing estimates remain unknown.
                input_price,output_price = os.getenv('MODEL_INPUT_USD_PER_MILLION'),os.getenv('MODEL_OUTPUT_USD_PER_MILLION')
                estimate = None
                if inputs is not None and input_price and output_price:
                    estimate = (inputs * float(input_price) + outputs * float(output_price)) / 1_000_000
                with self.budget.store.transaction() as db:
                    db.execute('INSERT OR REPLACE INTO api_usage_details VALUES(?,?,?,?,?)',
                               (call_id,value.get('model') or (body or {}).get('model'),inputs,outputs,estimate))
            if _receipt is not None:
                request_bytes=response.request.content
                _receipt.append({'call_id':call_id,'raw_body':bytes(data),
                    'request_sha256':hashlib.sha256(encode(json.loads(request_bytes)).encode()).hexdigest() if body is not None else None,
                    'request_wire_sha256':hashlib.sha256(request_bytes).hexdigest(),
                    'request_method':response.request.method,
                    'endpoint_sha256':hashlib.sha256(str(response.request.url).encode()).hexdigest(),
                    'transport_mode':'default_tls' if self.transport is None else 'custom_transport'})
            return value
        except httpx.TransportError:
            self.budget.finish(call_id, 'unknown', 'NETWORK_ERROR')
            raise HubError('NETWORK_ERROR', retryable=True) from None
        except HubError as exc:
            self.budget.finish(call_id, 'error', exc.code)
            raise
        except BaseException:
            self.budget.finish(call_id, 'unknown', 'CALL_INTERRUPTED')
            raise

    def request_recorded(self, provider, method, url, *, job_id, headers, params=None,
                         reserved_cost_usd=None, response_check=None):
        """Internal X source capture; no headers, credentials or arbitrary URLs in receipts."""
        try:
            parsed=urlsplit(url);port=parsed.port
        except (ValueError,TypeError):
            raise HubError('SOURCE_REQUEST_NOT_SUPPORTED',review=True) from None
        allowed={'user.fields','tweet.fields','expansions','media.fields','max_results','since_id','pagination_token','start_time'}
        if (provider!='x' or method!='GET' or not job_id or parsed.scheme!='https' or
                parsed.hostname!='api.x.com' or parsed.username or parsed.password or parsed.query or parsed.fragment or
                port is not None or not re.fullmatch(r'/2/(?:users/(?:by/username/[A-Za-z0-9_]{1,15}|[0-9]{1,30}/tweets)|tweets/[0-9]{1,30})',parsed.path) or
                not isinstance(params if params is not None else {},dict) or
                not set(params or {})<=allowed or not isinstance(headers,dict) or
                not isinstance(headers.get('Authorization'),str) or not headers['Authorization'].startswith('Bearer ')):
            raise HubError('SOURCE_REQUEST_NOT_SUPPORTED',review=True)
        if params and 'start_time' in params:
            value=params['start_time']
            try:
                if (not re.fullmatch(r'/2/users/[0-9]{1,30}/tweets',parsed.path) or not isinstance(value,str) or
                    not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z',value)):
                    raise ValueError()
                start=datetime.fromisoformat(value.replace('Z','+00:00'))
                if not 0<=start.timestamp()<=datetime.now(timezone.utc).timestamp():raise ValueError()
            except (ValueError,OverflowError,OSError):
                raise HubError('SOURCE_REQUEST_NOT_SUPPORTED',review=True) from None
        captured=[]
        value=self.request(provider,method,url,job_id=job_id,headers=headers,params=params,
                           reserved_cost_usd=reserved_cost_usd,response_check=response_check,_receipt=captured)
        if len(captured)!=1:
            raise HubError('SOURCE_HTTP_RECEIPT_MISSING',review=True)
        item=captured[0]
        # The source ledger later checks these hashes against an independently persisted API record.
        with self.budget.store.transaction() as db:
            call=db.execute('SELECT * FROM api_calls WHERE call_id=?',(item['call_id'],)).fetchone()
            if not call or call['provider']!='x' or call['state']!='done' or call['job_id']!=job_id:
                raise HubError('SOURCE_API_CALL_NOT_CONFIRMED',review=True)
            db.execute('INSERT INTO source_http_receipts VALUES(?,?,?,?,?,?,?,?,?,?)',
                (item['call_id'],job_id,'GET','api.x.com',parsed.path,encode(params or {}),
                 hashlib.sha256(item['raw_body']).hexdigest(),hashlib.sha256(encode(value).encode()).hexdigest(),
                 'default_tls' if self.transport is None else 'custom_transport',call['finished_at']))
            item['captured_at']=call['finished_at']
            item['cost_reservation']={'provider':'x','reserved_upper_bound_usd':call['cost_usd'],
                'http_attempt_units':call['units'],'actual_resource_charge_verified':False}
            resource=db.execute('SELECT * FROM api_resource_usage WHERE call_id=?',(item['call_id'],)).fetchone()
            if not resource or resource['state']!='estimated':
                raise HubError('SOURCE_X_RESOURCE_ESTIMATE_MISSING',review=True)
            item['cost_reservation'].update(reserved_upper_bound_usd=resource['reserved_upper_bound_usd'],
                response_estimate_usd=resource['settled_upper_bound_usd'],
                returned_posts=resource['returned_posts'],returned_users=resource['returned_users'],
                pricing_version=resource['pricing_version'],pricing_source=json.loads(resource['pricing_source']))
        # This is pending only. The source recorder must durably save the raw
        # receipt before confirm_saved_receipt may grant a UTC estimate credit.
        self.budget.note_x_receipt(item['call_id'])
        return value,item
