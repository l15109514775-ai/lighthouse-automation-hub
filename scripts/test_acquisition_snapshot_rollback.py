"""Boot the qualified candidate and previous worker on one disposable, idle DB copy.

Linux Docker host only. The private fixture has no provider credentials, live
mounts or network. Only the final, value-free report is retained.
"""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid

if __package__:
    from .test_acquisition_overlay_bundle import SOURCE_BINDING_VERSIONS, read_bundle, safe_path
else:
    from test_acquisition_overlay_bundle import SOURCE_BINDING_VERSIONS, read_bundle, safe_path


APP = Path('/opt/lighthouse-hub')
TYPES = ('youtube_replay_download_v4', 'youtube_replay_plan_v4',
         'youtube_replay_readiness_v4', 'youtube_replay_refresh_v4')
CODE = ('services/hub/worker.py', 'services/hub/source_controls.py')
POLICIES = ('source-controls.json', 'replay-fifo-policy-v4.json',
            'whole-replay-planning-policy-v4.toml', 'youtube-whole-replay-download-policy-v4.toml')


def docker(*args, timeout=30):
    return subprocess.check_output(['docker', *map(str, args)], text=True, timeout=timeout).strip()


def ro(path):
    return closing(sqlite3.connect(safe_path(path).as_uri() + '?mode=ro', uri=True, timeout=5))


