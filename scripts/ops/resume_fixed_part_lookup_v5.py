"""Resume one blocked read-only lookup after the verified manager-link fix.

Default writes a reviewable plan. Never resets attempts, claims a job, sends a
platform request, or changes the original publication/submit intent. Run as the
app user in the qualified video container while video admission is paused.
"""
from pathlib import Path
import argparse, hashlib, json, os, re, stat, time

LOOKUP = '981cc3d6-140a-4dc1-8856-80439a859b0b'
PUBLISH = '5c4be72a-e6ae-4d42-b030-9540f3d54baf'
GROUP = 'cf7b357c-a547-4a50-9943-15e64d69888c'
SOURCE = '6wHqfDmNF0U'
ERROR = 'REPLAY_LATER_MANAGER_BV_CHANGED'
INTENT_SHA = '4fb57f0fd73e5638c67de4ed07dcbd743dcf508563b12797961b0372990ede55'
VERSION = 'resume-fixed-readonly-part-lookup-v5'
FILES = ('adapters/replay_later_result_lookup_v2.mjs', 'adapters/replay_native_driver.mjs',
         'services/hub/replay_part_public_evidence_v5.py')


def need(value, code):
    if not value:
        raise ValueError(code)


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(wire(value)).hexdigest()


def runtime(settings, expected):
    need(set(expected) == set(FILES) and all(re.fullmatch('[a-f0-9]{64}', v) for v in expected.values()), 'FIXED_RUNTIME_PINS_REQUIRED')
    actual = {}
    for name in FILES:
        file = settings.root / name
        need(file.resolve() == file and not any(p.is_symlink() for p in (file, *file.parents)), 'RUNTIME_PATH_CHANGED')
        actual[name] = hashlib.sha256(file.read_bytes()).hexdigest()
    need(actual == expected, 'QUALIFIED_RUNTIME_CHANGED')
    return actual


def snapshot(settings, store, expected):
    from services.hub.replay_part_moderation_v5 import LookupService, parent_context, child_payload, TABLE, ATTEMPTS, LOOKUP_JOB_TYPE, POLICY
    runtime(settings, expected)
    need(store.query("SELECT paused FROM pipelines WHERE name='video'")[0]['paused'] == 1, 'VIDEO_ADMISSION_MUST_BE_PAUSED')
    need(not store.query("SELECT job_id FROM jobs WHERE queue='video' AND (state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)"), 'VIDEO_WORK_STILL_ACTIVE')
    service = LookupService(settings, store, part_index=0)
    context = parent_context(settings, store, service, PUBLISH)
    parent, binding, state, publication, intent, _ = context
    child = store.get(LOOKUP)
    schedules = store.query('SELECT * FROM ' + TABLE + ' WHERE parent_job_id=?', (PUBLISH,))
    need(parent['source_id'] == SOURCE and binding['parent_job_id'] == GROUP and
         binding['site_uid'] == '1000000000000000' and intent['sha256'] == INTENT_SHA and
         parent['state'] == 'publish_unknown' and parent['attempts'] == 2 and
         publication['state'] == 'unknown' and publication['external_id'] is None, 'ORIGINAL_PUBLICATION_CHANGED')
    need(child and child['job_type'] == LOOKUP_JOB_TYPE and child['policy_version'] == POLICY and
         child['source_platform'] == 'bilibili' and child['source_id'] == PUBLISH + ':lookup:1' and child['queue'] == 'video' and
         child['state'] == 'awaiting_review' and child['error_code'] == ERROR and child['attempts'] == 1 and
         child['lease_owner'] is None and child['lease_until'] is None and child['external_publish_id'] is None,
         'EXACT_BLOCKED_LOOKUP_REQUIRED')
    need(len(schedules) == 1, 'EXACT_SCHEDULE_REQUIRED')
    schedule = schedules[0]
    need(schedule['active_child_id'] == LOOKUP and schedule['status'] == 'blocked' and schedule['polls_total'] == 1 and
         schedule['deadline'] > time.time() and schedule['intent_sha256'] == INTENT_SHA and
         schedule['binding_sha256'] == digest(binding) and json.loads(schedule['record']) == {'last_code': ERROR} and
         child['payload'] == child_payload(context, 1), 'EXACT_SCHEDULE_REQUIRED')
    ledger = store.query('SELECT * FROM ' + ATTEMPTS + ' WHERE child_job_id=? ORDER BY started_at,id', (LOOKUP,))
    need(len(ledger) == 1, 'ORIGINAL_LOOKUP_LEDGER_REQUIRED')
    return {'runtime': expected, 'child': child, 'schedule': schedule, 'ledger': ledger,
            'parent_digest': digest(parent), 'publication': publication, 'intent': intent,
            'group_digest': digest(store.get(GROUP))}


