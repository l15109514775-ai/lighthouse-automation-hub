"""Held import, real models/render and one-use publishing for two authorized sources.

The raw-capture script is separately hash-pinned. No live hub file is changed.
"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
import types
import uuid

from adapters.http import ProviderHTTP
from adapters.x import build_official_record
from services.hub.budget import Budget
from services.hub.config import Settings
from services.hub.db import Store, encode
from services.hub.errors import HubError, PublishUnknown
from services.hub.evidence import sha256_file
from services.hub.locks import resource_guard
from services.hub.outcomes import JobOutcome
from services.hub.source_receipts import SourceReceipts, digest

VERSION = 'owner-two-reference-backfill-operator-v2'
POLICY = 'machine-tweet-v4-api-relations'
SOURCE_VERSION = 'x-official-source-v2'
CAPTURE_SHA = '9fba632ee6e0ac02bf88a73e24c24c8d971f20285fecbacd0865f42c33ad203e'
POLICY_RAW_SHA = 'c056b1b0d6c3232f431539e4585c130eb1b16419ca0c34543a58bd4a67220941'
POLICY_FILE = 'machine-tweet-policy-api-relations-v1.json'
AVATAR_CONFIG_KEY = 'owner_reference_verified_avatar_fetch'
DEFAULT_AVATAR_URL = 'https://abs.twimg.com/sticky/default_profile_images/default_profile_normal.png'
AVATAR_CONFIG = {'version': 'verified-official-default-avatar-v1', 'url': DEFAULT_AVATAR_URL,
    'host': 'abs.twimg.com', 'purpose': 'avatar_only', 'require_verified_api_node': True}


def fail(code):
    raise HubError(code)


def load_capture(file):
    file = Path(file)
    if not file.is_absolute() or file.resolve() != file or any(p.is_symlink() for p in (file, *file.parents)):
        fail('ONEOFF_REFERENCE_CAPTURE_CODE_PATH_INVALID')
    raw = file.read_bytes()
    if hashlib.sha256(raw).hexdigest() != CAPTURE_SHA:
        fail('ONEOFF_REFERENCE_CAPTURE_CODE_HASH_CHANGED')
    module = types.ModuleType('pinned_two_source_capture_v2');module.__file__ = str(file)
    exec(compile(raw, str(file), 'exec'), module.__dict__)
    return module


def scoped_settings(live, support):
    if sha256_file(live.root/'config/hub.toml') != support.LIVE_HUB_SHA:
        fail('ONEOFF_REFERENCE_LIVE_HUB_CHANGED')
    value = Settings(live.root, deepcopy(live.config), live.dry_run)
    value.config['policy_version'] = POLICY
    value.config[AVATAR_CONFIG_KEY] = deepcopy(AVATAR_CONFIG)
    if {key for key in set(value.config)|set(live.config) if value.config.get(key) != live.config.get(key)} - {'policy_version', AVATAR_CONFIG_KEY}:
        fail('ONEOFF_REFERENCE_SETTINGS_SCOPE_EXPANDED')
    return value


class Scope:
    def __init__(self, live, store, support, grant, descriptor_ref, *, historical_lookup=False):
        self.live, self.store, self.support, self.grant = live, store, support, grant
        self.settings = scoped_settings(live, support)
        self.descriptor_ref = descriptor_ref
        if historical_lookup:
            file = Path(descriptor_ref['file']);raw = file.read_bytes()
            if (file.resolve() != file or any(p.is_symlink() for p in (file, *file.parents)) or
                    not file.is_relative_to(live.root/'media') or len(raw) != descriptor_ref['bytes'] or
                    hashlib.sha256(raw).hexdigest() != descriptor_ref['sha256']):
                fail('ONEOFF_REFERENCE_HISTORICAL_DESCRIPTOR_CHANGED')
            value = support.strict_json(raw);owner = store.get(value['capture_job_id'])
            if (value['version'] != support.VERSION or value['source_ids'] != sorted(support.TARGETS) or value['grant'] != grant.ref or
                    not owner or owner['result'].get('capture_descriptor') != descriptor_ref or
                    owner['payload']['grant'] != grant.ref or owner['policy_version'] != support.VERSION):
                fail('ONEOFF_REFERENCE_HISTORICAL_DESCRIPTOR_CHANGED')
            self.descriptor = value
            # Actual inherited machine _snapshot reopens every bound raw/file
            # with require_fresh=False for an already reserved lookup. No new
            # source/model/dispatch permission is obtained from this branch.
        else:
            self.descriptor = support.validate_descriptor(live, store, descriptor_ref, grant_ref=grant.ref)
        self.capture_job_id = self.descriptor['capture_job_id']
        self.queue_depth = 0
        self.session_job = None
        self.worker_context_verified = False
        self.check(require_live=not historical_lookup)

    def check(self, *, require_live=True):
        value, ref = self.grant.check(require_live=require_live)
        if require_live:
            self.support.require_not_cleared(self.store)
        if self.session_job is not None:
            current = self.store.get(self.session_job['job_id'])
            if (not current or current['state'] != 'processing' or current['lease_owner'] != self.session_job['lease_owner'] or
                    (current['lease_until'] or 0) <= time.time()):
                fail('ONEOFF_REFERENCE_SESSION_LEASE_LOST')
        if sha256_file(self.live.root/'config/hub.toml') != self.support.LIVE_HUB_SHA:
            fail('ONEOFF_REFERENCE_LIVE_HUB_CHANGED')
        policy_file = self.live.root/'config'/POLICY_FILE
        if (policy_file.resolve() != policy_file or any(p.is_symlink() for p in (policy_file, *policy_file.parents)) or
                sha256_file(policy_file) != POLICY_RAW_SHA):
            fail('ONEOFF_REFERENCE_POLICY_HASH_CHANGED')
        for file in (policy_file, *policy_file.parents):
            stat = file.stat()
            if stat.st_uid != 0 or stat.st_mode & 0o022:
                fail('ONEOFF_REFERENCE_POLICY_NOT_ROOT_PROTECTED')
        return value, ref

    def payload(self, payload, *, require_live=True):
        self.check(require_live=require_live)
        self.support.target_post(payload['post'])
        ident = payload['post']['id'];original = self.descriptor['targets'][ident]['original_post']
        if (payload['author']['id'] != self.support.AUTHOR_ID or payload['author']['username'] != self.support.HANDLE or
                payload['url'] != 'https://x.com/'+self.support.HANDLE+'/status/'+ident or
                payload['post']['created_at'] != original['created_at'] or payload.get('source_api') != 'x-v2-official' or
                payload.get('official_source', {}).get('version') != SOURCE_VERSION):
            fail('ONEOFF_REFERENCE_ORIGINAL_SOURCE_CHANGED')
        return ident

    def job_policy(self):
        generation = self.settings.config.get('machine_source_generation', 1)
        if type(generation) is not int or not 1 <= generation <= 1000:
            fail('MACHINE_SOURCE_GENERATION_INVALID')
        return POLICY if generation == 1 else POLICY+':source-'+str(generation)


@contextmanager
def tweet_session(scope):
    if scope.queue_depth:
        scope.queue_depth += 1
        try:
            yield
        finally:
            scope.queue_depth -= 1
    else:
        with resource_guard(scope.settings.root/'data/locks/tweet'):
            scope.queue_depth = 1
            try:
                yield
            finally:
                scope.queue_depth = 0


def bounded_local_resource_wait(scope, operation):
    while True:
        try:
            return operation()
        except HubError as exc:
            if exc.code != 'HEAVY_RESOURCE_BUSY':
                raise
            scope.check()
            time.sleep(1)


@contextmanager
def bounded_heavy(scope):
    acquired = False
    while True:
        try:
            with resource_guard(scope.settings.root/'data/locks/heavy'):
                acquired = True
                yield
                return
        except HubError as exc:
            if acquired or exc.code != 'HEAVY_RESOURCE_BUSY':
                raise
            scope.check();time.sleep(1)


def avatar_download_hosts(settings, job, source, render_folder, url, destination):
    """Exact verified API avatar only; never widen the general media host list."""
    if settings.config.get(AVATAR_CONFIG_KEY) != AVATAR_CONFIG or url != DEFAULT_AVATAR_URL:
        fail('ONEOFF_REFERENCE_DEFAULT_AVATAR_URL_FORBIDDEN')
    destination = Path(destination)
    role = destination.name[:-len('-avatar.bin')] if destination.name.endswith('-avatar.bin') else None
    if (job['source_platform'] != 'x' or job['source_id'] not in ('2116836170986664205', '2116863407962108290') or
            role not in ('post', 'parent', 'quote') or
            destination.parent.parent != settings.root/'media'/job['job_id']/render_folder or
            destination.resolve() != destination or not re.fullmatch('[a-f0-9-]{36}', destination.parent.name) or
            any(p.is_symlink() for p in (destination, *destination.parents)) or
            source.get('proof', {}).get('source_live_verified') is not True):
        fail('ONEOFF_REFERENCE_DEFAULT_AVATAR_CONTEXT_INVALID')
    nodes = [n for n in source['nodes'] if n['role'] == role]
    if len(nodes) != 1:
        fail('ONEOFF_REFERENCE_DEFAULT_AVATAR_CONTEXT_INVALID')
    node = nodes[0]['source']
    if (node.get('source_api') != 'x-v2-official' or node.get('official_source', {}).get('version') != SOURCE_VERSION or
            node.get('capture', {}).get('official_api_receipt') is not True or
            node['author'].get('profile_image_url') != DEFAULT_AVATAR_URL):
        fail('ONEOFF_REFERENCE_DEFAULT_AVATAR_NOT_API_BOUND')
    before = list(settings.config['allowed_media_hosts'])
    if 'abs.twimg.com' in before:
        fail('ONEOFF_REFERENCE_GLOBAL_AVATAR_HOST_MUST_REMAIN_CLOSED')
    return [*before, 'abs.twimg.com'], node['author']


@contextmanager
def exact_verified_avatar_fetch(service, scope, job, source):
    from services.hub import machine_tweets_v2 as implementation
    # Reconstruct the real API/source receipt graph now. An injected node or a
    # config fallback cannot acquire this per-avatar permission.
    if source != service.source(job, require_fresh=True):
        fail('MACHINE_V2_SOURCE_CHANGED')
    actual = implementation.download
    def download(url, destination, **kwargs):
        from urllib.parse import urlsplit
        if urlsplit(url).hostname == 'abs.twimg.com':
            hosts, author = avatar_download_hosts(service.settings, job, source, service.render_folder, url, destination)
            if list(kwargs.get('allowed_hosts', [])) != list(service.settings.config['allowed_media_hosts']):
                fail('ONEOFF_REFERENCE_AVATAR_CALL_HOSTS_CHANGED')
            scope.check()
            scope.store.audit('owner-reference-backfill-v2', 'allow_exact_verified_default_avatar', job['job_id'], encode({
                'configuration': AVATAR_CONFIG, 'author_id': author['id'], 'author_handle': author['username'],
                'avatar_file': str(destination), 'original_api_avatar_url': author['profile_image_url'],
                'allowed_hosts_before': list(service.settings.config['allowed_media_hosts']), 'allowed_hosts_for_this_avatar': hosts,
                'grant': scope.grant.ref, 'descriptor': scope.descriptor_ref, 'live_hub_changed': False, 'human_review': False}))
            kwargs = {**kwargs, 'allowed_hosts': hosts}
        return actual(url, destination, **kwargs)
    implementation.download = download
    try:
        yield
    finally:
        implementation.download = actual


class AgeScopedReceipts(SourceReceipts):
    def __init__(self, scope):
        super().__init__(scope.settings, scope.store);self.scope = scope

    def _official_publication_window(self, discovery, payload, not_before=None):
        if discovery['job_id'] != self.scope.capture_job_id:
            fail('ONEOFF_REFERENCE_CAPTURE_OWNER_CHANGED')
        self.scope.payload(payload)
        # Only the original post-age window is excepted. The ordinary parent
        # lease, current raw receipts, rights, tombstones and global dedup remain.

    def _official_initial_job_state(self, discovery, payload):
        self._official_publication_window(discovery, payload)
        return 'upload_pending'


@contextmanager
def held_claim(scope, job_id, *, expected_type, allowed_states):
    """A real targeted lease, never a queue-wide claim/recover or attempt reset."""
    store = scope.store;scope.check()
    now = time.time();owner = str(uuid.uuid4());lease = scope.settings.config['lease_seconds']
    with store.transaction() as db:
        row = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if (not row or row['job_type'] != expected_type or row['queue'] != 'tweet' or row['state'] not in allowed_states or
                row['attempts'] >= scope.settings.config['max_attempts']):
            fail('ONEOFF_REFERENCE_CLAIM_STATE_OR_ATTEMPTS_INVALID')
        if row['state'] == 'processing':
            if (not row['lease_owner'] or type(row['lease_until']) not in (int, float) or row['lease_until'] > now):
                fail('ONEOFF_REFERENCE_ACTIVE_LEASE_CANNOT_BE_REPLACED')
        elif row['lease_owner'] is not None or row['lease_until'] is not None:
            fail('ONEOFF_REFERENCE_UNEXPECTED_EXISTING_LEASE')
        if expected_type == 'owner_reference_backfill_session':
            payload = json.loads(row['payload'])
            if (row['source_platform'] != 'owner_backfill' or row['policy_version'] != VERSION or
                    payload.get('grant') != scope.grant.ref or payload.get('descriptor') != scope.descriptor_ref):
                fail('ONEOFF_REFERENCE_SESSION_BINDING_CHANGED')
        elif expected_type == 'x_discovery':
            if row['source_platform'] != 'owner_backfill' or row['policy_version'] != scope.support.VERSION or job_id != scope.capture_job_id:
                fail('ONEOFF_REFERENCE_CAPTURE_OWNER_CHANGED')
        else:
            payload = json.loads(row['payload']);ident = scope.payload(payload)
            if row['source_platform'] != 'x' or row['source_id'] != ident or row['policy_version'] != scope.job_policy():
                fail('ONEOFF_REFERENCE_PARENT_CHANGED')
            if db.execute("SELECT 1 FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=? AND account_id=?",
                    (ident, 'site_uid:'+scope.support.SITE_UID)).fetchone():
                fail('ONEOFF_REFERENCE_SOURCE_ALREADY_RESERVED')
        changed = db.execute("UPDATE jobs SET state='processing',attempts=attempts+1,error_code=NULL,lease_owner=?,lease_until=?,updated_at=? WHERE job_id=? AND state=? AND updated_at=?",
            (owner, now+lease, now, job_id, row['state'], row['updated_at'])).rowcount
        if changed != 1:
            fail('ONEOFF_REFERENCE_CLAIM_CAS_REJECTED')
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now, 'owner-reference-backfill-v2', 'claim_targeted_reference_phase', job_id,
             encode({'grant': scope.grant.ref, 'descriptor': scope.descriptor_ref, 'previous_state': row['state'],
                'previous_error_code': row['error_code'], 'previous_attempts': row['attempts'], 'attempts_reset': False, 'human_review': False})))
    job = store.get(job_id);stop = threading.Event()
    def heartbeat():
        while not stop.wait(min(30, lease/3)):
            if not store.heartbeat(job, lease):
                return
            if scope.worker_context_verified and scope.queue_depth:
                store.beat('worker:tweet')
    thread = threading.Thread(target=heartbeat, daemon=True);thread.start()
    try:
        yield job
    except BaseException as exc:
        current = store.get(job_id)
        if current and current['state'] == 'processing' and current['lease_owner'] == owner:
            code = exc.code if isinstance(exc, HubError) else 'ONEOFF_REFERENCE_PHASE_INTERRUPTED'
            store.finish(job, 'failed', code=code, result=current['result'], stage=current['stage'])
        raise
    finally:
        stop.set();thread.join(timeout=2)


def actual_records(scope):
    support, receipts = scope.support, SourceReceipts(scope.settings, scope.store)
    scope.support.validate_descriptor(scope.live, scope.store, scope.descriptor_ref, grant_ref=scope.grant.ref)
    d = scope.descriptor;ids = {d['user_call_id'], *d['detail_call_ids'].values(), *[v['timeline_call_id'] for v in d['targets'].values()]}
    rows, bodies = {}, {}
    for ident in ids:
        rows[ident], bodies[ident] = support.checked_receipt(receipts, ident)
    if not all(r['transport_mode'] == 'default_tls' for r in rows.values()):
        fail('ONEOFF_REFERENCE_LIVE_OFFICIAL_RECEIPTS_REQUIRED')
    metadata = {ident: {'captured_at': row['captured_at'], 'transport_mode': row['transport_mode']} for ident, row in rows.items()}
    records = {}
    for source_id in sorted(support.TARGETS):
        entry = d['targets'][source_id]
        refs = {'user_call_id': d['user_call_id'], 'timeline_call_id': entry['timeline_call_id'], 'detail_call_ids': entry['detail_call_ids']}
        records[source_id] = build_official_record(support.HANDLE, source_id, bodies[d['user_call_id']], bodies[entry['timeline_call_id']],
            {ident: bodies[ident] for ident in entry['detail_call_ids']}, refs, metadata, source_version=SOURCE_VERSION)
        scope.payload(records[source_id]['payload'])
    return records


def ingest(scope):
    with tweet_session(scope):
        records = actual_records(scope);store = scope.store
        if not hasattr(SourceReceipts, '_official_initial_job_state') or not hasattr(SourceReceipts, '_official_publication_window'):
            fail('ONEOFF_REFERENCE_SOURCE_V2_RUNTIME_REQUIRED')
        capture_job = store.get(scope.capture_job_id)
        if set(capture_job['result'].get('oneoff_imported_parent_ids', {})) == set(scope.support.TARGETS):
            ids = capture_job['result']['oneoff_imported_parent_ids']
            for source_id, ident in ids.items():
                job = store.get(ident)
                if scope.payload(job['payload']) != source_id or job['policy_version'] != scope.job_policy():
                    fail('ONEOFF_REFERENCE_IMPORTED_PARENT_CHANGED')
                AgeScopedReceipts(scope).assess_official(job, require_fresh=True, source_version=SOURCE_VERSION)
            return {'version': VERSION, 'state': 'existing_verified_imports', 'parent_job_ids': ids,
                'models_called': 0, 'publications_created': 0, 'live_hub_changed': False}
        with held_claim(scope, scope.capture_job_id, expected_type='x_discovery', allowed_states=('upload_pending', 'failed', 'processing')) as discovery:
            receipts = AgeScopedReceipts(scope);parent_ids = dict(discovery['result'].get('oneoff_imported_parent_ids', {}))
            for source_id, record in records.items():
                result = bounded_local_resource_wait(scope, lambda: receipts.ingest_official(discovery, record, scope.job_policy()))
                job = store.get(result['job_id'])
                if (scope.payload(job['payload']) != source_id or job['policy_version'] != scope.job_policy() or
                        job['state'] != 'upload_pending' or job['lease_owner'] is not None):
                    fail('ONEOFF_REFERENCE_IMPORTED_PARENT_NOT_HELD')
                receipts.assess_official(job, require_fresh=True, source_version=SOURCE_VERSION)
                marker = {'version': VERSION, 'grant': scope.grant.ref, 'descriptor': scope.descriptor_ref,
                    'capture_job_id': scope.capture_job_id, 'original_created_at': job['payload']['post']['created_at'],
                    'age_exception_only': True, 'human_review': False, 'publication_authorized': False}
                with store.transaction() as db:
                    row = db.execute('SELECT state,result,payload,policy_version FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
                    if row['state'] != 'upload_pending' or json.loads(row['payload']) != job['payload'] or row['policy_version'] != scope.job_policy():
                        fail('ONEOFF_REFERENCE_IMPORTED_PARENT_CAS_REJECTED')
                    previous = json.loads(row['result']);old_marker = previous.get('owner_reference_backfill')
                    if old_marker is not None and old_marker != marker:
                        fail('ONEOFF_REFERENCE_PARENT_GRANT_CHANGED')
                    db.execute("UPDATE jobs SET result=?,stage='owner_reference_source_imported',updated_at=? WHERE job_id=? AND state='upload_pending'",
                        (encode({**previous, 'owner_reference_backfill': marker}), time.time(), job['job_id']))
                parent_ids[source_id] = job['job_id']
                store.checkpoint(discovery, 'owner_reference_import_recorded', {**discovery['result'], 'oneoff_imported_parent_ids': parent_ids})
            store.finish(discovery, 'upload_pending', result=discovery['result'], stage='owner_reference_sources_imported')
        return {'version': VERSION, 'state': 'real_v2_sources_imported_held', 'parent_job_ids': parent_ids,
            'models_called': 0, 'publications_created': 0, 'live_hub_changed': False}


def machine(scope):
    from services.hub.machine_tweets_api import MachineTweetsAPIReference
    class ScopedMachine(MachineTweetsAPIReference):
        def policy(self):
            scope.check(require_live=False)
            return super().policy()
        def fresh(self, job, *, now=None):
            scope.payload(job['payload'])
            if job['source_platform'] != 'x' or job['job_type'] != 'tweet_process' or job['policy_version'] != scope.job_policy():
                fail('ONEOFF_REFERENCE_PARENT_CHANGED')
            self._require_current_generation(job);self.require_source_not_cleared(job)
            return self.policy()
        def render_evidence(self, job, source, entries):
            with exact_verified_avatar_fetch(self, scope, job, source):
                built = super().render_evidence(job, source, entries)
            built['caption'] = '【补发】'+built['caption']
            return built
        def resume_prepared(self, job):
            self.fresh(job);current, manifest = self._snapshot(job['job_id'])
            result = {**current['result'], 'owner_reference_prepared': {'version': VERSION, 'grant': scope.grant.ref,
                'descriptor': scope.descriptor_ref, 'original_created_at': current['payload']['post']['created_at'],
                'manifest_sha256': current['result']['machine_web']['manifest_sha256'], 'human_review': False,
                'publication_authorized': False, 'normal_publish_child_enqueued': False}}
            self.store.checkpoint(job, 'owner_reference_render_ready', result)
            return JobOutcome('upload_pending', result)
        def reserve(self, principal, job_id, *, site_uid, artifact_sha256, caption_sha256):
            job = self.store.get(job_id);ident = scope.payload(job['payload'])
            if site_uid != scope.support.SITE_UID:
                fail('ONEOFF_REFERENCE_TARGET_UID_CHANGED')
            result = super().reserve(principal, job_id, site_uid=site_uid, artifact_sha256=artifact_sha256, caption_sha256=caption_sha256)
            if result['can_click_publish']:
                self.store.audit('owner-reference-backfill-v2', 'consume_source_publication_grant', job_id,
                    encode({'source_id': ident, 'grant': scope.grant.ref, 'descriptor': scope.descriptor_ref,
                        'idempotency_key': result['idempotency_key'], 'human_review': False, 'normal_policy_age_seconds_unchanged': 7200}))
            return result
    return ScopedMachine(scope.settings, scope.store)


def parent_job(scope, source_id):
    if source_id not in scope.support.TARGETS:
        fail('ONEOFF_REFERENCE_SOURCE_OUTSIDE_GRANT')
    capture_job = scope.store.get(scope.capture_job_id)
    ident = capture_job['result'].get('oneoff_imported_parent_ids', {}).get(source_id)
    job = scope.store.get(ident) if ident else None
    if (not job or scope.payload(job['payload'], require_live=False) != source_id or job['policy_version'] != scope.job_policy() or
            job['result'].get('owner_reference_backfill', {}).get('grant') != scope.grant.ref):
        fail('ONEOFF_REFERENCE_IMPORTED_PARENT_REQUIRED')
    return job


def prepare(scope, source_id):
    from adapters.model import ModelAPI
    with tweet_session(scope):
        service = machine(scope);job = parent_job(scope, source_id)
        existing = scope.store.query("SELECT * FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=? AND account_id=?",
            (source_id, 'site_uid:'+scope.support.SITE_UID))
        if existing:
            if (len(existing) != 1 or existing[0]['job_id'] != job['job_id'] or existing[0]['kind'] != service.kind or
                    existing[0]['state'] == 'rejected'):
                fail('ONEOFF_REFERENCE_SOURCE_ALREADY_RESERVED')
            _, manifest = service._snapshot(job['job_id'])
            return {'version': VERSION, 'state': 'existing_publication_evidence_no_new_models', 'job_id': job['job_id'],
                'manifest': job['result']['machine_web'], 'artifact': manifest['render'], 'caption': manifest['caption'],
                'publication': {key: existing[0][key] for key in ('state', 'external_id', 'external_url')},
                'paid_model_calls_started_by_resume': 0, 'human_review': False, 'publication_authorized': False}
        service.fresh(job)
        if job['state'] == 'upload_pending' and job['result'].get('machine_web'):
            _, manifest = service._snapshot(job['job_id'])
            return {'version': VERSION, 'state': 'existing_verified_render', 'job_id': job['job_id'],
                'manifest': job['result']['machine_web'], 'artifact': manifest['render'], 'caption': manifest['caption'],
                'paid_model_calls_started_by_resume': 0, 'human_review': False, 'publication_authorized': False}
        with held_claim(scope, job['job_id'], expected_type='tweet_process', allowed_states=('upload_pending', 'failed', 'processing')) as claimed:
            if claimed['result'].get('machine_web'):
                service._snapshot(claimed['job_id'])
                outcome = service.resume_prepared(claimed)
            else:
                source = service.source(claimed, require_fresh=True)
                if [n['role'] for n in source['nodes']] not in (['post', 'quote'], ['post', 'parent', 'quote']):
                    fail('ONEOFF_REFERENCE_EXPECTED_TYPED_SOURCE_REQUIRED')
                model = ModelAPI(ProviderHTTP(Budget(scope.store, scope.settings.config['budgets'])), scope.store, settings=scope.settings)
                entries = {node['role']: service.resolve_node(claimed, node, model) for node in source['nodes']}
                outcome = bounded_local_resource_wait(scope, lambda: service.prepare_extended(claimed, source, entries))
            if not isinstance(outcome, JobOutcome) or outcome.state != 'upload_pending':
                fail('ONEOFF_REFERENCE_RENDER_MUST_REMAIN_HELD')
            scope.store.finish(claimed, 'upload_pending', result=outcome.result, stage='owner_reference_prepared')
        job = scope.store.get(job['job_id']);_, manifest = service._snapshot(job['job_id'])
        return {'version': VERSION, 'state': 'real_models_checked_render_held', 'job_id': job['job_id'], 'attempts': job['attempts'],
            'manifest': job['result']['machine_web'], 'artifact': manifest['render'], 'caption': manifest['caption'],
            'roles': list(manifest['models']), 'human_review': False, 'publication_authorized': False, 'live_hub_changed': False}


def dispatch(scope, source_id, *, lookup_only=False):
    """Same proven native coordinator; the real two-source grant supplies deadline."""
    with tweet_session(scope):
        service = machine(scope);job = parent_job(scope, source_id)
        with bounded_heavy(scope):
            job, manifest = service._snapshot(job['job_id']);grant, grant_ref = scope.check(require_live=False)
            rows = scope.store.query("SELECT * FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=? AND account_id=?",
                (source_id, 'site_uid:'+scope.support.SITE_UID))
            if rows and (len(rows) != 1 or rows[0]['job_id'] != job['job_id'] or rows[0]['kind'] != service.kind or rows[0]['state'] == 'rejected'):
                fail('ONEOFF_REFERENCE_SOURCE_ALREADY_RESERVED')
            if rows and rows[0]['state'] == 'completed':
                return {'version': VERSION, 'state': 'completed', 'external_id': rows[0]['external_id'], 'url': rows[0]['external_url'], 'sent': False}
            if lookup_only and not rows:
                fail('ONEOFF_REFERENCE_LOOKUP_RESERVATION_REQUIRED')
            if not rows:
                service.fresh(job);service.source_check(job, require_fresh=True)
            attempt = str(uuid.uuid4());folder = scope.settings.root/'media'/job['job_id']/'website-attempts'/attempt
            folder.mkdir(parents=True, mode=0o700)
            spec = {'version': 'bilibili-ui-transport-v1', 'job_id': job['job_id'], 'attempt_id': attempt,
                'mode': 'lookup' if rows else 'publish', 'source_url': manifest['source_url'],
                'target_site_uid': manifest['target_site_uid'], 'target_name': manifest['target_name'],
                'file': manifest['render']['png'], 'artifact_sha256': manifest['render']['sha256'], 'caption': manifest['caption'],
                'machine_policy': service.version, 'dispatch_expires_at': grant['expires_at'],
                'owner_age_exception_grant': {'version': VERSION, 'grant': grant_ref, 'descriptor': scope.descriptor_ref,
                    'original_created_at': job['payload']['post']['created_at'], 'human_review': False}}
            scope.support.write_new(folder/'two-source-dispatch-spec.json', spec)
            env = {k: os.environ[k] for k in ('PATH', 'PLAYWRIGHT_BROWSERS_PATH', 'TEMP', 'TMP', 'TMPDIR') if k in os.environ}
            env.update(HUB_ROOT=str(scope.settings.root), BILIBILI_EXPECTED_SITE_UID=scope.support.SITE_UID,
                HUB_BROWSER_CHANNEL=scope.settings.config.get('web_browser_channel', 'chromium'),
                XDG_CONFIG_HOME='/tmp/lighthouse-browser-config', XDG_CACHE_HOME='/tmp/lighthouse-browser-cache')
            proc = subprocess.Popen(['node', str(scope.settings.root/'adapters/browser_publisher.mjs')],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            proc.stdin.write(encode(spec).encode('utf-8'));proc.stdin.close();reserved = bool(rows)
            try:
                deadline = time.monotonic()+18*60;preparation_deadline = time.monotonic()+120
                while time.monotonic() < deadline:
                    result_file = folder/'result.json'
                    if result_file.exists():
                        outcome = scope.support.strict_json(result_file.read_bytes())
                        if outcome['state'] in ('published_visible', 'existing_visible'):
                            file = folder/'visible-result.json';scope.support.write_new(file, outcome['evidence'])
                            if not reserved:
                                permit = service.reserve('machine-policy', job['job_id'], site_uid=spec['target_site_uid'],
                                    artifact_sha256=spec['artifact_sha256'], caption_sha256=manifest['caption_sha256'])
                                if not permit['can_click_publish']:
                                    raise PublishUnknown()
                                reserved = True
                            result = service.record_visible('machine-policy', job['job_id'], file)
                            ref = scope.support.write_new(folder/'owner-two-source-result.json', {'version': VERSION,
                                'grant': grant_ref, 'descriptor': scope.descriptor_ref, 'result': result, 'human_review': False})
                            return {**result, 'owner_backfill_receipt': ref}
                        if reserved:
                            raise PublishUnknown()
                        fail(outcome.get('error_code', 'WEB_PUBLICATION_BLOCKED'))
                    ready = folder/'ready.json'
                    if ready.exists() and not reserved:
                        actual = scope.support.strict_json(ready.read_bytes())
                        if (actual.get('attempt_id') != attempt or actual.get('site_uid') != spec['target_site_uid'] or
                                actual.get('artifact_sha256') != spec['artifact_sha256'] or actual.get('caption_sha256') != manifest['caption_sha256']):
                            fail('ONEOFF_REFERENCE_NATIVE_DRAFT_MISMATCH')
                        permit = service.reserve('machine-policy', job['job_id'], site_uid=spec['target_site_uid'],
                            artifact_sha256=spec['artifact_sha256'], caption_sha256=manifest['caption_sha256'])
                        if not permit['can_click_publish']:
                            raise PublishUnknown()
                        reserved = True;temporary = folder/'commit.partial'
                        scope.support.write_new(temporary, {'action': 'publish', 'attempt_id': attempt, 'artifact_sha256': spec['artifact_sha256']})
                        temporary.replace(folder/'commit.json')
                        from services.hub.web_source import WebSources
                        WebSources._sync_dir(folder)
                    if proc.poll() is not None:
                        if reserved:
                            raise PublishUnknown()
                        fail('WEB_BROWSER_EXITED_BEFORE_DISPATCH')
                    if not ready.exists() and time.monotonic() > preparation_deadline:
                        raise PublishUnknown() if reserved else HubError('WEB_DRAFT_PREPARATION_TIMEOUT', retryable=True)
                    time.sleep(.25)
                raise PublishUnknown() if reserved else HubError('WEB_DRAFT_PREPARATION_TIMEOUT', retryable=True)
            finally:
                if proc.poll() is None:
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.terminate()
                        try:
                            proc.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            proc.kill();proc.wait(timeout=5)


def validate_worker_context(scope, expected_container_id, expected_hostname):
    # Root supplies the actual docker-exec target in the systemd invocation;
    # no Docker socket or management capability is exposed to this app.
    hostname = os.environ.get('HOSTNAME', '')
    if (not re.fullmatch('[a-f0-9]{64}', expected_container_id or '') or
            not re.fullmatch('[A-Za-z0-9][A-Za-z0-9.-]{0,62}', expected_hostname or '') or hostname != expected_hostname or
            Path('/etc/hostname').read_text().strip() != hostname):
        fail('ONEOFF_REFERENCE_ACTUAL_TWEET_CONTAINER_REQUIRED')
    scope.worker_context_verified = True


def wait_for_root_commit(scope, prepared):
    expected = {ident: {'job_id': value['job_id'], 'manifest_sha256': value['manifest']['manifest_sha256'],
        'artifact_sha256': value['artifact']['sha256'], 'caption_sha256': hashlib.sha256(value['caption'].encode()).hexdigest()}
        for ident, value in prepared.items()}
    file = scope.settings.root/'config/oneoff-reference-backfill-v2/commit.json'
    print(json.dumps({'version': VERSION, 'state': 'prepared_waiting_root_technical_acceptance',
        'prepared': prepared, 'expected_commit_prepared': expected, 'commit_file': str(file),
        'grant_sha256': scope.grant.ref['sha256'], 'descriptor_sha256': scope.descriptor_ref['sha256'],
        'session_job_id': scope.session_job['job_id'], 'human_review': False}), flush=True)
    while True:
        grant, _ = scope.check()
        if file.exists():
            if file.resolve() != file or file.stat().st_size > 32768:
                fail('ONEOFF_REFERENCE_COMMIT_PATH_INVALID')
            for item in (file, *file.parents):
                stat = item.stat()
                if item.is_symlink() or stat.st_uid != 0 or stat.st_mode & 0o022:
                    fail('ONEOFF_REFERENCE_COMMIT_NOT_ROOT_PROTECTED')
            raw = file.read_bytes();value = scope.support.strict_json(raw)
            if (set(value) != {'version', 'action', 'grant_sha256', 'descriptor_sha256', 'prepared', 'actual_operator_reference', 'issued_at'} or
                    value['version'] != 'owner-two-reference-root-commit-v2' or value['action'] != 'publish' or
                    value['grant_sha256'] != scope.grant.ref['sha256'] or value['descriptor_sha256'] != scope.descriptor_ref['sha256'] or
                    value['prepared'] != expected or not scope.support.reference_text(value['actual_operator_reference']) or
                    type(value['issued_at']) not in (int, float) or not grant['issued_at'] <= value['issued_at'] <= time.time()+5):
                fail('ONEOFF_REFERENCE_COMMIT_BINDING_INVALID')
            ref = {'file': str(file), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
            scope.store.audit('owner-reference-backfill-v2', 'root_technical_reference_acceptance', scope.session_job['job_id'],
                encode({'commit': ref, 'prepared': expected, 'actual_operator_reference': value['actual_operator_reference'],
                    'human_review': False, 'owner_content_authorization': 'existing_exact_two_source_grant'}))
            return ref
        time.sleep(1)


def run(scope, expected_container_id, expected_hostname):
    validate_worker_context(scope, expected_container_id, expected_hostname)
    with tweet_session(scope):
        grant, _ = scope.check()
        payload = {'version': VERSION, 'grant': scope.grant.ref, 'descriptor': scope.descriptor_ref,
            'source_ids': sorted(scope.support.TARGETS), 'actual_tweet_container_id': expected_container_id,
            'actual_tweet_hostname': expected_hostname, 'human_review': False}
        job_id = scope.store.enqueue('owner_backfill', 'reference-session:'+grant['grant_id'], 'owner_reference_backfill_session', 'tweet', payload, VERSION)
        existing = scope.store.get(job_id)
        if existing['payload'] != payload:
            fail('ONEOFF_REFERENCE_SESSION_BINDING_CHANGED')
        if existing['state'] == 'completed':
            return {'version': VERSION, 'state': 'existing_completed_session', 'result': existing['result'], 'new_publishes': 0}
        with held_claim(scope, job_id, expected_type='owner_reference_backfill_session', allowed_states=('pending', 'failed', 'processing', 'publish_unknown')) as session:
            scope.session_job = session
            try:
                imported = ingest(scope);prepared = {}
                for source_id in sorted(scope.support.TARGETS):
                    prepared[source_id] = prepare(scope, source_id)
                    print(json.dumps({'version': VERSION, 'state': 'one_source_prepared', 'source_id': source_id,
                        'prepared': prepared[source_id], 'human_review': False}), flush=True)
                commit = wait_for_root_commit(scope, prepared)
                published = {};uncertain = []
                for source_id in sorted(scope.support.TARGETS):
                    try:
                        published[source_id] = dispatch(scope, source_id)
                    except PublishUnknown:
                        uncertain.append(source_id)
                        published[source_id] = {'state': 'publish_unknown', 'new_dispatch_for_unknown_forbidden': True,
                            'next_action': 'lookup_only', 'human_review': False}
                result = {'version': VERSION, 'grant': scope.grant.ref, 'descriptor': scope.descriptor_ref,
                    'commit': commit, 'imported': imported, 'prepared': prepared, 'publications': published,
                    'uncertain_source_ids': uncertain, 'human_review': False, 'live_hub_changed': False}
                scope.store.finish(session, 'publish_unknown' if uncertain else 'completed', result=result,
                    code='PUBLISH_UNKNOWN' if uncertain else None,
                    stage='owner_reference_results_require_lookup' if uncertain else 'owner_two_reference_sources_completed')
                return result
            finally:
                scope.session_job = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('plan', 'run', 'lookup'), default='plan')
    parser.add_argument('--capture-operator-file', required=True)
    parser.add_argument('--grant-file', required=True);parser.add_argument('--grant-sha256', required=True)
    parser.add_argument('--descriptor-file', required=True);parser.add_argument('--descriptor-sha256', required=True)
    parser.add_argument('--descriptor-bytes', required=True, type=int);parser.add_argument('--source-id')
    parser.add_argument('--expected-tweet-container-id')
    parser.add_argument('--expected-tweet-hostname')
    args = parser.parse_args()
    if os.name != 'posix' or os.getuid() != 10001:
        fail('ONEOFF_REFERENCE_APP_UID_REQUIRED')
    def interrupted(signum, frame):
        fail('ONEOFF_REFERENCE_SESSION_INTERRUPTED')
    signal.signal(signal.SIGTERM, interrupted);signal.signal(signal.SIGINT, interrupted)
    support = load_capture(args.capture_operator_file);live = Settings.load();store = Store(live.db_path)
    if args.mode in ('lookup', 'plan'):
        grant = support.RootGrant.__new__(support.RootGrant)
        grant.settings, grant.file, grant.expected_sha = live, Path(args.grant_file), args.grant_sha256
        grant.value, grant.ref = grant.check(require_live=False)
    else:
        grant = support.RootGrant(live, args.grant_file, args.grant_sha256)
    descriptor_ref = {'file': args.descriptor_file, 'sha256': args.descriptor_sha256, 'bytes': args.descriptor_bytes}
    scope = Scope(live, store, support, grant, descriptor_ref, historical_lookup=args.mode == 'lookup')
    if args.mode == 'plan':
        result = {'version': VERSION, 'state': 'plan_only', 'capture_job_id': scope.capture_job_id,
            'source_ids': sorted(support.TARGETS), 'scoped_policy': POLICY, 'source_version': SOURCE_VERSION,
            'live_hub_sha256': support.LIVE_HUB_SHA, 'live_hub_changed': False, 'models_called': 0, 'publications_created': 0}
    elif args.mode == 'run':
        result = run(scope, args.expected_tweet_container_id, args.expected_tweet_hostname)
    else:
        if args.source_id not in support.TARGETS:
            parser.error('one of the exact two --source-id values is required')
        result = dispatch(scope, args.source_id, lookup_only=True)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except HubError as exc:
        print(json.dumps({'version': VERSION, 'error_code': exc.code, 'state': 'lookup_required' if isinstance(exc, PublishUnknown) else 'stopped', 'automatic_retry': False}))
        raise SystemExit(2) from None
    except Exception as exc:
        print(json.dumps({'version': VERSION, 'error_class': type(exc).__name__, 'state': 'stopped_reconcile_before_retry', 'automatic_retry': False}))
        raise SystemExit(2) from None
