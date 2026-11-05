"""Genuine bounded capture of two owner-authorized old sources; never publishes.

Uses only the existing image's recorded HTTP, budget and source receipt code.
No v4 source/model/renderer module is required for this raw capture phase.
"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from adapters.http import ProviderHTTP
from adapters.x import TWEET_FIELDS, MEDIA_FIELDS, USER_FIELDS, _author
from services.hub.budget import Budget
from services.hub.config import Settings
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.locks import resource_guard
from services.hub.source_receipts import SourceReceipts, digest

VERSION = 'owner-two-reference-source-capture-v2'
GRANT_VERSION = 'owner-two-reference-backfill-grant-v2'
SITE_UID = '1000000000000000'
HANDLE = 'example-channel-b'
AUTHOR_ID = '1986339274611159040'
LIVE_HUB_SHA = 'e2b874d7ffb40600083ec534fc8ae449b4dd6f42ff9b775dacbe1d6e89b43df8'
START_TIME = '2026-11-01T10:00:00Z'
TARGETS = {
    '2116863407962108290': [('quoted', '2115350083315302605'), ('replied_to', '2116863357798215887')],
    '2116836170986664205': [('quoted', '2116832933575282739')],
}
DETAIL_IDS = sorted({ident for refs in TARGETS.values() for kind, ident in refs})
SCOPES = ['capture_exact_sources_with_post_age_exception', 'translate_check_render_exact_sources', 'publish_each_source_once']
GRANT_KEYS = {'version', 'grant_id', 'source_ids', 'target_site_uid', 'author_id', 'author_handle',
    'owner_response', 'owner_response_reference', 'source_urls', 'issued_at', 'expires_at', 'live_hub_sha256',
    'scopes', 'max_timeline_requests', 'max_detail_requests', 'max_user_requests', 'max_publications_per_source', 'human_review'}
SHA = re.compile('[0-9a-f]{64}')


def fail(code):
    raise HubError(code)


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                fail('ONEOFF_CAPTURE_DUPLICATE_JSON_FIELD')
            value[key] = item
        return value
    def invalid(value):
        fail('ONEOFF_CAPTURE_JSON_INVALID')
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        fail('ONEOFF_CAPTURE_JSON_INVALID')


def is_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def reference_text(value):
    return isinstance(value, str) and 1 <= len(value.strip()) <= 8000 and '\x00' not in value


def validate_grant(value, *, require_live=True):
    now = time.time()
    numbers = all(type(value.get(k)) in (int, float) and math.isfinite(value[k]) for k in ('issued_at', 'expires_at'))
    if (set(value) != GRANT_KEYS or value['version'] != GRANT_VERSION or not is_uuid(value['grant_id']) or
            value['source_ids'] != sorted(TARGETS) or value['target_site_uid'] != SITE_UID or
            value['author_id'] != AUTHOR_ID or value['author_handle'] != HANDLE or
            value['source_urls'] != ['https://x.com/'+HANDLE+'/status/'+ident for ident in sorted(TARGETS)] or
            not reference_text(value['owner_response']) or not reference_text(value['owner_response_reference']) or
            value['live_hub_sha256'] != LIVE_HUB_SHA or value['scopes'] != SCOPES or value['human_review'] is not False or
            not numbers or not 0 <= value['issued_at'] <= now+5 or not 0 < value['expires_at']-value['issued_at'] <= 3600 or
            any(type(value[key]) is not int or value[key] != number for key, number in (
                ('max_timeline_requests', 2), ('max_detail_requests', 3), ('max_user_requests', 1), ('max_publications_per_source', 1)))):
        fail('ONEOFF_CAPTURE_ROOT_GRANT_INVALID')
    if require_live and not value['issued_at']-5 <= now < value['expires_at']:
        fail('ONEOFF_CAPTURE_ROOT_GRANT_EXPIRED')


class RootGrant:
    def __init__(self, settings, file, expected_sha):
        self.settings, self.file, self.expected_sha = settings, Path(file), expected_sha
        self.value, self.ref = self.check()

    def check(self, *, require_live=True):
        expected = self.settings.root/'config/oneoff-reference-backfill-v2/grant.json'
        if self.file != expected or not SHA.fullmatch(self.expected_sha or '') or self.file.resolve() != self.file:
            fail('ONEOFF_CAPTURE_ROOT_GRANT_PATH_INVALID')
        if not self.file.is_file() or self.file.stat().st_size > 16384:
            fail('ONEOFF_CAPTURE_ROOT_GRANT_SIZE_INVALID')
        for item in (self.file, *self.file.parents):
            stat = item.stat()
            if item.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
                fail('ONEOFF_CAPTURE_ROOT_GRANT_UNPROTECTED')
        raw = self.file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.expected_sha:
            fail('ONEOFF_CAPTURE_ROOT_GRANT_HASH_CHANGED')
        value = strict_json(raw);validate_grant(value, require_live=require_live)
        if sha256_file(self.settings.root/'config/hub.toml') != value['live_hub_sha256']:
            fail('ONEOFF_CAPTURE_LIVE_HUB_CHANGED')
        if (self.settings.config.get('target_bilibili_site_uid') != SITE_UID or
                HANDLE not in {s['x_handle'] for s in self.settings.config['sources']}):
            fail('ONEOFF_CAPTURE_CONFIGURED_SCOPE_CHANGED')
        return value, {'file': str(self.file), 'sha256': self.expected_sha, 'bytes': len(raw)}


def write_new(file, value):
    file = Path(file);file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if file.exists() or any(p.is_symlink() for p in (file, *file.parents)):
        fail('ONEOFF_CAPTURE_IMMUTABLE_FILE_EXISTS')
    raw = (encode(value)+'\n').encode('utf-8')
    with file.open('xb') as stream:
        os.chmod(file, 0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
    from services.hub.web_source import WebSources
    WebSources._sync_dir(file.parent)
    return {'file': str(file), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def normal_cursors(store):
    return store.query("SELECT key,value FROM source_cursors WHERE key LIKE 'x-api:%' ORDER BY key")


def require_not_cleared(store):
    if store.query("SELECT 1 FROM source_tombstones WHERE source_platform='x' AND source_id IN (?,?)", tuple(sorted(TARGETS))):
        fail('OWNER_BACKLOG_CLEARED')


def inspect(settings, store):
    parents = store.query("SELECT job_id,source_id,job_type,state,stage,attempts,error_code,policy_version FROM jobs WHERE source_platform='x' AND source_id IN (?,?) ORDER BY source_id", tuple(sorted(TARGETS)))
    pubs = store.query("SELECT job_id,source_id,account_id,kind,state,external_id,external_url FROM publications WHERE source_platform='x' AND source_id IN (?,?) ORDER BY source_id", tuple(sorted(TARGETS)))
    return {'version': VERSION, 'state': 'plan_only', 'source_ids': sorted(TARGETS), 'author_id': AUTHOR_ID,
        'author_handle': HANDLE, 'target_site_uid': SITE_UID, 'parents': parents, 'publications': pubs,
        'root_grant_file': str(settings.root/'config/oneoff-reference-backfill-v2/grant.json'),
        'maximum_timeline_requests': 2, 'maximum_detail_requests': 3, 'maximum_user_requests': 1,
        'maximum_x_resource_reservation_usd': 0.155, 'normal_policy_age_seconds_unchanged': 7200,
        'normal_cursors': normal_cursors(store), 'paid_calls': 0, 'publications_created': 0, 'human_review': False}


def claim_capture(settings, store, grant):
    value, ref = grant.check()
    require_not_cleared(store)
    source_id = 'owner-two-reference-backfill-v2:'+value['grant_id']
    payload = {'version': VERSION, 'source_ids': sorted(TARGETS), 'target_site_uid': SITE_UID,
        'author_id': AUTHOR_ID, 'author_handle': HANDLE, 'grant': ref, 'normal_cursors': normal_cursors(store)}
    now = time.time();owner = str(uuid.uuid4());ident = str(uuid.uuid4())
    lease = settings.config['lease_seconds'];maximum = settings.config['max_attempts']
    with store.transaction() as db:
        row = db.execute("SELECT * FROM jobs WHERE source_platform='owner_backfill' AND source_id=? AND job_type='x_discovery' AND policy_version=?", (source_id, VERSION)).fetchone()
        if row is not None:
            if json.loads(row['payload']) != payload:
                fail('ONEOFF_CAPTURE_JOB_BINDING_CHANGED')
            if row['state'] == 'upload_pending' and json.loads(row['result']).get('capture_descriptor'):
                return store.get(row['job_id']), False
            if row['state'] not in ('failed', 'processing', 'pending', 'retry_scheduled') or row['attempts'] >= maximum:
                fail('ONEOFF_CAPTURE_ATTEMPTS_EXHAUSTED_OR_STATE_INVALID')
            ident = row['job_id']
            db.execute("UPDATE jobs SET state='processing',error_code=NULL,attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE job_id=?", (owner, now+lease, now, ident))
        else:
            if db.execute("SELECT 1 FROM publications WHERE source_platform='x' AND source_id IN (?,?) AND account_id=?", (*sorted(TARGETS), 'site_uid:'+SITE_UID)).fetchone():
                fail('ONEOFF_CAPTURE_SOURCE_ALREADY_RESERVED')
            if db.execute("SELECT 1 FROM jobs WHERE source_platform='x' AND source_id IN (?,?) AND job_type='tweet_process'", tuple(sorted(TARGETS))).fetchone():
                fail('ONEOFF_CAPTURE_EXISTING_PARENT_REQUIRES_REUSE')
            result = {'capture': {'version': VERSION, 'requests': [], 'user_call_id': None,
                'timeline_call_ids': [], 'target_timeline_call_ids': {}, 'detail_call_ids': {}, 'cursor_unchanged': True}}
            db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,
                attempts,policy_version,payload,result,stage,lease_owner,lease_until)
                VALUES(?,'owner_backfill',?,'x_discovery','tweet','processing',?,?,1,?,?,?,'oneoff_capture_claimed',?,?)""",
                (ident, source_id, now, now, VERSION, encode(payload), encode(result), owner, now+lease))
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now, 'owner-backfill-v2', 'claim_two_source_capture', ident, encode({'grant': ref, 'source_ids': sorted(TARGETS),
                'ordinary_jobs_claimed': 0, 'attempts_reset': False, 'human_review': False})))
    return store.get(ident), True


