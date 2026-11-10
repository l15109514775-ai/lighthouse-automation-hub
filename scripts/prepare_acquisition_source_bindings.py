"""Prepare an acquisition-only Compose override from a reviewed private release.

Run on the Docker host with the signed-off manifest and the matching private tar.
The output must be a new directory. No Docker service is started or stopped.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess

if __package__:
    from .test_acquisition_overlay_bundle import SOURCE_BINDING_VERSIONS, read_bundle, safe_path
else:
    from test_acquisition_overlay_bundle import SOURCE_BINDING_VERSIONS, read_bundle, safe_path


def prepare(manifest, archive, directory):
    if manifest['version'] not in SOURCE_BINDING_VERSIONS:
        raise ValueError('ACQUISITION_SOURCE_RELEASE_REQUIRED')
    blobs = read_bundle(manifest, safe_path(archive))
    safe_path(directory)
    directory.mkdir(mode=0o700)
    volumes = []
    for name, content in sorted(blobs.items()):
        file = directory / name
        file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with file.open('xb') as stream:
            stream.write(content)
        file.chmod(0o444)
        volumes.append({'type': 'bind', 'source': str(file),
                        'target': '/opt/lighthouse-hub/' + name, 'read_only': True,
                        'bind': {'create_host_path': False}})
    override = {'services': {'replay-acquisition-worker': {
        'image': manifest['base_image'], 'pull_policy': 'never', 'volumes': volumes,
    }}}
    # Write the consumable declaration only after all bound files are ready.
    path = directory / 'candidate.compose.json'
    with path.open('x', encoding='utf-8') as stream:
        json.dump(override, stream, indent=2)
        stream.write('\n')
    return path


def stage_installed_override(previous, candidate, source_image):
    """Build the two-file systemd deployment's replacement and exact fallback."""
    original_bytes = safe_path(previous).read_bytes()
    document = json.loads(original_bytes)
    service = document['services']['replay-acquisition-worker']
    if service['image'] != source_image or service['command'] != [
        'python', '-m', 'services.hub.replay_multipart_worker_runtime_v4', '--acquisition-only'
    ]:
        raise ValueError('INSTALLED_ACQUISITION_RELEASE_MISMATCH')
    addition = json.loads(candidate.read_bytes())['services']['replay-acquisition-worker']
    pending = {v['target']: v for v in addition['volumes']}
    volumes = []
    seen = set()
    for mount in service['volumes']:
        if not isinstance(mount, dict) or not isinstance(mount.get('target'), str) or mount['target'] in seen:
            raise ValueError('INSTALLED_ACQUISITION_MOUNT_AMBIGUOUS')
        target = mount['target']
        seen.add(target)
        if target in pending:
            if mount['type'] != 'bind' or mount.get('read_only') is not True:
                raise ValueError('INSTALLED_ACQUISITION_CODE_MOUNT_INVALID')
            volumes.append(pending.pop(target))
        else:
            volumes.append(mount)
    service.update(image=addition['image'], pull_policy=addition['pull_policy'], volumes=volumes + list(pending.values()))
    folder = candidate.parent
    with (folder / 'installed-previous.compose.json').open('xb') as fallback:
        fallback.write(original_bytes)
    replacement = folder / 'installed-candidate.compose.json'
    with replacement.open('x', encoding='utf-8') as output:
        json.dump(document, output, indent=2)
        output.write('\n')
    return replacement


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('manifest', 'archive', 'directory'):
        parser.add_argument('--' + option, required=True, type=Path)
    parser.add_argument('--installed-override', type=Path,
                        help='Stage a replacement and exact fallback for a private acquisition JSON declaration')
    args = parser.parse_args()
    manifest = json.loads(safe_path(args.manifest).read_bytes())
    if manifest['version'] not in SOURCE_BINDING_VERSIONS:
        parser.error('Expected an acquisition source-binding manifest')
    image = manifest['base_image']
    if not isinstance(image, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        parser.error('Expected a local immutable image ID')
    actual = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
                                     text=True, timeout=30).strip()
    if actual != image:
        parser.error('Local image differs from the qualified base image')
    candidate = prepare(manifest, args.archive, args.directory)
    if args.installed_override:
        candidate = stage_installed_override(args.installed_override, candidate, manifest['source_image'])
    print(candidate)


if __name__ == '__main__':
    main()
