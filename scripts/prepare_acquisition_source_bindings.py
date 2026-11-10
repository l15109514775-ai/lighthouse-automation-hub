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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('manifest', 'archive', 'directory'):
        parser.add_argument('--' + option, required=True, type=Path)
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
    print(prepare(manifest, args.archive, args.directory))


if __name__ == '__main__':
    main()
