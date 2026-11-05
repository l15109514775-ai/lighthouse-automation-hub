"""Plan/apply the qualified V5 acquisition service; publication stays closed.

Only video-worker and its private configuration change. Existing jobs and
publications remain intact; only entirely unstarted FIFO rows may rebind.
"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import select
import subprocess
import time
import tomllib
import uuid
import xml.etree.ElementTree as ET

ROOT = Path('/opt/lighthouse-hub')
PEERS = ('api', 'scheduler', 'tweet-worker', 'system-worker')
OLD_IMAGE = 'sha256:6efc69d170d35e81cbf68a03ff2d10aeb62c6771e39b8b7424491d890bd0fc30'
ENTRY = 'services.hub.replay_parts_worker_runtime_v5'
TESTS = {
    'tests/test_replay_parts_v5.py', 'tests/test_replay_parts_runtime_v5.py',
    'tests/test_replay_part_registry_v5.py', 'tests/test_replay_part_order_v5.py',
    'tests/test_replay_part_moderation_v5.py', 'tests/test_replay_part_native_v5.py',
    'tests/test_replay_fifo_v4.py', 'tests/test_replay_multipart_worker_runtime_v4.py',
    'tests/test_replay_multipart_v4.py', 'tests/test_replay_delivery_v5.py',
    'tests/test_youtube_download_v4.py', 'tests/test_youtube_readiness_v4.py',
    'tests/test_verified_track_release_v4.py', 'tests/test_replay_native_driver.py',
    'tests/test_replay_later_native_result_v2.py',
}
FLAGS = {'youtube_whole_replay_download_enabled': True,
         'youtube_natural_download_enabled': False}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def run(args, **kw):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kw)


def safe(file):
    assert file.is_absolute() and file.resolve() == file
    assert not any(p.is_symlink() for p in (file, *file.parents))
    return file


def write(file, raw, mode=0o640):
    safe(file)
    temp = file.with_name('.' + file.name + '.' + str(uuid.uuid4()) + '.tmp')
    with temp.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temp, mode)
    os.chown(temp, 0, 10001)
    os.replace(temp, file)
    fd = os.open(file.parent, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def toml_bytes(value):
    assert all(type(v) in (str, bool, int, list) for v in value.values())
    text = '\n'.join(k + ' = ' + (
        'true' if v is True else 'false' if v is False else json.dumps(v, ensure_ascii=False)
    ) for k, v in value.items()) + '\n'
    assert tomllib.loads(text) == value
    return text.encode()


def shadow_hub(raw):
    text = raw.decode('utf-8-sig')
    before = tomllib.loads(text)
    lines = text.splitlines(keepends=True)
    boundary = next((i for i, line in enumerate(lines) if line.lstrip().startswith('[')), len(lines))
    head, tail = lines[:boundary], lines[boundary:]
    for key, value in FLAGS.items():
        selected = [i for i, line in enumerate(head) if re.match(r'^\s*' + key + r'\s*=', line)]
        assert len(selected) <= 1
        replacement = key + ' = ' + ('true' if value else 'false') + '\n'
        if selected:
            head[selected[0]] = replacement
        else:
            if head and not head[-1].endswith('\n'):
                head[-1] += '\n'
            head.append(replacement)
    result = ''.join(head + tail).encode()
    assert tomllib.loads(result.decode()) == {**before, **FLAGS}
    return result


def policies(now):
    config = ROOT / 'config'
    old = tomllib.loads((config / 'youtube-highest-download-policy.toml').read_text())
    planning = tomllib.loads((config / 'whole-replay-planning-policy-v4.example.toml').read_text())
    planning.update(enabled=True, raw_track_release_after_verified_merge=True,
                    prefer_single_whole_passthrough_v5=True)
    assert planning['disk_floor_bytes'] == 15 * 1024**3
    assert planning['max_parts'] == 32
    canonical = json.dumps(planning, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    # Reuse actual historical rights, risk and login references; do not invent
    # timestamps or claim that a technical acceptance is a human review.
    refs = {key: old[key] for key in (
        'owner_permission_reference', 'acquisition_authorization_reference',
        'quality_authorization_reference', 'owner_risk_acceptance_reference',
        'session_authorization_reference', 'owner_risk_accepted_at', 'session_authorized_at')}
    acquisition = {
        'version': 'youtube-whole-replay-download-policy-v4', 'enabled': True,
        'source_scope': 'all_eligible_ended_since_operating_cutoff',
        'planning_policy_sha256': sha(canonical), 'session_profile': 'youtube',
        'acquisition_mode': 'owner_accepted_platform_terms_risk',
        'allowed_channel_ids': sorted(('UCbbbbbbbbbbbbbbbbbbbbbb', 'UCaaaaaaaaaaaaaaaaaaaaaa')),
        'operating_not_before': '2026-10-31T16:00:00Z',
        'daily_http_requests': 6000, 'monthly_http_requests': 150000, 'timeout_seconds': 86400,
        **refs,
    }
    fifo_file = config / 'replay-fifo-policy-v4.json'
    fifo = json.loads(fifo_file.read_bytes())
    old_fifo_sha = sha(fifo_file.read_bytes())
    fifo.update(planning_policy_sha256=sha(canonical), activated_at=now)
    return {
        'video-runtime-v5.toml': shadow_hub((config / 'hub.toml').read_bytes()),
        'whole-replay-planning-policy-v4.toml': toml_bytes(planning),
        'youtube-whole-replay-download-policy-v4.toml': toml_bytes(acquisition),
        'replay-fifo-policy-v4.json': (json.dumps(fifo, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode(),
    }, old_fifo_sha


def inspect(service):
    cid = run(['docker', 'compose', '-f', str(ROOT / 'compose.yaml'), 'ps', '-q', service]).stdout.strip()
    assert re.fullmatch('[a-f0-9]{64}', cid)
    return json.loads(run(['docker', 'inspect', cid]).stdout)[0]


def qualification(file, checksum):
    safe(file)
    raw = file.read_bytes()
    assert sha(raw) == checksum
    q = json.loads(raw)
    assert q['version'] == 'tracked-source-candidate-ubuntu-result-v1'
    assert q['passed'] is True and q['network'] == 'none' and q['exit_code'] == 0
    assert all(q[k] is False for k in ('production_data_mounted', 'secrets_injected', 'oom_killed', 'timed_out'))
    assert q['failure'] == q['error'] == q['skipped'] == 0
    assert set(q['source_manifest']['test_selections']) == TESTS
    junit = safe(Path(q['junit_file'])).read_bytes()
    assert sha(junit) == q['junit_sha256']
    cases = list(ET.fromstring(junit).iter('testcase'))
    assert len(cases) == q['tests'] and len(cases) >= 200
    assert all(not any(c.find(t) is not None for t in ('error', 'failure', 'skipped')) for c in cases)
    assert run(['docker', 'image', 'inspect', '-f', '{{.Id}}', q['image']]).stdout.strip() == q['image']
    return q


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qualification', required=True)
    parser.add_argument('--qualification-sha256', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    assert os.geteuid() == 0
    q = qualification(Path(args.qualification), args.qualification_sha256)
    compose = safe(ROOT / 'compose.yaml')
    raw = compose.read_bytes()
    before = json.loads(raw)
    svc = before['services']['video-worker']
    assert svc['image'] == OLD_IMAGE
    assert svc['command'] == ['python', '-m', 'services.hub.replay_multipart_worker_runtime_v4', '--register-only']
    assert len(svc['volumes']) == 8
    assert all(not m['target'].startswith(str(ROOT) + '/services/') for m in svc['volumes'])
    now = datetime.now(timezone.utc).isoformat()
    files, previous_fifo = policies(now)
    # Examples come from this qualified source image, copied by the operator
    # before running this script. They contain no credentials.
    after = deepcopy(before)
    svc = after['services']['video-worker']
    svc['image'] = q['image']
    svc['command'] = ['python', '-m', ENTRY, '--acquisition-only']
    assert isinstance(svc['environment'], dict)
    svc['environment']['HUB_CONFIG'] = str(ROOT / 'config/video-runtime-v5.toml')
    peers = {name: inspect(name) for name in PEERS}
    old = inspect('video-worker')
    assert old['Image'] == OLD_IMAGE
    report = {'version': 'replay-parts-acquisition-v5-deploy', 'applied': False,
              'mode': 'acquisition_only', 'publication_enabled': False,
              'source_commit': q['source_commit'], 'image': q['image'],
              'qualification': args.qualification, 'qualification_sha256': args.qualification_sha256,
              'tests': q['tests'], 'config_sha256': {k: sha(v) for k, v in files.items()},
              'compose_before_sha256': sha(raw), 'old_fifo_policy_sha256': previous_fifo}
    if not args.apply:
        print(json.dumps(report)); return
    folder = ROOT / 'backups' / ('replay-parts-acquisition-v5-' + str(uuid.uuid4()))
    folder.mkdir(mode=0o700)
    write(folder / 'compose.before.json', raw, 0o600)
    original_files = {name: (ROOT / 'config' / name).read_bytes() if (ROOT / 'config' / name).exists() else None for name in files}
    for name, content in original_files.items():
        if content is not None: write(folder / (name + '.before'), content, 0o600)
    holder = None
    activated = None
    activation_attempted = False
    check = """import json,sqlite3,sys;from pathlib import Path
