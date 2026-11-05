"""Verify or install the recurring replay source-revision reconciliation.

Default is verify-only. ``--install`` writes the pinned operator script and the two
systemd units (never enables them). ``--enable`` enables and starts the timer.
The operator is read from the checkout next to this installer; its byte hash must
match ``PIN`` so the deployed flow is exactly the reviewed one.
"""
import argparse
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path('/opt/lighthouse-hub')
TARGET = ROOT / 'backups/ops-replay-reconcile'
UNITS = Path('/etc/systemd/system')
CONTAINER = 'lighthouse-hub-video-worker-1'
NAME = 'reconcile_replay_source_revision_v1.py'
SERVICE = 'lighthouse-replay-source-reconcile.service'
TIMER = 'lighthouse-replay-source-reconcile.timer'
PIN = '2499837eafd75ed861e4451fa7609b832f198f77c36f7f2f6a1e2090205eca3a'
PLACEHOLDER = 'REPLACE_WITH_REVIEWED_SHA256'
HERE = Path(__file__).resolve().parent


def need(value, code):
    if not value:
        raise SystemExit(code)


def run(*args, check=True):
    return subprocess.run(args, check=check, text=True, capture_output=True)


def verify():
    source = HERE / NAME
    need(source.is_file(), 'SOURCE_MISSING')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    need(PIN != PLACEHOLDER, 'PIN_NOT_SET')
    need(digest == PIN, 'SOURCE_SHA_CHANGED')
    for unit in (SERVICE, TIMER):
        need((HERE / unit).is_file(), 'UNIT_MISSING:' + unit)
    need(run('docker', 'inspect', '--format', '{{.State.Running}}', CONTAINER).stdout.strip() == 'true',
         'CONTAINER_NOT_RUNNING')
    return {'version': 'install-replay-source-reconcile-v1', 'source_sha256': digest,
            'target': str(TARGET / NAME), 'service': SERVICE, 'timer': TIMER,
            'container': CONTAINER, 'installed': (TARGET / NAME).is_file(),
            'unit_present': (UNITS / SERVICE).is_file() and (UNITS / TIMER).is_file()}


def install():
    info = verify()
    TARGET.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.chown(TARGET, user=10001, group=10001)
    os.chmod(TARGET, 0o700)
    shutil.copyfile(HERE / NAME, TARGET / NAME)
    os.chmod(TARGET / NAME, 0o600)
    shutil.chown(TARGET / NAME, user=10001, group=10001)
    need(hashlib.sha256((TARGET / NAME).read_bytes()).hexdigest() == PIN, 'DEPLOYED_SHA_CHANGED')
    for unit in (SERVICE, TIMER):
        shutil.copyfile(HERE / unit, UNITS / unit)
        os.chmod(UNITS / unit, 0o644)
    need(run('docker', 'exec', '-u', '10001', CONTAINER, 'test', '-r', str(TARGET / NAME),
             check=False).returncode == 0, 'OPERATOR_NOT_VISIBLE_IN_CONTAINER')
    run('systemctl', 'daemon-reload')
    info.update(installed=True, unit_present=True)
    return info


def enable():
    run('systemctl', 'enable', '--now', TIMER)
    active = run('systemctl', 'is-active', TIMER).stdout.strip()
    need(active == 'active', 'TIMER_NOT_ACTIVE:' + active)
    return {'version': 'install-replay-source-reconcile-v1', 'timer_active': active}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--enable', action='store_true')
    args = parser.parse_args()
    need(os.getuid() == 0, 'ROOT_REQUIRED')
    result = enable() if args.enable else install() if args.install else verify()
    print(json_text(result))


def json_text(value):
    import json
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


if __name__ == '__main__':
    main()
