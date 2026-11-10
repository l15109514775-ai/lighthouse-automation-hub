"""Restore a migration source ONLY before the target has ever been activated.

The coordinator must inspect the target over pinned SSH and supply a fresh
root-owned target-proof JSON. This is not a post-publication database rollback.
No database, jobs, attempts or publication records are written by this script.
Default is a dry run; --apply is an explicit operator action.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time


def checked_json(path):
    path = Path(path)
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('ROLLBACK_UNSAFE_PATH')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError('ROLLBACK_RECORD_PERMISSIONS')
    return json.loads(path.read_text())


def plan(record, proof, containers, now):
    if record.get('version') != 'lighthouse-source-fence-v1' or record.get('target_activated') is not False:
        raise ValueError('ROLLBACK_SOURCE_RECORD_INVALID')
    if record.get('phase') == 'source_restored':
        raise ValueError('ROLLBACK_ALREADY_RESTORED')
    if (proof.get('version') != 'lighthouse-target-quiescent-v1'
            or proof.get('target_ip') != record['target_ip']
            or proof.get('ever_activated') is not False
            or proof.get('all_containers_stopped') is not True
            or proof.get('owner_record_present') is not False
            or not 0 <= now - proof.get('checked_at', 0) <= 120):
        raise ValueError('ROLLBACK_TARGET_NOT_PROVEN_QUIESCENT')
    actual = {c['Id']: c for c in containers}
    commands, running = [], []
    for c in record['containers_before']:
        if not re.fullmatch(r'(?:lighthouse|subtitle)[a-zA-Z0-9_.-]*', c['name']):
            raise ValueError('ROLLBACK_CONTAINER_NAME_INVALID')
        current = actual.get(c['id'])
        if not current or current['Image'] != c['image'] or current['Name'].lstrip('/') != c['name']:
            raise ValueError('ROLLBACK_CONTAINER_IDENTITY_CHANGED')
        if not c['running'] and current['State']['Running']:
            raise ValueError('ROLLBACK_RETIRED_CONTAINER_RUNNING')
        rp = c['restart_policy']
        policy = rp['Name'] or 'no'
        if policy not in ('no', 'always', 'unless-stopped', 'on-failure'):
            raise ValueError('ROLLBACK_RESTART_POLICY_INVALID')
        count = rp.get('MaximumRetryCount', 0)
        if not isinstance(count, int) or count < 0:
            raise ValueError('ROLLBACK_RETRY_COUNT_INVALID')
        if policy == 'on-failure' and count:
            policy += ':' + str(count)
        commands.append(['docker', 'update', '--restart', policy, c['id']])
        if c['running']:
            running.append(c['name'])
    if not running:
        raise ValueError('ROLLBACK_NO_ORIGINAL_RUNNING_SERVICES')
    # Never run the original generic `compose up`: it would resurrect legacy
    # services which were deliberately stopped before migration.
    dropin = ('[Service]\nExecStart=\nExecStart=/usr/bin/docker start ' + ' '.join(running)
              + '\nExecStop=\nExecStop=/usr/bin/docker stop ' + ' '.join(running) + '\n')
    commands += [['systemctl', 'daemon-reload'], ['docker', 'start', *running]]
    for u in record['units_before']:
        name = u['name']
        if not re.fullmatch(r'lighthouse[a-zA-Z0-9_.-]*\.(?:service|timer)', name):
            raise ValueError('ROLLBACK_UNIT_NAME_INVALID')
        if u['enabled'] == 'enabled':
            commands.append(['systemctl', 'enable', name])
    for suffix in ('.service', '.timer'):
        for u in record['units_before']:
            if u['name'].endswith(suffix) and u['active'] == 'active':
                commands.append(['systemctl', 'start', u['name']])
    return commands, dropin, running


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', required=True, type=Path)
    parser.add_argument('--target-proof', required=True, type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    record, proof = checked_json(args.record), checked_json(args.target_proof)
    ids = [c['id'] for c in record['containers_before']]
    actual = json.loads(subprocess.check_output(['docker', 'inspect', *ids]))
    commands, dropin, running = plan(record, proof, actual, time.time())
    cron_restore = []
    for c in record['cron_before']:
        if not re.fullmatch(r'subtitle[a-zA-Z0-9_.-]*', c['name']):
            raise ValueError('ROLLBACK_CRON_NAME_INVALID')
        source = args.record.parent / 'frozen-cron' / c['name']
        target = Path('/etc/cron.d') / c['name']
        if source.is_symlink() or target.is_symlink():
            raise ValueError('ROLLBACK_CRON_SYMLINK')
        body = source.read_bytes()
        if hashlib.sha256(body).hexdigest() != c['sha256']:
            raise ValueError('ROLLBACK_CRON_COPY_CHANGED')
        if target.exists() and target.read_bytes() != body and not target.read_text().startswith('# Temporarily disabled for Lighthouse host migration.'):
            raise ValueError('ROLLBACK_CRON_DESTINATION_CHANGED')
        cron_restore.append((target, body, c))
    override = Path('/etc/systemd/system/lighthouse-hub.service.d/90-migration-rollback.conf')
    if any(p.is_symlink() for p in (override, *override.parents)) or (override.exists() and override.read_text() != dropin):
        raise ValueError('ROLLBACK_DROPIN_CONFLICT')
    if not args.apply:
        print(json.dumps({'dry_run': True, 'running_allowlist': running, 'command_count': len(commands), 'database_writes': False}))
        return
    os.umask(0o077)
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text(dropin)
    override.chmod(0o644)
    for command in commands:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
    for target, body, meta in cron_restore:
        target.write_bytes(body)
        os.chmod(target, meta['mode'])
        os.chown(target, meta['uid'], meta['gid'])
    after = json.loads(subprocess.check_output(['docker', 'inspect', *ids]))
    expected = {c['id']: c['running'] for c in record['containers_before']}
    if any(c['State']['Running'] != expected[c['Id']] for c in after):
        raise ValueError('ROLLBACK_FINAL_RUNNING_STATE_MISMATCH')
    record.update(phase='source_restored', restored_at=time.time(), database_restored=False)
    args.record.write_text(json.dumps(record, indent=2))
    print(json.dumps({'restored': True, 'running_allowlist': running, 'database_writes': False}))


if __name__ == '__main__':
    main()
