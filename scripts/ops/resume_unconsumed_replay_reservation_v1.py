"""Default-plan: recover one reserved, demonstrably unclicked native replay.

Independent operator only; no deployed module, queue lease, counter or policy
is changed. The original source-wide publication/key remains in place.
"""
from copy import deepcopy
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import types
import uuid

JOB = '594aed06-3aa1-45f4-90f0-1db49a602395'
ATTEMPT = '150c1074-4aed-4f5d-b87a-83cf2840af24'
UID = '1000000000000000'
SOURCE = '9T5tn1WGlJg'
VERSION = 'replay-unconsumed-reservation-recovery-v1'
APPLIER_SHA = '118189718b4b41e0728db3eb383c93e1c1fa2dc99e585803d9c571d9adbc398e'
ERROR_SHA = '51cdddef5c0f1030b9075d4a2655261b07d40cf339e33f49d2b1b06f45e5657c'
OWNED_CLEANUP_SHA = '81022dddf5ab0f2e74332804c5f9d00075ebffee44efb7de73d94015e52fe7eb'
NODE_WRAPPER_SHA = '8a88dbe2b3500d16728709cdb64758aa15c094bd7b90872a35f7751f67cb14f0'
KINDS = {'video-selection.json', 'cover-completion.json', 'private-draft-save.json'}


