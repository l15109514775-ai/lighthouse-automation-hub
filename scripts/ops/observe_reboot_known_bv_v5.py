"""Fixed post-reboot known-BV read-only native check. App UID 10001 only.

Run only after root confirms all cutovers and host reboot finished. No claim,
lease, record_result, queue change, upload, prepare or submit exists here.
"""
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib, json, os, signal, sqlite3, time, uuid
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.errors import HubError
from services.hub.replay_parts_v5 import ReplayPartVideoWeb, reference_body
from services.hub.replay_part_publish_worker_v5 import PartReplayBrowser, part_replay_manifest
from services.hub.replay_part_public_evidence_v5 import validate_result

JOB = '5c4be72a-e6ae-4d42-b030-9540f3d54baf'
GROUP = 'cf7b357c-a547-4a50-9943-15e64d69888c'
ATTEMPT = 'e3a44d84-5e8f-4b70-8b3b-8d63581b17c3'
SOURCE = '6wHqfDmNF0U'
UID = '1000000000000000'
BV = 'BV1gv4A6cEdK'
FIRST = 'a674e7326f2a4e0e5125fa6decbc09e7af96c90a4231612616b348cc5f1a0460'
INTENTS = {'video-selection.json', 'cover-completion.json', 'private-draft-save.json', 'public-submit.json'}


def need(value):
    if not value:
        raise HubError('REBOOT_KNOWN_BV_READONLY_FENCE_CHANGED', review=True)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def small(file, maximum=2_000_000):
    need(file.resolve() == file and not any(p.is_symlink() for p in (file, *file.parents)))
    with os.fdopen(os.open(file, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        before = os.fstat(stream.fileno()); need(0 < before.st_size <= maximum)
        raw = stream.read(maximum + 1); after = os.fstat(stream.fileno())
    need(len(raw) == before.st_size and all(getattr(before, k) == getattr(after, k) for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')))
    return raw


def save(file, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode() + b'\n'
    with os.fdopen(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    fd = os.open(file.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try: os.fsync(fd)
    finally: os.close(fd)
    return {'file': str(file), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


class ReadOnlyStore(Store):
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row; db.execute('PRAGMA query_only=ON')
        return db

    @contextmanager
    def transaction(self):
        raise HubError('READONLY_OBSERVER_DATABASE_WRITE_FORBIDDEN', review=True)
        yield


def main():
    need(os.name == 'posix' and os.getuid() == os.getgid() == 10001)
    os.umask(0o077)
    settings = Settings.load(); need(settings.root == Path('/opt/lighthouse-hub'))
    store = ReadOnlyStore(settings.db_path); service = ReplayPartVideoWeb(settings, store, part_index=0)
    job, binding, state = service.snapshot(JOB, write=False)
    need(job['state'] == state['state'] == 'completed' and job['attempts'] == 2 and job['external_publish_id'] == BV and
         job['source_id'] == SOURCE and state['attempt_id'] == ATTEMPT and binding['site_uid'] == UID and
         binding['parent_job_id'] == GROUP and binding['part_index'] == 0 and binding['part_count'] == 1)
    parent = store.get(GROUP); need(parent['state'] == 'completed')
    first = parent['result']['replay_parts_first_completion_v5']; need(first['sha256'] == FIRST)
    intent_folder = settings.root / 'media' / JOB / 'replay-native-intents'

    def fingerprint():
        current = store.get(JOB); group = store.get(GROUP)
        pubs = store.query('SELECT * FROM publications WHERE job_id=?', (JOB,))
        need(current['state'] == 'completed' and current['lease_owner'] is None and current['lease_until'] is None and
             len(pubs) == 1 and pubs[0]['state'] == 'completed' and pubs[0]['external_id'] == BV)
        need({p.name for p in intent_folder.iterdir()} == INTENTS)
        intents = {n: hashlib.sha256(small(intent_folder / n, 65536)).hexdigest() for n in sorted(INTENTS)}
        proof = group['result']['replay_parts_first_completion_v5']; need(proof == first)
        raw = small(Path(proof['file'])); need(len(raw) == proof['bytes'] and hashlib.sha256(raw).hexdigest() == FIRST)
        return {'job': digest(current), 'publication': digest(pubs), 'group': digest(group), 'intents': intents, 'first_completion': FIRST}

    original = fingerprint()
    config = json.loads(small(settings.root / 'config/bilibili-video-handoff-v5.json', 4096))
    ttl = config['ttl_seconds']; need(type(ttl) is int and 60 <= ttl <= 1200)
    deadline = time.monotonic() + 300 + ttl + 90
    area = settings.root / 'media' / JOB / 'post-reboot-known-bv-readonly-v1' / str(uuid.uuid4())
    area.mkdir(parents=True, mode=0o700); need(area.resolve() == area)

    def guard():
        need(time.monotonic() < deadline and fingerprint() == original)

    def owner_observation(body, reference):
        # An actual observer stop is durable only in this owned JSON, never a
        # fabricated Worker lease or DB hold. The existing Node/Python protocol
        # retains the original protected page. The existing remote bridge may
        # reject this completed-job context; root handles that real case.
        guard()
        ref = save(area / ('owner-observation-' + body['nonce'] + '.json'), {
            'reason': body['reason'], 'event_reference': reference, 'nonce': body['nonce'],
            'expires_at': body['expires_at'], 'readonly_observer_stopped': True,
            'database_hold_created': False, 'lease_created': False, 'automatic_retry': False})
        print(json.dumps({'state': 'owner_observation_required', 'reference': ref, 'nonce': body['nonce'],
                          'expires_at': body['expires_at'], 'remote_bridge_completed_context_supported': False}), flush=True)

    manifest = part_replay_manifest(settings, store, job, binding, state, ATTEMPT, require_cover=False)
    report = {'job_id': JOB, 'source_id': SOURCE, 'attempt_id': ATTEMPT, 'external_id': BV,
              'before': original, 'record_result_calls': 0, 'queue_changes': 0, 'upload_prepare_submit_calls': 0}
    try:
        guard()
        with PartReplayBrowser(settings).session(manifest, assert_lease=guard, owner_handoff=owner_observation) as session:
            result = session.operation('lookup', external_id=BV)
            guard()
        observation = result.get('result_observation'); reference = result.get('result_reference')
        need(result.get('state') == 'completed' and isinstance(reference, dict) and reference_body(settings.root, reference) == observation)
        coverage = validate_result(service, job, binding, state, observation)
        guard()
        report.update(state='known_bv_readonly_verified', result_reference=reference, public_coverage=coverage,
                      after=fingerprint(), observed_at=datetime.now(timezone.utc).isoformat())
    except BaseException as error:
        report.update(state='stopped_readonly', error_class=type(error).__name__, code=getattr(error, 'code', None), after=fingerprint())
        save(area / 'result.json', report)
        raise
    reference = save(area / 'result.json', report)
    print(json.dumps({'state': report['state'], 'reference': reference, 'database_unchanged': report['after'] == original}), flush=True)


def interrupted(*_args):
    raise HubError('READONLY_OBSERVER_INTERRUPTED', review=True)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted); signal.signal(signal.SIGINT, interrupted)
    main()