@contextmanager
def capture_lease(settings, store, job, grant):
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(min(30, settings.config['lease_seconds']/3)):
            if not store.heartbeat(job, settings.config['lease_seconds']):
                return
    thread = threading.Thread(target=heartbeat, daemon=True);thread.start()
    def check():
        grant.check();require_not_cleared(store);SourceReceipts(settings, store)._parent(job)
    try:
        check();yield check
    finally:
        stop.set();thread.join(timeout=2)


def target_post(post):
    ident = post.get('id') if isinstance(post, dict) else None
    if ident not in TARGETS or post.get('author_id') != AUTHOR_ID:
        fail('ONEOFF_CAPTURE_TARGET_IDENTITY_MISMATCH')
    refs = post.get('referenced_tweets', [])
    actual = [(r.get('type'), r.get('id')) for r in refs if isinstance(r, dict)] if isinstance(refs, list) else []
    if len(actual) != len(TARGETS[ident]) or len(actual) != len(refs) or set(actual) != set(TARGETS[ident]):
        fail('ONEOFF_CAPTURE_RELATIONSHIP_CHANGED')
    try:
        moment = datetime.fromisoformat(post['created_at'].replace('Z', '+00:00'))
        if moment.tzinfo is None or moment.timestamp() > time.time()+300:
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        fail('ONEOFF_CAPTURE_ORIGINAL_TIME_INVALID')
    return post


