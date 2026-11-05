"""One owner-authorized old tweet only. Default plan; no policy/attempt reset."""
from copy import deepcopy
from datetime import datetime
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from services.hub.config import Settings
from services.hub.db import Store, encode
from services.hub.errors import HubError, PublishUnknown
from services.hub.evidence import sha256_file
from services.hub.locks import resource_guard
from services.hub.machine_tweets_api import MachineTweetsAPI
from services.hub.source_receipts import digest

VERSION = 'owner-single-tweet-age-exception-v1'
PLAN_VERSION = 'owner-single-tweet-age-plan-v1'
GRANT_VERSION = 'owner-single-tweet-age-grant-v1'
JOB_ID = '55c4ed05-8650-46bb-aefc-a2b0c11323a1'
SOURCE_ID = '2116863357798215887'
SITE_UID = '1000000000000000'
MODEL_JOB_ID = 'c9632433-0c77-443f-9bcb-b43d0c1b6c1e'
OWNER_RESPONSE = '那个超过了2小时限制的旧贴先补发，只有这次这次忽略2小时限制'
CORE_SHA256 = '01b073e7e725ea78c182ea5bc821ad73427f8dbab0bb5b14fc90daa799fb39b4'
SHA = re.compile('[0-9a-f]{64}')
GRANT_KEYS = {'version', 'grant_id', 'job_id', 'source_id', 'target_site_uid', 'owner_response',
    'owner_response_reference', 'issued_at', 'expires_at', 'plan_file', 'plan_sha256',
    'manifest_sha256', 'artifact_sha256', 'caption_sha256', 'job_policy_version',
    'payload_sha256', 'policy_sha256', 'max_dispatches', 'scope', 'human_review'}


def fail(code):
    raise HubError(code)


def row_sha(row):
    return digest(row)


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                fail('ONEOFF_DUPLICATE_JSON_FIELD')
            value[key] = item
        return value
    def invalid(value):
        fail('ONEOFF_JSON_NUMBER_INVALID')
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        fail('ONEOFF_JSON_INVALID')


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _reference(value):
    return isinstance(value, str) and 1 <= len(value.strip()) <= 1000 and '\x00' not in value


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def confined_file(root, file, *, maximum=1000000):
    file = Path(file)
    if (not file.is_absolute() or file.resolve() != file or not file.is_relative_to(root) or
            any(p.is_symlink() for p in (file, *file.parents)) or not file.is_file()):
        fail('ONEOFF_FILE_PATH_INVALID')
    raw = file.read_bytes()
    if not 0 < len(raw) <= maximum:
        fail('ONEOFF_FILE_SIZE_INVALID')
    return raw


