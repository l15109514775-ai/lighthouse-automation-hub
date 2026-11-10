"""Materialize exact locked upstream bytes after checking out pinned submodules.

The original lock records CRLF for some vendor text. Git checks these out as LF
on Linux. Recover only the exact byte representation whose SHA-256 is already
locked; never replace the lock or accept a changed source. No downloads occur.
"""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def materialize(root=ROOT):
    manifest = json.loads((root / 'config/upstream-lock.json').read_text('utf-8'))
    changed = 0
    for folder, entry in manifest.items():
        directory = root / folder
        if folder not in ('compositor', 'vendor/subtitle-cloud-worker') or directory.resolve() != directory:
            raise RuntimeError('UPSTREAM_PATH_INVALID')
        commit = subprocess.check_output(['git', '-C', str(directory), 'rev-parse', 'HEAD'], text=True).strip()
        if commit != entry['commit']:
            raise RuntimeError('UPSTREAM_COMMIT_MISMATCH')
        for relative, expected in entry['files'].items():
            path = directory / relative
            if path.resolve() != path or '..' in Path(relative).parts or Path(relative).is_absolute():
                raise RuntimeError('UPSTREAM_PATH_INVALID')
            if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected:
                continue
            blob = subprocess.check_output(['git', '-C', str(directory), 'show', commit + ':' + relative])
            variants = [blob]
            try:
                blob.decode('utf-8')
                if b'\0' not in blob:
                    lf = blob.replace(b'\r\n', b'\n')
                    variants += [lf, lf.replace(b'\n', b'\r\n')]
            except UnicodeDecodeError:
                pass
            matched = next((v for v in variants if hashlib.sha256(v).hexdigest() == expected), None)
            if matched is None or path.exists() and path.read_bytes() not in variants:
                raise RuntimeError('UPSTREAM_INTEGRITY_MISMATCH')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(matched)
            changed += 1
    return changed


if __name__ == '__main__':
    print(json.dumps({'locked_files_materialized': materialize()}))