def apply(store, plan, refresh):
    """The caller holds existing video/moderation/scheduling OS locks."""
    from services.hub.db import encode
    checksum = digest(plan)
    need(plan['version'] == VERSION and plan['lookup_job_id'] == LOOKUP and plan['publish_job_id'] == PUBLISH,
         'EXACT_PLAN_REQUIRED')
    with store.transaction() as db:
        need(refresh() == plan['snapshot'], 'LOOKUP_PLAN_CHANGED')
        before = plan['snapshot']['child']
        row = db.execute('SELECT * FROM jobs WHERE job_id=?', (LOOKUP,)).fetchone()
        need(row is not None and row['state'] == 'awaiting_review' and row['attempts'] == 1 and
             row['error_code'] == ERROR and row['updated_at'] == before['updated_at'] and
             row['payload'] == encode(before['payload']) and row['result'] == encode(before['result']) and
             row['lease_owner'] is None and row['lease_until'] is None, 'LOOKUP_CAS_CHANGED')
        now = time.time()
        db.execute("UPDATE jobs SET state='retry_scheduled',next_run=?,updated_at=?,error_code=NULL WHERE job_id=?", (now, now, LOOKUP))
        db.execute("UPDATE replay_part_lookup_schedule_v5 SET status='waiting' WHERE parent_job_id=?", (PUBLISH,))
        result = {'version': VERSION, 'plan_sha256': checksum, 'lookup_job_id': LOOKUP,
                  'publish_job_id': PUBLISH, 'state': 'readonly_lookup_retry_scheduled',
                  'attempts_preserved': 1, 'parent_attempts_preserved': 2,
                  'publication_unchanged': True, 'submit_intent_unchanged': True,
                  'prior_attempt_ledger_preserved': True, 'provider_requests': 0,
                  'upload_or_submit_requests': 0, 'lease_created_by_operator': False, 'at': now}
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                   (now, 'operator:ssh-sudo', VERSION, LOOKUP, encode(result)))
    return result


def read_private(file, area):
    need(file.parent == area and file.resolve() == file and not file.is_symlink(), 'PLAN_PATH_CHANGED')
    with os.fdopen(os.open(file, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == 10001 and
             info.st_mode & 0o077 == 0 and 0 < info.st_size < 4_000_000, 'PRIVATE_PLAN_REQUIRED')
        return stream.read(info.st_size + 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-pins', required=True, help='JSON map from the accepted candidate qualification')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--plan-file', type=Path)
    parser.add_argument('--expected-plan-sha256')
    args = parser.parse_args()
    need(os.name == 'posix' and os.getuid() == os.getgid() == 10001 and __debug__, 'APP_IDENTITY_REQUIRED')
    need(args.apply == bool(args.plan_file and args.expected_plan_sha256), 'EXACT_PLAN_REQUIRED')
    os.umask(0o077)
    from services.hub.config import Settings
    from services.hub.db import Store
    from services.hub.locks import resource_guard
    settings = Settings.load()
    need(settings.root == Path('/opt/lighthouse-hub'), 'FIXED_APPLICATION_ROOT_REQUIRED')
    store = Store(settings.db_path)
    expected = json.loads(args.runtime_pins)
    area = settings.root / 'media' / PUBLISH / 'readonly-lookup-resume-v5'
    need(area.parent.resolve() == area.parent and not any(p.is_symlink() for p in area.parents), 'PLAN_PATH_CHANGED')
    area.mkdir(mode=0o700, exist_ok=True)
    need(not area.is_symlink() and area.stat().st_uid == 10001 and stat.S_IMODE(area.stat().st_mode) == 0o700, 'PRIVATE_PLAN_DIRECTORY_REQUIRED')
    with resource_guard(*(settings.root / 'data/locks' / name for name in ('video', 'moderation', 'part-lookup-schedule-v5'))):
        refresh = lambda: snapshot(settings, store, expected)
        if args.apply:
            raw = read_private(args.plan_file, area)
            need(hashlib.sha256(raw).hexdigest() == args.expected_plan_sha256, 'EXACT_PLAN_HASH_REQUIRED')
            plan = json.loads(raw)
            result = apply(store, plan, refresh)
            print(json.dumps(result))
        else:
            plan = {'version': VERSION, 'lookup_job_id': LOOKUP, 'publish_job_id': PUBLISH, 'snapshot': refresh()}
            raw = wire(plan)
            file = area / (digest(plan) + '.json')
            try:
                with os.fdopen(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
                    stream.write(raw); stream.flush(); os.fsync(stream.fileno())
            except FileExistsError:
                need(read_private(file, area) == raw, 'PLAN_FILE_CHANGED')
            fd = os.open(area, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(fd)
            finally: os.close(fd)
            print(json.dumps({'state': 'readonly_plan', 'plan_file': str(file), 'plan_sha256': digest(plan),
                              'lookup_job_id': LOOKUP, 'attempts': 1, 'provider_requests': 0, 'jobs_changed': 0}))


if __name__ == '__main__':
    main()