def write_new(file, value):
    file = Path(file);file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any(p.is_symlink() for p in (file, *file.parents)):
        fail('ONEOFF_FILE_PATH_INVALID')
    raw = (encode(value)+'\n').encode('utf-8')
    with file.open('xb') as stream:
        os.chmod(file, 0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
    from services.hub.web_source import WebSources
    WebSources._sync_dir(file.parent)
    return {'file': str(file), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def target(settings, store):
    job = store.get(JOB_ID)
    if (not job or job['source_platform'] != 'x' or job['source_id'] != SOURCE_ID or
            job['job_type'] != 'tweet_process' or job['queue'] != 'tweet' or
            job['payload']['post']['id'] != SOURCE_ID or job['payload']['author']['username'] != 'example-channel-b' or
            job['payload']['url'] != 'https://x.com/example-channel-b/status/'+SOURCE_ID or
            settings.config.get('target_bilibili_site_uid') != SITE_UID or
            settings.config.get('policy_version') != MachineTweetsAPI.version):
        fail('ONEOFF_TARGET_MISMATCH')
    return job


def source_publications(store):
    return store.query("SELECT * FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=? AND account_id=?",
        (SOURCE_ID, 'site_uid:'+SITE_UID))


def inspect_target(settings, store):
    job = target(settings, store);service = MachineTweetsAPI(settings, store)
    service._require_current_generation(job);service.require_source_not_cleared(job)
    policy = service.policy()
    rows = source_publications(store)
    return {'version': VERSION, 'state': 'plan_only', 'job_id': JOB_ID, 'source_id': SOURCE_ID,
        'target_site_uid': SITE_UID, 'job_state': job['state'], 'attempts': job['attempts'],
        'error_code': job['error_code'], 'row_sha256': row_sha(job), 'policy_sha256': digest(policy),
        'normal_max_post_age_seconds': policy['max_post_age_seconds'], 'original_created_at': job['payload']['post']['created_at'],
        'publications': [{'kind': r['kind'], 'state': r['state'], 'external_id': r['external_id'], 'url': r['external_url']} for r in rows],
        'root_grant_file': str(settings.root/'config/oneoff-tweet-age-v1/grant.json'),
        'human_review': False, 'publication_authorized': False, 'paid_model_calls': 0}


class ProjectedStore:
    """Only parent get() differs while the real inherited snapshot validates it."""
    def __init__(self, store, projected):
        self.store, self.projected = store, projected
    def get(self, job_id):
        return deepcopy(self.projected) if job_id == JOB_ID else self.store.get(job_id)
    def __getattr__(self, name):
        return getattr(self.store, name)


def prepared_result(job, manifest_ref, manifest, *, grant_ref=None, grant=None, plan_ref=None):
    result = deepcopy(job['result'])
    result.update(source_sha256=manifest['source_sha256'], machine_web={
        'manifest_file': manifest_ref['file'], 'manifest_sha256': manifest_ref['sha256'],
        'artifact_sha256': manifest['render']['sha256'], 'state': 'ready',
        'policy': MachineTweetsAPI.version, 'human_review': False})
    if grant is not None:
        result['oneoff_owner_age_exception'] = {'version': VERSION, 'grant_id': grant['grant_id'],
            'grant': grant_ref, 'plan': plan_ref, 'original_state': job['state'],
            'original_stage': job['stage'], 'original_error_code': job['error_code'],
            'original_attempts': job['attempts'], 'original_row_sha256': row_sha(job),
            'owner_response': grant['owner_response'], 'owner_response_reference': grant['owner_response_reference'],
            'age_limit_exception_only': True, 'human_review': False, 'created_at_unchanged': True}
    return result


def prepare(settings, store):
    """Render/validate genuine existing proofs. Never update a job or call a model."""
    with resource_guard(settings.root/'data/locks/tweet'):
        job = target(settings, store);before = row_sha(job)
        service = MachineTweetsAPI(settings, store)
        service._require_current_generation(job);service.require_source_not_cleared(job);policy = service.policy()
        if (job['state'] != 'failed' or job['error_code'] != 'MACHINE_COMPOSITOR_REAL_CHECK_FAILED' or
                job['attempts'] != 1 or job['lease_owner'] is not None or job.get('external_publish_id') is not None or
                job['result'].get('machine_web') or source_publications(store)):
            fail('ONEOFF_PREPARE_STATE_INVALID')
        if sha256_file(settings.root/'compositor/src/core.mjs') != CORE_SHA256:
            fail('ONEOFF_REPAIRED_COMPOSITOR_REQUIRED')
        source = service.source(job, require_fresh=True)
        if [n['role'] for n in source['nodes']] != ['post']:
            fail('ONEOFF_SOURCE_SCOPE_CHANGED')
        child = store.get(MODEL_JOB_ID)
        if not child or child['state'] != 'completed' or child['result'].get('human_review') is not False:
            fail('ONEOFF_COMPLETED_MODEL_REQUIRED')
        entry = {'job_id': MODEL_JOB_ID, 'translation': child['result']['translation'],
            'quality': child['result']['quality']['independent_model_advice']}
        service._model_proof(job, source['nodes'][0], entry)
        built = service.render_evidence(job, source, {'post': entry})
        render, models, attempt = (built[k] for k in ('render', 'models', 'attempt'))
        caption = '【补发】'+built['caption']
        manifest = {'version': service.version, 'job_id': JOB_ID, 'job_policy_version': job['policy_version'],
            'post_id': SOURCE_ID, 'source_url': job['payload']['url'],
            'source_sha256': sha256_file(settings.root/'media'/JOB_ID/'source.json'),
            'target_site_uid': SITE_UID, 'target_name': settings.config['target_bilibili_account_name'],
            'policy_sha256': digest(policy), 'caption': caption, 'caption_sha256': hashlib.sha256(caption.encode()).hexdigest(),
            'source_proof': source['proof'], 'models': models, 'render': render, 'human_review': False,
            'approval_basis': 'owner_authorized_versioned_machine_policy', 'renderer_grants_publication': False}
        manifest_ref = write_new(attempt/'oneoff-age-manifest-v1.json', manifest)
        projected = deepcopy(job)
        projected.update(state='upload_pending', result=prepared_result(job, manifest_ref, manifest))
        MachineTweetsAPI(settings, ProjectedStore(store, projected))._snapshot(JOB_ID)
        if row_sha(target(settings, store)) != before or source_publications(store):
            fail('ONEOFF_JOB_CHANGED_DURING_PREPARE')
        plan_id = str(uuid.uuid4())
        plan = {'version': PLAN_VERSION, 'plan_id': plan_id, 'created_at': time.time(), 'job_id': JOB_ID,
            'source_id': SOURCE_ID, 'target_site_uid': SITE_UID, 'before_row_sha256': before,
            'job_policy_version': job['policy_version'], 'payload_sha256': digest(job['payload']),
            'policy_sha256': digest(policy), 'original_created_at': job['payload']['post']['created_at'],
            'original_state': job['state'], 'original_error_code': job['error_code'], 'original_attempts': job['attempts'],
            'manifest': manifest_ref, 'artifact_sha256': render['sha256'], 'caption_sha256': manifest['caption_sha256'],
            'caption': caption, 'model_job_id': MODEL_JOB_ID, 'human_review': False,
            'publication_authorized': False, 'job_unchanged': True, 'paid_model_calls': 0}
        ref = write_new(settings.root/'media'/JOB_ID/'oneoff-age-v1'/plan_id/'plan.json', plan)
        return {'version': VERSION, 'state': 'evidence_prepared_no_job_mutation', 'plan': ref,
            'artifact': {'file': render['png'], 'sha256': render['sha256']}, 'caption': caption,
            'normal_max_post_age_seconds': policy['max_post_age_seconds'], 'job_unchanged': True,
            'publication_authorized': False, 'human_review': False, 'paid_model_calls': 0}


def load_plan(settings, file, expected_sha):
    file = Path(file);root = settings.root
    if (not SHA.fullmatch(expected_sha or '') or file.name != 'plan.json' or
            file.parent.parent != root/'media'/JOB_ID/'oneoff-age-v1' or not _uuid(file.parent.name)):
        fail('ONEOFF_PLAN_PATH_INVALID')
    raw = confined_file(root, file)
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        fail('ONEOFF_PLAN_HASH_CHANGED')
    plan = strict_json(raw)
    if (plan['version'] != PLAN_VERSION or plan['plan_id'] != file.parent.name or
            plan['job_id'] != JOB_ID or plan['source_id'] != SOURCE_ID or plan['target_site_uid'] != SITE_UID or
            plan['original_state'] != 'failed' or plan['original_error_code'] != 'MACHINE_COMPOSITOR_REAL_CHECK_FAILED' or
            plan['original_attempts'] != 1 or plan['model_job_id'] != MODEL_JOB_ID or
            plan['human_review'] is not False or plan['publication_authorized'] is not False or
            plan['job_unchanged'] is not True or plan['paid_model_calls'] != 0 or not plan['caption'].startswith('【补发】')):
        fail('ONEOFF_PLAN_INVALID')
    ref = {'file': str(file), 'sha256': expected_sha, 'bytes': len(raw)}
    return plan, ref


def protected_grant(settings, file, expected_sha, plan, plan_ref, *, require_live=False):
    file = Path(file)
    if file != settings.root/'config/oneoff-tweet-age-v1/grant.json' or not SHA.fullmatch(expected_sha or ''):
        fail('ONEOFF_ROOT_GRANT_PATH_INVALID')
    raw = confined_file(settings.root, file, maximum=16384)
    # This is an actual root-created permission, never an app-created boolean.
    for item in (file, *file.parents):
        stat = item.stat()
        if stat.st_uid != 0 or stat.st_mode & 0o022:
            fail('ONEOFF_ROOT_GRANT_NOT_PROTECTED')
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        fail('ONEOFF_ROOT_GRANT_HASH_CHANGED')
    grant = strict_json(raw)
    validate_grant(grant, plan, plan_ref, require_live=require_live)
    return grant, {'file': str(file), 'sha256': expected_sha, 'bytes': len(raw)}


def validate_grant(grant, plan, plan_ref, *, require_live=False):
    now = time.time()
    if (set(grant) != GRANT_KEYS or grant['version'] != GRANT_VERSION or not _uuid(grant['grant_id']) or
            grant['job_id'] != JOB_ID or grant['source_id'] != SOURCE_ID or grant['target_site_uid'] != SITE_UID or
            grant['owner_response'] != OWNER_RESPONSE or not _reference(grant['owner_response_reference']) or
            grant['human_review'] is not False or grant['max_dispatches'] != 1 or type(grant['max_dispatches']) is not int or
            grant['scope'] != ['ignore_source_post_age_for_this_job_once'] or
            not _number(grant['issued_at']) or not _number(grant['expires_at']) or
            not plan['created_at']-5 <= grant['issued_at'] <= now+5 or
            not 0 < grant['expires_at']-grant['issued_at'] <= 3600 or
            grant['plan_file'] != plan_ref['file'] or grant['plan_sha256'] != plan_ref['sha256'] or
            any(grant[key] != plan[key] for key in ('artifact_sha256', 'caption_sha256', 'job_policy_version', 'payload_sha256', 'policy_sha256')) or
            grant['manifest_sha256'] != plan['manifest']['sha256']):
        fail('ONEOFF_ROOT_GRANT_INVALID')
    if require_live and not grant['issued_at']-5 <= now < grant['expires_at']:
        fail('ONEOFF_ROOT_GRANT_EXPIRED')


class OneoffMachineTweetsAPI(MachineTweetsAPI):
    def __init__(self, settings, store, *, plan, plan_ref, grant_file, grant_sha256):
        super().__init__(settings, store)
        self.plan, self.plan_ref = plan, plan_ref
        self.grant_file, self.grant_sha256 = Path(grant_file), grant_sha256
        self.grant, self.grant_ref = self.check_grant()

    def check_grant(self, *, require_live=False):
        grant, ref = protected_grant(self.settings, self.grant_file, self.grant_sha256,
            self.plan, self.plan_ref, require_live=require_live)
        job = target(self.settings, self.store)
        if (digest(job['payload']) != self.plan['payload_sha256'] or job['policy_version'] != self.plan['job_policy_version'] or
                job['payload']['post']['created_at'] != self.plan['original_created_at'] or
                digest(self.policy()) != self.plan['policy_sha256']):
            fail('ONEOFF_BOUND_SOURCE_OR_POLICY_CHANGED')
        return grant, ref

    def fresh(self, job, *, now=None):
        # Only the post-age comparison differs. These ordinary gates all remain.
        if job['job_id'] != JOB_ID or job['source_id'] != SOURCE_ID or digest(job['payload']) != self.plan['payload_sha256']:
            fail('ONEOFF_TARGET_MISMATCH')
        self._require_current_generation(job);self.require_source_not_cleared(job)
        self.check_grant(require_live=True)
        policy = self.policy()
        try:
            moment = datetime.fromisoformat(job['payload']['post']['created_at'].replace('Z', '+00:00'))
            if moment.tzinfo is None or moment.timestamp() > time.time()+5:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            fail('MACHINE_SOURCE_TIMESTAMP_INVALID')
        return policy

    def reserve(self, principal, job_id, *, site_uid, artifact_sha256, caption_sha256):
        grant, ref = self.check_grant(require_live=True)
        if (job_id != JOB_ID or site_uid != SITE_UID or artifact_sha256 != grant['artifact_sha256'] or
                caption_sha256 != grant['caption_sha256']):
            fail('ONEOFF_DISPATCH_BINDING_CHANGED')
        result = super().reserve(principal, job_id, site_uid=site_uid,
            artifact_sha256=artifact_sha256, caption_sha256=caption_sha256)
        if result['can_click_publish']:
            self.store.audit('owner-oneoff-age', 'consume_single_tweet_age_grant', JOB_ID,
                encode({'grant_id': grant['grant_id'], 'grant': ref, 'plan': self.plan_ref,
                    'idempotency_key': result['idempotency_key'], 'human_review': False,
                    'publication_authority': 'actual_owner_single_job_age_exception'}))
        return result


def adopt_prepared(service):
    settings, store = service.settings, service.store
    grant, grant_ref = service.check_grant(require_live=True)
    job = target(settings, store);plan = service.plan
    manifest_ref = plan['manifest']
    raw = confined_file(settings.root, manifest_ref['file'])
    if hashlib.sha256(raw).hexdigest() != manifest_ref['sha256'] or len(raw) != manifest_ref['bytes']:
        fail('ONEOFF_MANIFEST_HASH_CHANGED')
    manifest = strict_json(raw)
    if (manifest['render']['sha256'] != grant['artifact_sha256'] or manifest['caption_sha256'] != grant['caption_sha256'] or
            manifest['caption'] != plan['caption'] or manifest['policy_sha256'] != grant['policy_sha256']):
        fail('ONEOFF_MANIFEST_BINDING_CHANGED')
    service.fresh(job);service.source_check(job, require_fresh=True)
    existing = job['result'].get('oneoff_owner_age_exception')
    if existing:
        if (existing.get('grant') != grant_ref or existing.get('plan') != service.plan_ref or
                job['attempts'] != plan['original_attempts'] or job['result'].get('machine_web') !=
                prepared_result(job, manifest_ref, manifest)['machine_web'] or job['state'] != 'upload_pending'):
            fail('ONEOFF_PREPARED_STATE_CHANGED')
        service._snapshot(JOB_ID)
        return {'registered': False, 'already_registered': True}
    if (job['state'] != 'failed' or job['attempts'] != 1 or job['lease_owner'] is not None or
            job['error_code'] != plan['original_error_code'] or row_sha(job) != plan['before_row_sha256'] or source_publications(store)):
        fail('ONEOFF_PREPARE_CAS_REJECTED')
    result = prepared_result(job, manifest_ref, manifest, grant_ref=grant_ref, grant=grant, plan_ref=service.plan_ref)
    projected = deepcopy(job);projected.update(state='upload_pending', result=result)
    MachineTweetsAPI(settings, ProjectedStore(store, projected))._snapshot(JOB_ID)
    service.check_grant(require_live=True)
    now = time.time()
    with store.transaction() as db:
        row = db.execute('SELECT * FROM jobs WHERE job_id=?', (JOB_ID,)).fetchone()
        current = dict(row)
        current.update(payload=json.loads(current['payload']), result=json.loads(current['result']))
        if row_sha(current) != plan['before_row_sha256']:
            fail('ONEOFF_PREPARE_CAS_REJECTED')
        service.require_source_not_cleared(current, db=db)
        if db.execute("SELECT 1 FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=? AND account_id=?",
                (SOURCE_ID, 'site_uid:'+SITE_UID)).fetchone():
            fail('ONEOFF_SOURCE_ALREADY_RESERVED')
        changed = db.execute("UPDATE jobs SET state='upload_pending',stage='owner_age_exception_prepared',error_code=NULL,result=?,updated_at=? WHERE job_id=? AND state='failed' AND updated_at=? AND lease_owner IS NULL",
            (encode(result), now, JOB_ID, row['updated_at'])).rowcount
        if changed != 1:
            fail('ONEOFF_PREPARE_CAS_REJECTED')
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now, 'owner-oneoff-age', 'register_single_tweet_age_grant', JOB_ID, encode({
                'grant': grant_ref, 'grant_id': grant['grant_id'], 'plan': service.plan_ref,
                'original_state': job['state'], 'original_error_code': job['error_code'], 'attempts': job['attempts'],
                'human_review': False, 'source_created_at_unchanged': True, 'policy_max_age_unchanged': 7200})))
    service._snapshot(JOB_ID)
    return {'registered': True, 'already_registered': False}


