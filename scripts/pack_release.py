from pathlib import Path
import argparse
import io
import hashlib
import json
import subprocess
import tarfile
import time

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--out', required=True)
args = parser.parse_args()
destination = Path(args.out).resolve()
if subprocess.check_output(['git','status','--porcelain','--untracked-files=normal'],cwd=ROOT).strip():
    raise SystemExit('RELEASE_REQUIRES_COMMITTED_CLEAN_SOURCE')
destination.parent.mkdir(parents=True, exist_ok=True)
commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
snapshot=tarfile.open(fileobj=io.BytesIO(subprocess.check_output(['git','archive','--format=tar',commit],cwd=ROOT)))
# The committed tree is authoritative even if a filesystem stat cache misses a mutation.
manifest=json.load(snapshot.extractfile('config/upstream-lock.json'))
upstream=[]
for folder, source in manifest.items():
    for name,expected in source['files'].items():
        path=ROOT/folder/name
        if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise SystemExit('UPSTREAM_INTEGRITY_MISMATCH')
        upstream.append((folder+'/'+name,path))
release_time=time.time()
def stamp(item):
    # Fresh release metadata makes BuildKit rescan same-size files after extraction.
    # In particular the generated 40-character commit file must not always have mtime=0.
    item.mtime=release_time
    item.pax_headers={**item.pax_headers,'mtime':str(release_time)}
    return item

with tarfile.open(destination, 'w:gz') as archive:
    for member in snapshot:
        if member.isfile() and member.name!='source-version.json':
            archive.addfile(stamp(member),snapshot.extractfile(member))
    for relative,path in sorted(upstream):
        archive.add(path, arcname=relative, recursive=False,filter=stamp)
    version = {'commit':commit,
               'upstream':{folder:item['commit'] for folder,item in manifest.items()}}
    data = json.dumps(version,indent=2).encode()
    entry = tarfile.TarInfo('source-version.json')
    entry.size = len(data)
    entry.mtime=release_time
    archive.addfile(entry, io.BytesIO(data))
snapshot.close()
print(json.dumps({'release':str(destination),'bytes':destination.stat().st_size,'source':version}))