def checked_receipt(receipts, ident):
    row = receipts._receipt(ident);body = receipts._body(row)
    if row['method'] != 'GET' or row['host'] != 'api.x.com':
        fail('ONEOFF_CAPTURE_RECEIPT_NOT_OFFICIAL')
    return row, body


def validate_descriptor(settings, store, descriptor_ref, *, grant_ref=None):
    file = Path(descriptor_ref['file'])
    if (file.resolve() != file or not file.is_relative_to(settings.root/'media') or
            file.name != 'capture-descriptor.json' or any(p.is_symlink() for p in (file, *file.parents))):
        fail('ONEOFF_CAPTURE_DESCRIPTOR_PATH_INVALID')
    raw = file.read_bytes()
    if len(raw) != descriptor_ref['bytes'] or hashlib.sha256(raw).hexdigest() != descriptor_ref['sha256']:
        fail('ONEOFF_CAPTURE_DESCRIPTOR_HASH_CHANGED')
    value = strict_json(raw)
    if value['version'] != VERSION or value['source_ids'] != sorted(TARGETS) or value['human_review'] is not False or value['publication_authorized'] is not False:
        fail('ONEOFF_CAPTURE_DESCRIPTOR_INVALID')
    if grant_ref is not None and value['grant'] != grant_ref:
        fail('ONEOFF_CAPTURE_DESCRIPTOR_GRANT_CHANGED')
    job = store.get(value['capture_job_id'])
    if not job or job['job_type'] != 'x_discovery' or job['source_platform'] != 'owner_backfill' or job['policy_version'] != VERSION:
        fail('ONEOFF_CAPTURE_DESCRIPTOR_JOB_CHANGED')
    if job['payload']['grant'] != value['grant'] or file.parent.parent != settings.root/'media'/job['job_id']/'owner-two-reference-capture-v2' or not is_uuid(file.parent.name):
        fail('ONEOFF_CAPTURE_DESCRIPTOR_JOB_BINDING_CHANGED')
    receipts = SourceReceipts(settings, store)
    user, user_body = checked_receipt(receipts, value['user_call_id'])
    author = _author(user_body.get('data'), HANDLE)
    if author['id'] != AUTHOR_ID or user['path'] != '/2/users/by/username/'+HANDLE:
        fail('ONEOFF_CAPTURE_AUTHOR_RECEIPT_CHANGED')
    all_rows = {user['call_id']: user}
    for ident in sorted(TARGETS):
        entry = value['targets'][ident];page, body = checked_receipt(receipts, entry['timeline_call_id'])
        if page['discovery_job_id'] != job['job_id'] or page['path'] != '/2/users/'+AUTHOR_ID+'/tweets':
            fail('ONEOFF_CAPTURE_TIMELINE_OWNER_CHANGED')
        found = [p for p in body.get('data', []) if isinstance(p, dict) and p.get('id') == ident]
        if len(found) != 1 or target_post(found[0]) != entry['original_post']:
            fail('ONEOFF_CAPTURE_ORIGINAL_POST_CHANGED')
        if entry['detail_call_ids'] != [value['detail_call_ids'][pid] for kind, pid in TARGETS[ident]]:
            fail('ONEOFF_CAPTURE_DETAIL_BINDING_CHANGED')
        all_rows[page['call_id']] = page
    for post_id in DETAIL_IDS:
        row, body = checked_receipt(receipts, value['detail_call_ids'][post_id])
        if row['discovery_job_id'] != job['job_id'] or row['path'] != '/2/tweets/'+post_id or body.get('data', {}).get('id') != post_id:
            fail('ONEOFF_CAPTURE_DETAIL_OWNER_CHANGED')
        all_rows[row['call_id']] = row
    maximum = min(86400, settings.config.get('source_receipt_max_age_seconds', 86400));now = time.time()
    if any(not 0 <= now-r['captured_at'] <= maximum for r in all_rows.values()):
        fail('SOURCE_RECEIPT_EXPIRED_OR_FUTURE')
    if {ident: row['raw_sha256'] for ident, row in all_rows.items()} != value['receipt_raw_sha256']:
        fail('ONEOFF_CAPTURE_RAW_HASH_CHANGED')
    recorded = {ident: checked_receipt(receipts, ident)[0] for ident in value['all_recorded_call_ids']}
    if (not set(all_rows) <= set(recorded) or any(r['discovery_job_id'] != job['job_id'] for ident, r in recorded.items() if ident != user['call_id']) or
            value['all_transport_default_tls'] is not all(r['transport_mode'] == 'default_tls' for r in recorded.values())):
        fail('ONEOFF_CAPTURE_TRANSPORT_OR_OWNER_CHANGED')
    return value