def run_oneoff_website(service):
    """Existing coordinator contract; the grant supplies this one dispatch deadline."""
    settings, store = service.settings, service.store
    with resource_guard(settings.root/'data/locks/heavy'):
        job, manifest = service._snapshot(JOB_ID)
        grant, grant_ref = service.check_grant()
        known = source_publications(store)
        if known and (len(known) != 1 or known[0]['job_id'] != JOB_ID or known[0]['kind'] != service.kind):
            fail('ONEOFF_SOURCE_ALREADY_RESERVED')
        if known and known[0]['state'] == 'completed':
            return {'state': 'completed', 'url': known[0]['external_url'], 'external_id': known[0]['external_id'], 'sent': False}
        if known and known[0]['state'] == 'rejected':
            fail('ONEOFF_SOURCE_ALREADY_RESERVED')
        unresolved = bool(known)
        if not unresolved:
            service.fresh(job);service.source_check(job, require_fresh=True)
        attempt_id = str(uuid.uuid4())
        folder = settings.root/'media'/JOB_ID/'website-attempts'/attempt_id
        folder.mkdir(parents=True, mode=0o700)
        spec = {'version': 'bilibili-ui-transport-v1', 'job_id': JOB_ID, 'attempt_id': attempt_id,
            'mode': 'lookup' if unresolved else 'publish', 'source_url': manifest['source_url'],
            'target_site_uid': manifest['target_site_uid'], 'target_name': manifest['target_name'],
            'file': manifest['render']['png'], 'artifact_sha256': manifest['render']['sha256'], 'caption': manifest['caption'],
            'machine_policy': service.version, 'dispatch_expires_at': grant['expires_at'],
            'owner_age_exception_grant': {'version': VERSION, 'grant_id': grant['grant_id'], 'grant': grant_ref,
                'plan': service.plan_ref, 'original_created_at': service.plan['original_created_at'], 'human_review': False}}
        write_new(folder/'oneoff-dispatch-spec.json', spec)
        env = {k: os.environ[k] for k in ('PATH', 'PLAYWRIGHT_BROWSERS_PATH', 'TEMP', 'TMP', 'TMPDIR') if k in os.environ}
        env.update(HUB_ROOT=str(settings.root), BILIBILI_EXPECTED_SITE_UID=SITE_UID,
            HUB_BROWSER_CHANNEL=settings.config.get('web_browser_channel', 'chromium'),
            XDG_CONFIG_HOME='/tmp/lighthouse-browser-config', XDG_CACHE_HOME='/tmp/lighthouse-browser-cache')
        proc = subprocess.Popen(['node', str(settings.root/'adapters/browser_publisher.mjs')],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        proc.stdin.write(encode(spec).encode('utf-8'));proc.stdin.close()
        reserved = unresolved
        try:
            deadline = time.monotonic()+18*60;preparation_deadline = time.monotonic()+120
            while time.monotonic() < deadline:
                result_file = folder/'result.json'
                if result_file.exists():
                    outcome = strict_json(confined_file(settings.root, result_file))
                    if outcome['state'] in ('published_visible', 'existing_visible'):
                        evidence_file = folder/'visible-result.json'
                        write_new(evidence_file, outcome['evidence'])
                        if not reserved:
                            permit = service.reserve('machine-policy', JOB_ID, site_uid=SITE_UID,
                                artifact_sha256=spec['artifact_sha256'], caption_sha256=manifest['caption_sha256'])
                            if not permit['can_click_publish']:
                                raise PublishUnknown()
                            reserved = True
                        return service.record_visible('machine-policy', JOB_ID, evidence_file)
                    if reserved:
                        if outcome.get('error_code') == 'AUTH_EXPIRED':
                            store.event('auth.expired', {'job_id': JOB_ID, 'component': 'bilibili_website'}, dedup_key='web-auth:'+JOB_ID)
                        raise PublishUnknown()
                    raise HubError(outcome.get('error_code', 'WEB_PUBLICATION_BLOCKED'))
                ready = folder/'ready.json'
                if ready.exists() and not reserved:
                    actual = strict_json(confined_file(settings.root, ready))
                    if (actual.get('attempt_id') != attempt_id or actual.get('site_uid') != SITE_UID or
                            actual.get('artifact_sha256') != spec['artifact_sha256'] or
                            actual.get('caption_sha256') != manifest['caption_sha256']):
                        fail('ONEOFF_WEBSITE_DRAFT_MISMATCH')
                    permit = service.reserve('machine-policy', JOB_ID, site_uid=SITE_UID,
                        artifact_sha256=spec['artifact_sha256'], caption_sha256=manifest['caption_sha256'])
                    if not permit['can_click_publish']:
                        raise PublishUnknown()
                    reserved = True
                    temporary = folder/'commit.partial'
                    write_new(temporary, {'action': 'publish', 'attempt_id': attempt_id, 'artifact_sha256': spec['artifact_sha256']})
                    temporary.replace(folder/'commit.json')
                    from services.hub.web_source import WebSources
                    WebSources._sync_dir(folder)
                if proc.poll() is not None:
                    if reserved:
                        raise PublishUnknown()
                    fail('WEB_BROWSER_EXITED_BEFORE_DISPATCH')
                if not ready.exists() and time.monotonic() > preparation_deadline:
                    raise PublishUnknown() if reserved else HubError('WEB_DRAFT_PREPARATION_TIMEOUT', retryable=True)
                time.sleep(0.25)
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


def publish(settings, store, *, plan_file, plan_sha256, grant_file, grant_sha256, lookup_only=False):
    with resource_guard(settings.root/'data/locks/tweet'):
        plan, plan_ref = load_plan(settings, plan_file, plan_sha256)
        service = OneoffMachineTweetsAPI(settings, store, plan=plan, plan_ref=plan_ref,
            grant_file=grant_file, grant_sha256=grant_sha256)
        known = source_publications(store)
        if lookup_only and not known:
            fail('ONEOFF_LOOKUP_RESERVATION_REQUIRED')
        if not known:
            adopt_prepared(service)
        result = run_oneoff_website(service)
        record = {'version': VERSION, 'job_id': JOB_ID, 'source_id': SOURCE_ID,
            'grant': service.grant_ref, 'plan': plan_ref, 'result': result,
            'human_review': False, 'attempts_reset': False, 'policy_max_age_unchanged': 7200}
        receipt = write_new(settings.root/'media'/JOB_ID/'oneoff-age-v1'/plan['plan_id']/('result-'+str(uuid.uuid4())+'.json'), record)
        return {**record, 'receipt': receipt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('plan', 'prepare', 'publish', 'lookup'), default='plan')
    parser.add_argument('--plan-file');parser.add_argument('--plan-sha256')
    parser.add_argument('--grant-file');parser.add_argument('--grant-sha256')
    args = parser.parse_args()
    if os.name != 'posix' or os.getuid() != 10001:
        fail('ONEOFF_APP_UID_REQUIRED')
    settings = Settings.load();store = Store(settings.db_path)
    if args.mode == 'plan':
        result = inspect_target(settings, store)
    elif args.mode == 'prepare':
        result = prepare(settings, store)
    else:
        if not all((args.plan_file, args.plan_sha256, args.grant_file, args.grant_sha256)):
            parser.error('plan/grant absolute paths and SHA-256 are required')
        result = publish(settings, store, plan_file=args.plan_file, plan_sha256=args.plan_sha256,
            grant_file=args.grant_file, grant_sha256=args.grant_sha256, lookup_only=args.mode == 'lookup')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except (HubError, PublishUnknown) as exc:
        print(json.dumps({'version': VERSION, 'error_code': exc.code, 'state': 'lookup_required' if isinstance(exc, PublishUnknown) else 'stopped',
            'human_review': False, 'automatic_retry': False}))
        raise SystemExit(2) from None
    except Exception as exc:
        print(json.dumps({'version': VERSION, 'error_code': 'ONEOFF_OPERATOR_FAILED', 'exception_class': type(exc).__name__,
            'state': 'stopped_reconcile_before_retry', 'human_review': False, 'automatic_retry': False}))
        raise SystemExit(2) from None