from services.hub.locks import resource_guard
r=Path('/opt/lighthouse-hub');c=sqlite3.connect((r/'data/hub.sqlite3').as_uri()+'?mode=ro',uri=True)
with resource_guard(r/'data/locks/video',r/'data/locks/moderation'):
 assert not c.execute(\"SELECT 1 FROM jobs WHERE queue IN ('video','moderation') AND state='processing'\").fetchone()
 print(json.dumps({'idle_locked':True}),flush=True);sys.stdin.buffer.read(1)
"""
    try:
        holder = subprocess.Popen(['docker', 'exec', '--user', '10001:10001', '-i', old['Id'], 'python', '-c', check],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert select.select([holder.stdout], [], [], 20)[0]
        assert json.loads(holder.stdout.readline()) == {'idle_locked': True}
        run(['docker', 'compose', '-f', str(compose), 'stop', '-t', '30', 'video-worker'], timeout=50)
        holder.stdin.close(); holder.wait(timeout=15)
        for name, content in files.items(): write(ROOT / 'config' / name, content)
        write(compose, (json.dumps(after, ensure_ascii=False, indent=2)+'\n').encode())
        rebind = """import json,os
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.replay_fifo_v4 import ReplayFIFOV4
s=Settings.load();d=Store(s.db_path)
r=ReplayFIFOV4(s,d).rebind_unstarted_candidates(expected_previous_policy_sha256=os.environ['PRIOR_FIFO_POLICY_SHA'],maintenance_reference='Owner: 修改队列，并继续支持更长或更大的录播; enable qualified two-copy and standalone delivery; entirely unstarted rows only')
print(json.dumps(r))
"""
        output = run(['docker', 'compose', '-f', str(compose), 'run', '--rm', '--no-deps', '-T',
                      '-e', 'PRIOR_FIFO_POLICY_SHA=' + previous_fifo, 'video-worker', 'python', '-c', rebind], timeout=60)
        rebound = json.loads(output.stdout.strip().splitlines()[-1])
        assert rebound['job_rows_changed'] == rebound['publication_rows_changed'] == rebound['source_rows_changed'] == 0
        write(folder / 'rebind-result.json', output.stdout.encode())
        run(['docker', 'compose', '-f', str(compose), 'run', '--rm', '--no-deps', '-T', 'video-worker',
             'python', '-m', ENTRY, '--acquisition-only', '--check'], timeout=60)
        keycheck = """import os
from services.hub.replay_worker_runtime import load_youtube_key
assert os.environ.get('YOUTUBE_API_KEY_FILE')=='/run/secrets/youtube-api-key'
load_youtube_key('/run/secrets/youtube-api-key')
assert os.environ.get('YOUTUBE_API_KEY')
print('youtube_key_readable_without_disclosure')
"""
        assert run(['docker', 'compose', '-f', str(compose), 'run', '--rm', '--no-deps', '-T',
                    'video-worker', 'python', '-c', keycheck], timeout=30).stdout.strip() == 'youtube_key_readable_without_disclosure'
        activation_attempted = True
        run(['docker', 'compose', '-f', str(compose), 'up', '-d', '--no-deps', '--pull', 'never', 'video-worker'], timeout=120)
        current = inspect('video-worker')
        assert current['State']['Running'] and current['Image'] == q['image']
        activated = current['Id']
        started = datetime.fromisoformat(current['State']['StartedAt'].replace('Z', '+00:00')).timestamp()
        beatcheck = """import json,sqlite3,time
c=sqlite3.connect('file:/opt/lighthouse-hub/data/hub.sqlite3?mode=ro',uri=True);c.execute('PRAGMA query_only=ON')
r=dict(c.execute(\"SELECT name,at FROM heartbeats WHERE name IN ('worker:video-producer-v5','worker:video-main-v5')\"))
print(json.dumps(r))
"""
        first = None
        deadline = time.monotonic() + 100
        while True:
            observed = json.loads(run(['docker', 'exec', '--user', '10001:10001', activated,
                                       'python', '-c', beatcheck], timeout=15).stdout)
            fresh = len(observed) == 2 and all(started <= v <= time.time()+1 for v in observed.values())
            if fresh and first is not None and all(observed[k] > first[k] for k in first):
                last_beats = deepcopy(observed)
                break
            if fresh and first is None:
                first = observed
            assert time.monotonic() < deadline, 'new runtime did not advance both real heartbeats'
            time.sleep(2)
        for name, prior in peers.items():
            peer_now = inspect(name)
            assert peer_now['Id'] == prior['Id'] and peer_now['State']['StartedAt'] == prior['State']['StartedAt']
        report.update(applied=True, new_video_container_id=current['Id'],
                      compose_after_sha256=sha(compose.read_bytes()), peer_containers_unchanged=True,
                      rebound_candidate_ids=rebound['rebound'], rollback_folder=str(folder),
                      youtube_key_readable_without_disclosure=True,
                      runtime_heartbeats_before=first, runtime_heartbeats_after=last_beats)
        write(folder / 'deployment-result.json', (json.dumps(report, indent=2)+'\n').encode())
        print(json.dumps({'report': str(folder / 'deployment-result.json'), 'applied': True,
                          'mode': 'acquisition_only', 'publication_enabled': False}))
    except BaseException:
        if holder and holder.poll() is None:
            holder.terminate(); holder.wait(timeout=15)
        # Once acquisition may have begun, never stop a busy new worker merely
        # because a later observation/report failed. Its durable job must run
        # or be reconciled with its real lease intact.
        if activation_attempted and not activated:
            try:
                found = inspect('video-worker')
                if found['Image'] == q['image'] and found['State']['Running']:
                    activated = found['Id']
                else:
                    assert found['Image'] == OLD_IMAGE or not found['State']['Running']
            except BaseException:
                write(folder / 'failure.json', json.dumps({'state': 'activation_result_unknown_preserved_requires_reconciliation',
                      'publication_enabled': False}).encode())
                raise
        if activated:
            recovery = None
            try:
                recovery = subprocess.Popen(['docker', 'exec', '--user', '10001:10001', '-i', activated,
                                              'python', '-c', check], stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                assert select.select([recovery.stdout], [], [], 10)[0]
                assert json.loads(recovery.stdout.readline()) == {'idle_locked': True}
                run(['docker', 'compose', '-f', str(compose), 'stop', '-t', '30', 'video-worker'], timeout=50)
            except BaseException:
                if recovery and recovery.poll() is None:
                    recovery.terminate(); recovery.wait(timeout=15)
                write(folder / 'failure.json', json.dumps({'state': 'active_acquisition_preserved_requires_reconciliation',
                      'new_video_container_id': activated, 'publication_enabled': False}).encode())
                raise
            finally:
                if recovery and recovery.poll() is None:
                    recovery.stdin.close(); recovery.wait(timeout=15)
        # Keep the new consistent FIFO policy if a rebind already happened;
        # registration-only can consume it without acquiring or publishing.
        restored = deepcopy(before)
        write(compose, (json.dumps(restored, ensure_ascii=False, indent=2)+'\n').encode())
        run(['docker', 'compose', '-f', str(compose), 'up', '-d', '--no-deps', '--pull', 'never', 'video-worker'], timeout=120)
        write(folder / 'failure.json', json.dumps({'state': 'registration_only_restored',
              'new_policy_and_additive_rows_preserved': True, 'publication_enabled': False}).encode())
        raise


if __name__ == '__main__':
    main()
