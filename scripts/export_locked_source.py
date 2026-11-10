"""Export committed source plus exact locked upstream bytes without a network.

This portable build context avoids depending on a missing public submodule ref.
It does not qualify a new image, publish source, export Git history, include live
configuration, or update any lock. Destination must be a new absolute .tar.gz path.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tarfile


def git(root, *args):
    # A Windows user's autocrlf preference must not change release tar bytes.
    return subprocess.check_output(['git', '-C', str(root), '-c', 'core.autocrlf=false',
                                    '-c', 'core.eol=lf', '-c', 'core.attributesFile=' + os.devnull,
                                    *args], stderr=subprocess.PIPE)


def checked_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
            or ':' in name or name != str(path)):
        raise ValueError('SOURCE_ARCHIVE_PATH_UNSAFE')
    if '.git' in path.parts or any(part in ('secrets', 'browser-profiles') for part in path.parts):
        raise ValueError('SOURCE_ARCHIVE_PRIVATE_PATH')
    if path.name == '.env' or (path.name.startswith('.env.') and not path.name.endswith(('.example', '.sample'))):
        raise ValueError('SOURCE_ARCHIVE_PRIVATE_PATH')
    if path.parts[0] in ('data', 'media', 'logs', 'backups', 'work'):
        raise ValueError('SOURCE_ARCHIVE_PRIVATE_PATH')
    if path.parts[0] == 'config' and path.suffix in ('.toml', '.json'):
        if '.example.' not in path.name and path.name not in (
                'upstream-lock.json', 'browser-seccomp.json', 'browser-seccomp.upstream.json'):
            raise ValueError('SOURCE_ARCHIVE_LIVE_CONFIG')
    return name


def locked_blob(root, folder, commit, relative, expected):
    checked_name(folder + '/' + relative)
    blob = git(root / folder, 'show', commit + ':' + relative)
    variants = [blob]
    try:
        blob.decode('utf-8')
        if b'\0' not in blob:
            lf = blob.replace(b'\r\n', b'\n')
            variants += [lf, lf.replace(b'\n', b'\r\n')]
    except UnicodeError:
        pass
    for candidate in variants:
        if hashlib.sha256(candidate).hexdigest() == expected:
            return candidate
    raise ValueError('LOCKED_SOURCE_HASH_MISMATCH')


def export(root, destination, revision='HEAD'):
    root, destination = Path(root), Path(destination)
    if not destination.is_absolute() or not destination.name.endswith('.tar.gz'):
        raise ValueError('ABSOLUTE_TAR_GZ_DESTINATION_REQUIRED')
    if destination.exists() or destination.is_symlink() or any(p.is_symlink() for p in destination.parents):
        raise ValueError('SOURCE_DESTINATION_UNSAFE_OR_EXISTS')
    commit = git(root, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    archive = git(root, 'archive', '--format=tar', commit)
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as source:
        for item in source:
            if item.isdir():
                continue
            name = checked_name(item.name)
            if not item.isfile() or name in entries:
                raise ValueError('SOURCE_ARCHIVE_NONREGULAR_OR_DUPLICATE')
            entries[name] = (source.extractfile(item).read(), item.mode & 0o777)
    lock = json.loads(entries['config/upstream-lock.json'][0])
    if set(lock) != {'compositor', 'vendor/subtitle-cloud-worker'}:
        raise ValueError('UPSTREAM_LOCK_SCOPE_INVALID')
    upstream = {}
    for folder, entry in lock.items():
        pinned = entry['commit']
        tree = git(root, 'ls-tree', commit, '--', folder).decode().split()
        if len(tree) != 4 or tree[:2] != ['160000', 'commit'] or tree[2] != pinned:
            raise ValueError('UPSTREAM_GITLINK_MISMATCH')
        for relative, expected in entry['files'].items():
            name = folder + '/' + relative
            if name in entries:
                raise ValueError('UPSTREAM_ARCHIVE_COLLISION')
            mode = git(root / folder, 'ls-tree', pinned, '--', relative).decode().split()
            if len(mode) != 4 or mode[0] not in ('100644', '100755') or mode[1] != 'blob':
                raise ValueError('UPSTREAM_NONREGULAR_FILE')
            entries[name] = (locked_blob(root, folder, pinned, relative, expected), int(mode[0][-3:], 8))
        upstream[folder] = {'commit': pinned, 'locked_files': len(entry['files'])}
    manifest = {'version': 'lighthouse-locked-source-v1', 'commit': commit, 'upstream': upstream,
                'image_qualified': False, 'includes_live_configuration': False,
                'files': {name: hashlib.sha256(data).hexdigest() for name, (data, _) in sorted(entries.items())}}
    manifest_name = 'BUILD_SOURCE_MANIFEST.json'
    if manifest_name in entries:
        raise ValueError('SOURCE_MANIFEST_COLLISION')
    entries[manifest_name] = ((json.dumps(manifest, sort_keys=True, indent=2) + '\n').encode(), 0o644)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidental replacement of a previous release.
    # A failed export keeps its partial artifact; never mistake it for a release.
    with destination.open('xb') as file:
        with gzip.GzipFile(filename='', mode='wb', fileobj=file, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as output:
                for name, (data, mode) in sorted(entries.items()):
                    item = tarfile.TarInfo(name)
                    item.size, item.mode, item.mtime = len(data), mode, 0
                    output.addfile(item, io.BytesIO(data))
    with destination.open('rb') as file:
        checksum = hashlib.file_digest(file, 'sha256').hexdigest()
    return {'version': manifest['version'], 'commit': commit, 'upstream': upstream,
            'sha256': checksum, 'bytes': destination.stat().st_size, 'image_qualified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--revision', default='HEAD')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(export(args.root, args.output, args.revision), sort_keys=True))
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError, tarfile.TarError):
        # No subprocess stderr, source bytes or credential-bearing paths in logs.
        raise SystemExit('LOCKED_SOURCE_EXPORT_FAILED') from None