def wire(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def safe(file):
    file = Path(file)
    assert file.is_absolute() and file.resolve() == file
    assert not any(p.is_symlink() for p in (file, *file.parents))
    return file


def reference(file, maximum=1_000_000):
    file = safe(file)
    assert file.is_file() and 0 < file.stat().st_size <= maximum
    raw = file.read_bytes()
    assert 0 < len(raw) <= maximum
    return {'file': str(file), 'bytes': len(raw), 'sha256': sha(raw)}, raw


def write(file, body, *, exclusive=False):
    file = safe(file)
    target = file if exclusive else file.with_name(file.name + '.partial')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(wire(body)); stream.flush(); os.fsync(stream.fileno())
    if not exclusive:
        os.replace(target, file)
    if os.name == 'posix':
        fd = os.open(file.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(fd)
        finally: os.close(fd)


def publications(store, job):
    return store.query("SELECT * FROM publications WHERE job_id=? OR (platform='bilibili' AND source_platform='youtube' AND source_id=?)",
                       (job['job_id'], job['source_id']))


def validate_original(job, state, pubs):
    assert job['state'] == 'publish_unknown' and job['attempts'] == 3
    assert job['lease_owner'] is None and job['lease_until'] is None and job['external_publish_id'] is None
    assert state['state'] == 'submit_unknown' and state['public_submission_approved'] is False
    assert state['human_review'] is False and 'owner_acceptance' not in state and not state.get('result_observation')
    assert job['result']['publish_performed'] is False and 'reservation_recovery' not in state
    assert len(pubs) == 1
    pub = pubs[0]
    assert pub['job_id'] == job['job_id'] and pub['idempotency_key'] == state['key']
    assert pub['platform'] == 'bilibili' and pub['source_platform'] == 'youtube' and pub['source_id'] == job['source_id']
    assert pub['account_id'] == 'site_uid:' + job['payload']['site_uid']
    assert pub['state'] == 'unknown' and pub['external_id'] is None and pub['external_url'] is None


def validate_manager(result, inventory, metadata):
    """An actually loaded empty ALL tab, never an empty early DOM count."""
    assert result == {'state': 'unknown'}
    dom = inventory['visible_dom']
    assert dom['url'] == 'https://member.bilibili.com/platform/upload-manager/article'
    text = dom['visible_text']
    for line in ('视频管理', '图文管理', '互动视频管理', '全部稿件 0', '草稿 2',
                 '进行中 0', '已发布 0', '未通过 0', '一个稿件都没有，请换个筛选条件'):
        assert line in [s.strip() for s in text.splitlines()]
    assert metadata['title'] not in text and metadata['source_url'] not in text
    assert not re.search(r'BV[A-Za-z0-9]{10}', text)
    assert not any(re.search(r'/video/BV[A-Za-z0-9]{10}', item.get('url') or '') for item in dom['links'])


def owned_session_factory(cleanup_raw, node_raw, output):
    """Reuse the proven PID/start-time/descendant cleanup, never broad pkill."""
    from services.hub.replay_video_browser import ReplayBrowserSession
    raw = cleanup_raw.encode(); assert sha(raw) == OWNED_CLEANUP_SHA
    assert sha(node_raw.encode()) == NODE_WRAPPER_SHA
    ns = {'__name__': 'pinned_owned_process_cleanup'}
    exec(compile(raw, '<pinned-owned-cleanup>', 'exec'), ns)
    class OwnedSession(ReplayBrowserSession):
        def __init__(self, *args, **kwargs):
            self._owned, self._groups, self._child = {}, {}, None
            self._stop_tracking, self._tracker, self._cleaned = threading.Event(), None, False
            self._baseline = {(pid, r['start_ticks']) for pid, r in ns['proc_table']().items()}
            def create(*a, **k):
                assert len(a) == 1 and a[0] == ['node', str(args[0].root/'adapters/browser_replay_video.mjs')]
                self._child = subprocess.Popen(['node', '--input-type=module', '-e', node_raw], **k)
                def track():
                    while not self._stop_tracking.is_set():
                        ns['track_owned'](self._child, self._owned, self._groups, self._baseline)
                        self._stop_tracking.wait(.05)
                self._tracker = threading.Thread(target=track, daemon=True); self._tracker.start()
                return self._child
            try: super().__init__(*args, process_factory=create, **kwargs)
            except BaseException:
                self._cleanup(); raise
        def _cleanup(self):
            if self._cleaned: return
            self._stop_tracking.set()
            if self._tracker: self._tracker.join(timeout=2)
            assert not self._tracker or not self._tracker.is_alive()
            ns['cleanup_owned'](self._child, self._owned, self._groups, self._baseline, output)
            self._cleaned = True
        def close(self):
            try: super().close()
            finally: self._cleanup()
    return OwnedSession


class ReuseUnconsumedReservation:
    """Override only reservation INSERT, after all original parent gates ran."""
    def _reserve_submit_once(self, principal, job, binding, state, *, site_uid):
        from services.hub.db import encode
        self._operator(principal)
        self.recovery_check()
        assert not self.recovery_used and state['state'] == 'uploaded'
        assert state['reservation_recovery']['operation_id'] == self.recovery_operation
        assert site_uid == binding['site_uid'] and state['key'] == self.recovery_publication['idempotency_key']
        assert not (self.recovery_intents / 'public-submit.json').exists()
        now = time.time()
        with self.store.transaction() as db:
            current = db.execute('SELECT * FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
            assert self._checkpoint_matches(current, job)
            rows = [dict(r) for r in db.execute("SELECT * FROM publications WHERE job_id=? OR (platform='bilibili' AND source_platform='youtube' AND source_id=?)",
                                               (job['job_id'], job['source_id']))]
            assert rows == [self.recovery_publication]
            assert not (self.recovery_intents / 'public-submit.json').exists()
            changed = db.execute("UPDATE publications SET state='dispatching',updated_at=? WHERE idempotency_key=? AND job_id=? AND state='unknown' AND external_id IS NULL AND external_url IS NULL",
                                 (now, state['key'], job['job_id'])).rowcount
            assert changed == 1
            checkpoint = {**state, 'state': 'submitting'}
            db.execute("UPDATE jobs SET state='publish_unknown',stage='replay_unconsumed_reservation_reused',error_code='RECONCILE_BEFORE_RETRY',result=?,updated_at=? WHERE job_id=?",
                       (encode({**job['result'], self.STATE: checkpoint}), now, job['job_id']))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (now, principal, 'replay_unconsumed_reservation_reused', job['job_id'], encode({
                           'operation_id': self.recovery_operation, 'original_idempotency_key': state['key'],
                           'original_publication_preserved': True, 'attempts_reset': False, 'new_upload': False})))
        self.recovery_used = True
        return {'can_click_submit': True, 'state': 'dispatching', 'idempotency_key': state['key']}


class OperationGuard:
    def __init__(self, settings, store, job, pubs, intents, pins):
        self.settings, self.store, self.expected, self.pubs = settings, store, deepcopy(job), deepcopy(pubs)
        self.original, self.deadline, self.pins = deepcopy(job), time.monotonic() + 1200, pins
        self.intents = intents
        self.original_intents, self.ticket = self.intent_hashes(), None
        self.source = store.get(job['payload']['download_job_id'])

    def intent_hashes(self):
        return {p.name: reference(p, 100000)[0]['sha256'] for p in self.intents.iterdir()}

    def __call__(self):
        assert time.monotonic() < self.deadline
        assert self.store.get(self.expected['job_id']) == self.expected
        assert publications(self.store, self.expected) == self.pubs
        assert self.store.get(self.original['payload']['download_job_id']) == self.source
        assert not self.store.query("SELECT 1 FROM jobs WHERE queue='video' AND state='processing' LIMIT 1")
        for name, expected in self.pins.items():
            assert reference(self.settings.root / name, 1_000_000)[0]['sha256'] == expected
        actual = self.intent_hashes()
        assert {k: v for k, v in actual.items() if k != 'public-submit.json'} == self.original_intents
        if 'public-submit.json' in actual:
            assert self.ticket is not None
            _, raw = reference(self.intents / 'public-submit.json', 100000)
            intent = json.loads(raw)
            binding = self.original['payload']
            expected = {k: binding[k] for k in ('source_id', 'source_url', 'artifact_sha256', 'file_bytes', 'metadata_sha256')}
            expected.update(job_id=self.original['job_id'], attempt_id=self.original['result'][self.state_key]['attempt_id'],
                            site_uid=binding['site_uid'], binding_sha256=sha(wire(binding)))
            assert all(intent.get(k) == v for k, v in expected.items())
            assert intent['version'] == 'replay-native-one-use-intent-v1' and intent['kind'] == 'public-submit'
            assert intent['reservation'] == self.ticket

    def adopt(self):
        fresh = self.store.get(self.original['job_id'])
        assert all(fresh[k] == v for k, v in self.original.items() if k not in ('result', 'state', 'stage', 'error_code', 'updated_at', 'external_publish_id'))
        assert fresh['state'] in ('publish_unknown', 'awaiting_moderation', 'completed', 'failed')
        self.expected, self.pubs = fresh, publications(self.store, fresh)
        self()


def app_main(request, submit=False):
    from services.hub.config import Settings
    from services.hub.db import Store, encode
    from services.hub.short_video_web import PRINCIPAL
    from services.hub.replay_highest_video_v2 import HighestReplayVideoWeb, highest_replay_manifest
    from services.hub.replay_video_browser import ReplayVideoBrowser
    from services.hub.replay_highest_coverage_v2 import upload_coverage
    from services.hub.locks import resource_guard
    assert os.name == 'posix' and os.getuid() == os.getgid() == 10001 and not __import__('sys').flags.optimize
    settings = Settings.load(); store = Store(settings.db_path)
    class Service(ReuseUnconsumedReservation, HighestReplayVideoWeb):
        def assert_worker_lease(self):
            if getattr(self, 'recovery_check', None): self.recovery_check()
    service = Service(settings, store); service._operator(PRINCIPAL)
    with resource_guard(settings.root/'data/locks/video', settings.root/'data/locks/moderation'):
        job, binding, state = service.snapshot(JOB, write=False)
        assert job['source_id'] == binding['source_id'] == SOURCE and binding['site_uid'] == UID
        assert state['attempt_id'] == ATTEMPT
        pubs = publications(store, job); validate_original(job, state, pubs)
        assert pubs[0]['kind'] == service.KIND
        intents = safe(settings.root/'media'/JOB/'replay-native-intents')
        assert {p.name for p in intents.iterdir()} == KINDS
        guard = OperationGuard(settings, store, job, pubs, intents, request['pins']); guard.state_key = service.STATE
        assert guard.original_intents == request['intents_sha256']
        service.recovery_check = guard
        guard()
        assert request['before_job_sha256'] == sha(wire(job))
        policy = service._validate_snapshot_binding(job, binding, verify_artifact_hash=False)
        service._require_workflow_launch(policy)
        # Exact deployed 45-source helper already fixes the Highest-v2 path;
        # its qualified signature has only fresh=, not later Natural kwargs.
        assert upload_coverage(settings, job, binding, state, state['upload_observation']['upload_coverage'],
                               fresh=False) == state['upload_coverage']
        output = safe(settings.root/'media'/JOB/service.NATIVE_DIRECTORY/ATTEMPT)
        error_ref, raw = reference(output/'last-error.json'); error = json.loads(raw)
        assert error_ref['sha256'] == ERROR_SHA and error['action'] == 'submit'
        assert error['error_code'] == 'REPLAY_BROWSER_DRAFT_LOOKUP_REQUIRED' and error['video_selection_attempted_in_session'] is False
        lookup_ref, raw = reference(Path(request['lookup']['file'])); assert lookup_ref == request['lookup']
        observed = json.loads(raw)
        assert observed['version'] == 'same-attempt-private-draft-readonly-lookup-v3'
        assert observed['state'] == 'same_attempt_private_draft_observed_readonly'
        assert all(observed[k] == v for k, v in {'job_id': JOB, 'attempt_id': ATTEMPT, 'site_uid': UID,
                   'source_url': binding['source_url'], 'metadata_sha256': binding['metadata_sha256']}.items())
        assert all(observed[k] is True for k in ('metadata_matches', 'upload_complete', 'cover_dom_present'))
        assert observed['server_file_hash_verified'] is False and observed['public_submission_observed'] is False
        assert observed['save_clicks'] == observed['submit_clicks'] == observed['new_file_selections'] == 0
        prior_preparation, raw = reference(output/'native-preparation.json'); old_preparation = json.loads(raw)
        assert prior_preparation == request['prior_preparation']
        assert old_preparation['private_draft_saved'] is old_preparation['metadata_verified'] is old_preparation['cover_applied'] is True
        assert all(old_preparation[k] == v for k, v in {'job_id': JOB, 'attempt_id': ATTEMPT, 'site_uid': UID,
                   'metadata_sha256': binding['metadata_sha256'], 'binding_sha256': sha(wire(binding))}.items())
        plan = {'version': VERSION, 'state': 'verified_plan_only', 'job_id': JOB, 'attempt_id': ATTEMPT,
                'before_job_sha256': sha(wire(job)), 'idempotency_key': state['key'], 'attempts': job['attempts'],
                'lookup': lookup_ref, 'prior_preparation': prior_preparation, 'prior_submit_error': error_ref,
                'new_video_selections': 0, 'fake_worker_lease': False, 'publication_deleted': False, 'submitted': False}
        operation_folder = safe(settings.root/'media'/JOB/'unconsumed-reservation-recovery-v1')
        assert not (operation_folder/'operation-intent.json').exists()
        if not submit:
            print(json.dumps(plan)); return
        operation_folder.mkdir(mode=0o700, exist_ok=True)
        ident = str(uuid.uuid4()); receipt_folder = operation_folder/ident; receipt_folder.mkdir(mode=0o700)
        manifest = highest_replay_manifest(settings, store, job, binding, state, ATTEMPT,
                    duration_seconds=guard.source['result'][service.DOWNLOAD_STATE]['binding']['duration'])
        changed = False
        try:
            factory = owned_session_factory(request['cleanup_code'], request['node_code'], receipt_folder)
            write(receipt_folder/'previous-submit-error-preserved.json', {'original_reference': error_ref, 'original_body': error})
            with ReplayVideoBrowser(settings, session_factory=factory).session(manifest, assert_lease=guard) as browser:
                queried = browser.operation('lookup')
                manager_ref, raw = reference(output/'lookup-inventory.json'); manager = json.loads(raw)
                validate_manager(queried, manager, {**binding['metadata'], 'source_url': binding['source_url']})
                # The original native preparation recovery branch is mandatory;
                # do not fall through to image selection or another draft save.
                assert (output/'native-preparation.json').is_file()
                prepared = browser.operation('prepare')
                assert prepared.get('recovered_without_modification') is True
                assert guard.intent_hashes() == guard.original_intents
                guard()
                recovery = {'version': VERSION, 'operation_id': ident, 'lookup': lookup_ref, 'manager_query': manager_ref,
                            'previous_checkpoint': deepcopy(state), 'previous_publication': deepcopy(pubs[0]),
                            'previous_error_code': job['error_code'], 'prior_submit_error': error_ref,
                            'prior_preparation': prior_preparation, 'new_authorization': False, 'attempts_reset': False}
                write(operation_folder/'operation-intent.json', {**plan, 'operation_id': ident, 'manager_query': manager_ref}, exclusive=True)
                service._update(PRINCIPAL, job, {**state, 'state': 'uploaded', 'reservation_recovery': recovery},
                                job_state='publish_unknown', stage='replay_unconsumed_reservation_observed')
                changed = True; guard.adopt()
                service.record_native_prepare(PRINCIPAL, JOB, prepared['native_preparation']); guard.adopt()
                service.recovery_operation, service.recovery_publication = ident, deepcopy(pubs[0])
                service.recovery_intents, service.recovery_used = intents, False
                reserved = service.reserve_submit(PRINCIPAL, JOB, attempt_id=ATTEMPT, site_uid=UID,
                                  upload_reference_sha256=state['upload_observation']['upload_reference_sha256'])
                guard.adopt()
                assert reserved['can_click_submit'] is True and reserved['idempotency_key'] == state['key']
                ticket = {'version': service.NATIVE_RESERVATION_VERSION, 'job_id': JOB, 'attempt_id': ATTEMPT,
                          'binding_sha256': sha(wire(binding)), 'metadata_sha256': binding['metadata_sha256'],
                          'can_click_submit': True, 'state': 'dispatching', 'idempotency_key': state['key'],
                          'machine_evidence_sha256': reserved['machine_evidence_sha256'], 'expires_at': time.time()+300}
                guard.ticket = ticket
                value = browser.operation('submit', reservation=ticket)
                guard()
                observation = value.get('result_observation')
                assert observation and observation['same_submit_attempt_observed'] is True
                service.record_result(PRINCIPAL, JOB, observation); guard.adopt()
                result = {**plan, 'state': store.get(JOB)['state'], 'submitted': True,
                          'external_id': observation['external_id'], 'operation_id': ident, 'manager_query': manager_ref}
                write(receipt_folder/'result.json', result); print(json.dumps(result))
        except BaseException as exc:
            # An exception never schedules or clicks again. Preserve the same
            # global reservation and every original/one-use native intent.
            if changed:
                try:
                    guard()
                    current, _, checkpoint = service.snapshot(JOB, write=False)
                    if checkpoint['state'] == 'uploaded':
                        service._update(PRINCIPAL, current, {**checkpoint, 'state': 'submit_unknown'},
                                        job_state='publish_unknown', stage='replay_unconsumed_recovery_unknown', code='PUBLISH_UNKNOWN')
                    elif checkpoint['state'] == 'submitting': service.mark_unknown(PRINCIPAL, JOB)
                    guard.adopt()
                except BaseException: pass
            code = getattr(exc, 'code', None)
            if not isinstance(code, str) or not re.fullmatch('[A-Z][A-Z0-9_]{1,100}', code): code = 'REPLAY_UNCONSUMED_RECOVERY_STOPPED'
            result = {**plan, 'state': 'reconcile_required', 'error_code': code, 'submitted': None,
                      'operation_id': ident, 'automatic_retry': False, 'manual_lease_created': False}
            write(receipt_folder/'result.json', result); print(json.dumps(result)); raise SystemExit(2)


def main():
    assert os.name == 'posix' and os.geteuid() == 0 and not __import__('sys').flags.optimize
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('navigation-applier-file', 'helper-file', 'owned-cleanup-file', 'node-wrapper-file', 'attestation', 'archive', 'deployment-report', 'lookup-file', 'lookup-sha256'):
        p.add_argument('--'+name, required=True)
    p.add_argument('--submit', action='store_true'); args = p.parse_args()
    applier = safe(Path(args.navigation_applier_file)); raw = applier.read_bytes(); assert sha(raw) == APPLIER_SHA
    a = types.ModuleType('pinned_navigation_applier'); a.__file__ = str(applier); exec(compile(raw, str(applier), 'exec'), a.__dict__)
    d = a.load_helper(args.helper_file)
    manifest, _, qualification = a.qualification(d, args.attestation, args.archive)
    current = d.inspect(d.cid('video-worker'))
    assert current['State']['Running'] and current['Image'] == a.IMAGE and current['Config']['User'] == '10001:10001'
    report = safe(Path(args.deployment_report)); d.protected(report); body = d.read(report, 500000); deployed = json.loads(body)
    assert deployed['version'] == 'saved-draft-navigation-single-source-v3' and deployed['state'] == 'active_verified_no_lookup_run'
    assert deployed['video_container_id'] == current['Id'] and deployed['qualification'] == qualification
    pins = manifest['runtime_source_sha256']; assert len(pins) == 45; a.sources(d, current['Id'], pins)
    assert all(sha(d.read(Path(file), 2_000_000)) == digest for file, digest in deployed['configuration_sha256'].items())
    before = a.observation(d); assert before['fingerprint'] == deployed['fingerprint'] and before['intents'] == deployed['intents']
    lookup = safe(Path(args.lookup_file)); assert lookup.parent.parent == d.ROOT/'media'/JOB/'draft-lookup-readonly-v3'
    lookup_ref, _ = reference(lookup); assert lookup_ref['sha256'] == args.lookup_sha256
    cleanup = safe(Path(args.owned_cleanup_file)).read_bytes(); assert sha(cleanup) == OWNED_CLEANUP_SHA
    node = safe(Path(args.node_wrapper_file)).read_bytes(); assert sha(node) == NODE_WRAPPER_SHA
    config_pins = {str(Path(file).relative_to(d.ROOT)): digest for file, digest in deployed['configuration_sha256'].items()}
    job_sha_code = "import hashlib,json;from services.hub.config import Settings;from services.hub.db import Store;s=Settings.load();j=Store(s.db_path).get('"+JOB+"');print(hashlib.sha256(json.dumps(j,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest())"
    row_sha = d.run(['/usr/bin/docker','exec','--user','10001:10001',current['Id'],'python','-c',job_sha_code]).strip()
    assert re.fullmatch('[a-f0-9]{64}', row_sha)
    request = {'lookup': lookup_ref, 'before_job_sha256': row_sha, 'pins': {**pins, **config_pins},
               'deployment': {'file': str(report), 'sha256': sha(body)}, 'qualification': qualification,
               'cleanup_code': cleanup.decode(), 'node_code': node.decode(), 'intents_sha256': before['intents'],
               'prior_preparation': reference(d.ROOT/'media'/JOB/'replay-browser-highest-v2'/ATTEMPT/'native-preparation.json')[0]}
    others = a.other_cores(d)
    captured = Path(__file__).read_bytes()
    program = "import json,sys;scope={'__name__':'pinned_recovery_operator'};exec(compile("+repr(captured)+",'<pinned-recovery>','exec'),scope);scope['app_main'](json.loads(sys.argv[1]),sys.argv[2]=='submit')"
    result = json.loads(d.run(['/usr/bin/docker','exec','--user','10001:10001',current['Id'],'python','-c',program,
                               wire(request).decode(), 'submit' if args.submit else 'plan'], timeout=1400))
    assert a.other_cores(d) == others and d.inspect(current['Id'])['State']['Running']
    assert all(sha(d.read(Path(file), 2_000_000)) == digest for file, digest in deployed['configuration_sha256'].items())
    print(json.dumps(result))


if __name__ == '__main__': main()
