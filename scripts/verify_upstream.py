from pathlib import Path
import hashlib
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / 'config/upstream-lock.json'


def verify():
    manifest = json.loads(LOCK.read_text('utf-8'))
    checked = 0
    for folder, entry in manifest.items():
        for relative, expected in entry['files'].items():
            path = ROOT / folder / relative
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError(f'UPSTREAM_INTEGRITY_MISMATCH: {folder}/{relative}')
            checked += 1
    return checked


if __name__ == '__main__':
    if '--record-reviewed-lock' in sys.argv:
        manifest = {}
        for folder in ('compositor','vendor/subtitle-cloud-worker'):
            path = ROOT / folder
            commit = subprocess.check_output(['git','-C',str(path),'rev-parse','HEAD'],text=True).strip()
            tracked = subprocess.check_output(['git','-C',str(path),'ls-files','-z']).decode('utf-8').split('\0')
            manifest[folder] = {'commit':commit, 'files':{
                name:hashlib.sha256((path / name).read_bytes()).hexdigest() for name in tracked if name}}
        LOCK.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n','utf-8')
    print(json.dumps({'upstream_files_verified':verify()}))