def capture(settings, store, grant, *, http=None):
    if not isinstance(grant, RootGrant):
        fail('ONEOFF_CAPTURE_ROOT_GRANT_REQUIRED')
    with resource_guard(settings.root/'data/locks/tweet'):
        value, grant_ref = grant.check()
        job, claimed = claim_capture(settings, store, grant)
        if not claimed:
            descriptor = job['result']['capture_descriptor']
            validate_descriptor(settings, store, descriptor, grant_ref=grant_ref)
            return {'version': VERSION, 'state': 'existing_verified_capture', 'capture_job_id': job['job_id'],
                'capture_descriptor': descriptor, 'new_api_calls': 0, 'publications_created': 0, 'human_review': False}
        receipts = SourceReceipts(settings, store)
        client = http or ProviderHTTP(Budget(store, settings.config['budgets']))
        try:
            with capture_lease(settings, store, job, grant) as check:
                cp = deepcopy(job['result']['capture'])
                def save():
                    store.checkpoint(job, 'oneoff_capture_recorded', {**job['result'], 'capture': cp})
                def get(kind, path, params, bound):
                    check()
                    limit = value['max_'+kind+'_requests']
                    if sum(item['kind'] == kind for item in cp['requests']) >= limit:
                        fail('ONEOFF_CAPTURE_REQUEST_BOUND_REACHED')
                    request = {'id': str(uuid.uuid4()), 'kind': kind, 'path': path, 'params': params,
                        'state': 'reserved_result_unknown', 'reserved_at': time.time()}
                    cp['requests'].append(request);save()
                    token = os.getenv('X_BEARER_TOKEN')
                    if not token:
                        fail('X_CREDENTIAL_REQUIRED')
                    body, snapshot = client.request_recorded('x', 'GET', 'https://api.x.com'+path,
                        job_id=job['job_id'], headers={'Authorization': 'Bearer '+token}, params=params, reserved_cost_usd=bound)
                    check();call_id = receipts.record(job, snapshot)
                    request.update(state='recorded', call_id=call_id, raw_sha256=hashlib.sha256(snapshot['raw_body']).hexdigest())
                    save()
                    return body, call_id
                if cp['user_call_id']:
                    user, user_body = checked_receipt(receipts, cp['user_call_id']);author = _author(user_body.get('data'), HANDLE)
                else:
                    cache = receipts.cached_author(HANDLE)
                    if cache:
                        user, user_body = checked_receipt(receipts, cache['user_call_id']);author = _author(user_body.get('data'), HANDLE)
                        cp['user_call_id'] = cache['user_call_id'];save()
                    else:
                        user_body, cp['user_call_id'] = get('user', '/2/users/by/username/'+HANDLE, {'user.fields': USER_FIELDS}, .010)
                        author = _author(user_body.get('data'), HANDLE);save()
                if author['id'] != AUTHOR_ID:
                    fail('ONEOFF_CAPTURE_AUTHOR_IDENTITY_MISMATCH')
                found = {}
                next_token = None
                for call_id in cp['timeline_call_ids']:
                    row, body = checked_receipt(receipts, call_id)
                    if row['discovery_job_id'] != job['job_id'] or row['path'] != '/2/users/'+AUTHOR_ID+'/tweets':
                        fail('ONEOFF_CAPTURE_TIMELINE_OWNER_CHANGED')
                    for post in body.get('data', []):
                        if isinstance(post, dict) and post.get('id') in TARGETS:
                            target_post(post);found[post['id']] = (post, call_id)
                    next_token = body.get('meta', {}).get('next_token')
                while set(found) != set(TARGETS):
                    params = {'max_results': 10, 'tweet.fields': TWEET_FIELDS, 'expansions': 'attachments.media_keys',
                        'media.fields': MEDIA_FIELDS, 'user.fields': USER_FIELDS, 'start_time': START_TIME,
                        'since_id': str(min(map(int, TARGETS))-1)}
                    if next_token:
                        if not isinstance(next_token, str) or len(next_token) > 2048:
                            fail('X_PAGINATION_TOKEN_INVALID')
                        params['pagination_token'] = next_token
                    body, call_id = get('timeline', '/2/users/'+AUTHOR_ID+'/tweets', params, .050)
                    if body.get('errors') or not isinstance(body.get('data', []), list):
                        fail('X_SOURCE_RESPONSE_ERRORS')
                    cp['timeline_call_ids'].append(call_id);save()
                    for post in body.get('data', []):
                        if isinstance(post, dict) and post.get('id') in TARGETS:
                            target_post(post);found[post['id']] = (post, call_id)
                    next_token = body.get('meta', {}).get('next_token')
                    if set(found) == set(TARGETS):
                        break
                    if not next_token:
                        fail('ONEOFF_CAPTURE_TARGET_NOT_IN_BOUNDED_TIMELINE')
                cp['target_timeline_call_ids'] = {ident: found[ident][1] for ident in sorted(TARGETS)};save()
                for post_id in DETAIL_IDS:
                    if post_id in cp['detail_call_ids']:
                        row, body = checked_receipt(receipts, cp['detail_call_ids'][post_id])
                    else:
                        body, call_id = get('detail', '/2/tweets/'+post_id, {'tweet.fields': TWEET_FIELDS,
                            'expansions': 'attachments.media_keys,author_id', 'media.fields': MEDIA_FIELDS, 'user.fields': USER_FIELDS}, .015)
                        cp['detail_call_ids'][post_id] = call_id;save()
                    if body.get('errors') or not isinstance(body.get('data'), dict) or body['data'].get('id') != post_id:
                        fail('ONEOFF_CAPTURE_REFERENCE_DETAIL_INVALID')
                check()
                all_ids = {cp['user_call_id'], *cp['timeline_call_ids'], *cp['detail_call_ids'].values()}
                rows = {ident: checked_receipt(receipts, ident)[0] for ident in all_ids}
                used_ids = {cp['user_call_id'], *cp['target_timeline_call_ids'].values(), *cp['detail_call_ids'].values()}
                descriptor = {'version': VERSION, 'capture_job_id': job['job_id'], 'grant': grant_ref,
                    'source_ids': sorted(TARGETS), 'target_site_uid': SITE_UID, 'author_id': AUTHOR_ID, 'author_handle': HANDLE,
                    'user_call_id': cp['user_call_id'], 'detail_call_ids': cp['detail_call_ids'],
                    'targets': {ident: {'original_post': found[ident][0], 'timeline_call_id': found[ident][1],
                        'detail_call_ids': [cp['detail_call_ids'][pid] for kind, pid in TARGETS[ident]]} for ident in sorted(TARGETS)},
                    'receipt_raw_sha256': {ident: rows[ident]['raw_sha256'] for ident in sorted(used_ids)},
                    'all_recorded_call_ids': sorted(all_ids), 'actual_http_attempts': len(cp['requests']),
                    'all_transport_default_tls': all(row['transport_mode'] == 'default_tls' for row in rows.values()),
                    'normal_cursors_before': job['payload']['normal_cursors'], 'normal_cursors_after': normal_cursors(store),
                    'created_at': time.time(), 'human_review': False, 'publication_authorized': False,
                    'models_called': 0, 'publications_created': 0}
                if descriptor['normal_cursors_before'] != descriptor['normal_cursors_after']:
                    fail('ONEOFF_CAPTURE_NORMAL_CURSOR_CHANGED')
                ref = write_new(settings.root/'media'/job['job_id']/'owner-two-reference-capture-v2'/str(uuid.uuid4())/'capture-descriptor.json', descriptor)
                validate_descriptor(settings, store, ref, grant_ref=grant_ref)
                store.finish(job, 'upload_pending', result={**job['result'], 'capture_descriptor': ref}, stage='owner_two_sources_captured')
                return {'version': VERSION, 'state': 'sources_captured_no_import_or_publication', 'capture_job_id': job['job_id'],
                    'capture_descriptor': ref, 'http_attempts': len(cp['requests']),
                    'all_transport_default_tls': descriptor['all_transport_default_tls'], 'normal_cursors_unchanged': True,
                    'models_called': 0, 'publications_created': 0, 'human_review': False}
        except BaseException as exc:
            current = store.get(job['job_id'])
            if current and current['state'] == 'processing' and current['lease_owner'] == job['lease_owner']:
                code = exc.code if isinstance(exc, HubError) else 'ONEOFF_CAPTURE_INTERRUPTED'
                store.finish(job, 'failed', code=code, result=current['result'], stage=current['stage'])
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('plan', 'capture', 'verify'), default='plan')
    parser.add_argument('--grant-file');parser.add_argument('--grant-sha256')
    parser.add_argument('--descriptor-file');parser.add_argument('--descriptor-sha256');parser.add_argument('--descriptor-bytes', type=int)
    args = parser.parse_args()
    if os.name != 'posix' or os.getuid() != 10001:
        fail('ONEOFF_CAPTURE_APP_UID_REQUIRED')
    settings = Settings.load();store = Store(settings.db_path)
    if args.mode == 'plan':
        result = inspect(settings, store)
    else:
        if not args.grant_file or not args.grant_sha256:
            parser.error('root grant path and exact SHA are required')
        grant = RootGrant(settings, args.grant_file, args.grant_sha256)
        if args.mode == 'capture':
            result = capture(settings, store, grant)
        else:
            if not args.descriptor_file or not args.descriptor_sha256 or not args.descriptor_bytes:
                parser.error('all descriptor fields are required')
            value = validate_descriptor(settings, store, {'file': args.descriptor_file, 'sha256': args.descriptor_sha256,
                'bytes': args.descriptor_bytes}, grant_ref=grant.ref)
            result = {'version': VERSION, 'state': 'actual_receipts_reverified', 'capture_job_id': value['capture_job_id'],
                'source_ids': value['source_ids'], 'human_review': False, 'models_called': 0, 'publications_created': 0}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except HubError as exc:
        print(json.dumps({'version': VERSION, 'state': 'stopped', 'error_code': exc.code, 'automatic_retry': False}))
        raise SystemExit(2) from None
    except Exception as exc:
        print(json.dumps({'version': VERSION, 'state': 'stopped_reconcile_before_retry', 'error_class': type(exc).__name__, 'automatic_retry': False}))
        raise SystemExit(2) from None