def fingerprint(path):
    """Hash typed rows as a multiset, including duplicates and all columns."""
    with ro(path) as db:
        db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or db.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('CLONE_INTEGRITY_FAILED')
        schema = db.execute('SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
        result = {}
        for kind, name, _, _sql in schema:
            if kind != 'table' or name == 'heartbeats':
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            digests = []
            for row in db.execute('SELECT * FROM ' + quoted):
                encoded = json.dumps(row, separators=(',', ':'), default=lambda blob: {'blob': blob.hex()})
                digests.append(hashlib.sha256(encoded.encode()).digest())
            result[name] = (len(digests), hashlib.sha256(b''.join(sorted(digests))).hexdigest())
        return schema, result


def idle_clone(path):
    with ro(path) as db:
        db.execute('BEGIN')
        states = ('pending', 'processing', 'retry_scheduled')
        rows = db.execute('SELECT count(*) FROM jobs WHERE job_type IN (?,?,?,?) AND state IN (?,?,?)',
                          (*TYPES, *states)).fetchone()[0]
        active = db.execute("SELECT count(*) FROM replay_fifo_v4 WHERE state NOT IN ('completed','existing','excluded','hold')").fetchone()[0]
        if rows or active:
            raise ValueError('SNAPSHOT_HAS_ACTIVE_ACQUISITION_WORK')


def boot(image, mounts, env, database, code_hashes):
    label = uuid.uuid4().hex
    name = 'lighthouse-offline-history-' + label
    args = ['run', '--detach', '--name', name, '--label', 'lighthouse.history-probe=' + label,
            '--pull=never', '--network=none', '--read-only', '--user', '10001:10001', '--cap-drop=ALL',
            '--security-opt=no-new-privileges:true', '--cpus=1', '--memory=700m', '--pids-limit=96',
            '--tmpfs', '/tmp:rw,size=96m,mode=1777']
    for destination, (source, writable) in mounts.items():
        args.extend(['--mount', f'type=bind,src={source},dst={destination}' + ('' if writable else ',readonly')])
    for assignment in env:
        args.extend(['-e', assignment])
    try:
        ident = docker(*args, image, 'python', '-m', 'services.hub.replay_multipart_worker_runtime_v4', '--acquisition-only')
        info = json.loads(docker('inspect', ident))[0]
        if info['Image'] != image or info['HostConfig']['NetworkMode'] != 'none':
            raise ValueError('PROBE_CONTAINER_MISMATCH')
        observed = {}
        for line in docker('exec', ident, 'sha256sum', *(APP / n for n in sorted(code_hashes))).splitlines():
            digest, path = line.split(maxsplit=1)
            observed[str(Path(path).relative_to(APP))] = digest
        if observed != code_hashes:
            raise ValueError('PROBE_ACTIVE_CODE_MISMATCH')
        first = None
        deadline = time.monotonic() + 105
        while time.monotonic() < deadline:
            state = json.loads(docker('inspect', '--format', '{{json .State}}', ident))
            if not state['Running']:
                raise RuntimeError(f"PROBE_WORKER_EXITED_{state['ExitCode']}")
            health = subprocess.run(['docker', 'exec', ident, 'python', '-m', 'services.hub.acquisition_health'],
                                    capture_output=True, timeout=10)
            if health.returncode == 0:
                epoch = json.loads(docker('exec', ident, 'cat', '/tmp/replay-acquisition-epoch.json'))
                with ro(database) as db:
                    beat = db.execute('SELECT at FROM heartbeats WHERE name=?',
                                      ('worker:replay-acquisition-producer:' + epoch['instance'],)).fetchone()[0]
                if first is None:
                    first = beat
                elif beat > first:
                    return epoch['instance']
            time.sleep(2)
        raise RuntimeError('PROBE_SECOND_PRODUCER_TICK_NOT_OBSERVED')
    finally:
        # A client command timing out does not imply its isolated process stopped.
        matches = docker('ps', '-aq', '--no-trunc', '--filter', 'label=lighthouse.history-probe=' + label).splitlines()
        for ident in matches:
            info = json.loads(docker('inspect', ident))[0]
            if info['Name'] != '/' + name or info['Image'] != image:
                raise ValueError('PROBE_CLEANUP_IDENTITY_MISMATCH')
            docker('rm', '--force', ident)


def qualify(args):
    manifest = json.loads(safe_path(args.manifest).read_bytes())
    if manifest['version'] not in SOURCE_BINDING_VERSIONS:
        raise ValueError('SOURCE_BINDING_MANIFEST_REQUIRED')
    bindings = read_bundle(manifest, safe_path(args.archive))
    live = json.loads(docker('inspect', args.container))[0]
    if (live['Image'] != manifest['source_image'] or live['State'].get('Health', {}).get('Status') != 'healthy'
            or live['Config']['Cmd'] != ['python', '-m', 'services.hub.replay_multipart_worker_runtime_v4', '--acquisition-only']):
        raise ValueError('PREVIOUS_RELEASE_MISMATCH')
    sources = {m['Destination']: safe_path(Path(m['Source'])) for m in live['Mounts'] if m['Type'] == 'bind'}
    env = [e for e in live['Config']['Env'] if e.split('=', 1)[0] in ('HUB_ROOT', 'HUB_CONFIG', 'DRY_RUN')
           or e.startswith('BUDGET_')]
    settings = dict(e.split('=', 1) for e in env)
    config = Path(settings['HUB_CONFIG'])
    if config.parent != APP / 'config' or config.suffix != '.toml' or settings['HUB_ROOT'] != str(APP):
        raise ValueError('UNEXPECTED_CONFIG_LOCATION')
    env.append('YOUTUBE_API_KEY_FILE=/run/secrets/youtube-api-key')
    ids_before = set(docker('ps', '--no-trunc', '--format', '{{.ID}}').splitlines())
    phases = {}
    with tempfile.TemporaryDirectory(prefix='lighthouse-history-', dir='/var/tmp') as scratch:
        root = Path(scratch)
        mounts = {}
        for folder in ('data', 'media', 'logs', 'backups'):
            dest = root / folder
            dest.mkdir(mode=0o700)
            os.chown(dest, 10001, 10001)
            mounts[APP / folder] = (dest, True)
        dbfile = root / 'data/hub.sqlite3'
        with ro(sources[str(APP / 'data')] / 'hub.sqlite3') as source, closing(sqlite3.connect(dbfile)) as copy:
            source.backup(copy)
        dbfile.chmod(0o600)
        os.chown(dbfile, 10001, 10001)
        idle_clone(dbfile)
        for n in (config.name, *POLICIES):
            source = safe_path(sources[str(APP / 'config')] / n)
            dest = root / n
            shutil.copyfile(source, dest)
            dest.chmod(0o444)
            mounts[APP / 'config' / n] = (dest, False)
        key = root / 'test-key'
        key.write_text('synthetic-test-only-key-00000000')
        key.chmod(0o400)
        os.chown(key, 10001, 10001)
        mounts[Path('/run/secrets/youtube-api-key')] = (key, False)
        previous_hashes = {}
        for n in CODE:
            dest = root / 'previous-code' / n
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(sources[str(APP / n)], dest)
            dest.chmod(0o444)
            mounts[APP / n] = (dest, False)
            previous_hashes[n] = hashlib.sha256(dest.read_bytes()).hexdigest()
        candidate_mounts = dict(mounts)
        candidate_hashes = dict(previous_hashes)
        for n, raw in bindings.items():
            dest = root / 'candidate-code' / n
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(raw)
            dest.chmod(0o444)
            candidate_mounts[APP / n] = (dest, False)
            candidate_hashes[n] = manifest['files'][n]['sha256']
        original_schema, original = fingerprint(dbfile)
        boots = []
        for phase, image, files, code in (('candidate', manifest['base_image'], candidate_mounts, candidate_hashes),
                                          ('rollback', manifest['source_image'], mounts, previous_hashes)):
            boots.append(boot(image, files, env, dbfile, code))
            schema, rows = fingerprint(dbfile)
            changes = sorted(k for k in rows.keys() | original.keys() if rows.get(k) != original.get(k))
            if schema != original_schema or changes:
                raise ValueError('CLONE_SCHEMA_OR_ROW_CHANGE: ' + ','.join(changes))
            phases[phase] = {'image': image, 'two_producer_ticks': True, 'all_compared_tables_unchanged': True,
                             'mounted_code_sha256': code}
        if boots[0] == boots[1]:
            raise ValueError('BOOT_ID_REUSED')
    if ids_before != set(docker('ps', '--no-trunc', '--format', '{{.ID}}').splitlines()):
        raise ValueError('RUNNING_CONTAINER_SET_CHANGED')
    return {'version': 'acquisition-history-rollback-v1', 'at': time.time(), 'phases': phases,
            'table_counts': {k: v[0] for k, v in original.items()}, 'excluded_tables': ['heartbeats'],
            'schema_unchanged': True, 'integrity': 'ok', 'foreign_key_violations': 0, 'scratch_removed': not root.exists(),
            'original_running_containers': len(ids_before), 'production_cutover': False, 'network': 'none'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='lighthouse-hub-replay-acquisition-worker-1')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--archive', required=True, type=Path)
    args = parser.parse_args()
    if os.name != 'posix' or os.geteuid() != 0:
        parser.error('Requires Linux root on the Docker host')
    print(json.dumps(qualify(args), indent=2))


if __name__ == '__main__':
    main()
