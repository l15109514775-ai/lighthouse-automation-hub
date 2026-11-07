#!/usr/bin/env python3
"""Read-only check that every worker process started after its config file changed.

This does not restart anything. It writes a JSON report and, when stale, an alert
file plus a systemd journal line. Run on the host as root (needs docker inspect).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = 'worker-config-freshness-v1'
ROOT = Path(os.environ.get('HUB_ROOT', '/opt/lighthouse-hub'))
TARGETS = (
    ('lighthouse-hub-tweet-worker-1', 'tweet-worker'),
    ('lighthouse-hub-scheduler-1', 'scheduler'),
    ('lighthouse-hub-video-worker-1', 'video-worker'),
)
STALE_GRACE_SECONDS = 60


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def main():
    checked_at = time.time()
    workers = []
    stale = []
    for container, label in TARGETS:
        entry = {'container': container, 'label': label}
        try:
            env_raw = docker('inspect', container, '--format', '{{json .Config.Env}}')
            env = dict(item.split('=', 1) for item in json.loads(env_raw) if '=' in item)
            state_raw = docker('inspect', container, '--format', '{{.State.Status}}|{{.State.StartedAt}}')
            status, started_raw = state_raw.split('|', 1)
            config = env.get('HUB_CONFIG')
            entry.update(status=status, config=config, started_at=started_raw)
            if status != 'running':
                entry['error'] = 'NOT_RUNNING'
            elif not config:
                entry['error'] = 'HUB_CONFIG_MISSING'
            else:
                mounts = json.loads(docker('inspect', container, '--format', '{{json .Mounts}}'))
                source = config
                for mount in mounts:
                    destination = mount.get('Destination', '').rstrip('/')
                    if mount.get('Type') == 'bind' and (config == destination or config.startswith(destination + '/')):
                        source = mount['Source'] + config[len(destination):]
                        break
                entry['config_source'] = source
                config_file = Path(source)
                started = datetime.fromisoformat(started_raw.replace('Z', '+00:00')).timestamp()
                mtime = config_file.stat().st_mtime
                delta = mtime - started
                entry.update(config_mtime=datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                             delta_seconds=round(delta, 3), stale=delta > STALE_GRACE_SECONDS)
                if entry['stale']:
                    stale.append(entry)
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            entry['error'] = type(exc).__name__
            stale.append(entry)
        workers.append(entry)

    report = {
        'version': VERSION,
        'checked_at': datetime.fromtimestamp(checked_at, timezone.utc).isoformat(),
        'checked_at_beijing': datetime.fromtimestamp(
            checked_at, timezone(timedelta(hours=8))).isoformat(),
        'stale_grace_seconds': STALE_GRACE_SECONDS,
        'workers': workers,
        'stale': [{'container': item['container'], 'delta_seconds': item.get('delta_seconds')} for item in stale],
    }
    folder = ROOT / 'data' / 'ops-maintenance'
    folder.mkdir(parents=True, exist_ok=True)
    report_path = folder / 'worker-config-freshness.json'
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n', 'utf-8')
    os.chmod(report_path, 0o600)
    if stale:
        alert_path = folder / 'worker-config-freshness.alert'
        alert_path.write_text(datetime.now(timezone.utc).isoformat() + '\n', 'utf-8')
        os.chmod(alert_path, 0o600)
        names = ','.join(item['container'] for item in stale)
        subprocess.run(['logger', '-t', 'lighthouse-worker-config-freshness', '-p', 'user.err',
                        'stale worker config: ' + names], check=False)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
