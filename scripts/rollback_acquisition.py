"""Stop only a positively identified acquisition container; never restore a DB.

Default is a read-only plan. Supply --apply after checking the printed plan.
An unrelated/legacy worker, changed image or changed data mount is rejected.
No credentials, Docker environment, source payloads or publication IDs are read.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess


def inspect_container(container):
    return json.loads(subprocess.check_output(['docker', 'inspect', container], text=True))[0]


def validate(info, expected_image_id, expected_data_root):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', expected_image_id):
        raise ValueError('ROLLBACK_IMAGE_ID_REQUIRED')
    labels = info['Config'].get('Labels') or {}
    if labels.get('com.docker.compose.service') != 'replay-acquisition-worker':
        raise ValueError('ROLLBACK_SERVICE_MISMATCH')
    if info['Config'].get('Cmd') != ['python', '-m',
            'services.hub.replay_multipart_worker_runtime_v4', '--acquisition-only']:
        raise ValueError('ROLLBACK_COMMAND_MISMATCH')
    if info['Image'] != expected_image_id:
        raise ValueError('ROLLBACK_IMAGE_CHANGED')
    data = [m for m in info['Mounts'] if m['Destination'] == '/opt/lighthouse-hub/data']
    root = Path(expected_data_root)
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise ValueError('ROLLBACK_DATA_PATH_INVALID')
    if len(data) != 1 or data[0]['Type'] != 'bind' or data[0]['Source'] != str(root):
        raise ValueError('ROLLBACK_DATA_MOUNT_CHANGED')
    return {'action': 'stop_acquisition_only', 'container_id': info['Id'],
            'image_id': info['Image'], 'data_root': str(root),
            'previous_status': info['State']['Status'], 'database_restore': False,
            'historical_workers_started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', required=True)
    parser.add_argument('--expected-image-id', required=True)
    parser.add_argument('--expected-data-root', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        info = inspect_container(args.container)
        plan = validate(info, args.expected_image_id, args.expected_data_root)
        if args.apply:
            # Address the immutable container ID, never a name that may be replaced.
            again = inspect_container(plan['container_id'])
            validate(again, args.expected_image_id, args.expected_data_root)
            subprocess.run(['docker', 'stop', '--time', '30', plan['container_id']],
                           check=True, stdout=subprocess.DEVNULL, timeout=45)
            final = inspect_container(plan['container_id'])
            if final['State']['Running'] or final['State'].get('Restarting'):
                raise ValueError('ROLLBACK_CONTAINER_STILL_RUNNING')
            plan['result'] = 'stopped_preserving_database_and_media'
        else:
            plan['result'] = 'plan_only'
        print(json.dumps(plan))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        # Fail closed; do not echo daemon responses that can include deployment data.
        raise SystemExit('ACQUISITION_ROLLBACK_REFUSED_OR_INCOMPLETE')


if __name__ == '__main__':
    main()
