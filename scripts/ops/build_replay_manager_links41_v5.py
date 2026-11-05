from pathlib import Path
import copy,gzip,hashlib,io,json,subprocess,tarfile
ROOT=Path(__file__).resolve().parents[4];REPO=ROOT/'outputs/lighthouse-hub'
sha=lambda b:hashlib.sha256(b).hexdigest()
wire=lambda v:json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
PARENT_SHA='e3c646987e7660a232b9e6cddb5e6a449795bb6b05e0204ced50235983684a5f'
OLD26_SHA='ab8158d642c05a401f63c482c1994b458d82b8e35451016f015ff779e909b855'
base_raw=(ROOT/'outputs/replay-contention42-retention26-v5-actual/contention42/test-result.json').read_bytes();assert sha(base_raw)==PARENT_SHA
parent=json.loads(base_raw);base=parent['source_manifest'];assert len(base['files'])==853
handoff=json.loads((ROOT/'work/later-manager-v5-qualified-handoff.json').read_bytes());assert handoff['commit']=='ee56f173f4893decaccc43a81af4d44a4b4cccde'
fix=handoff['commit'];ops='97b3bf6d7ecab70bed846dd98d478556073c2046'
files={n:(fix,pin) for n,pin in handoff['files'].items() if not n.startswith('docs/')}
files.update({'scripts/ops/resume_fixed_part_lookup_v5.py':(ops,'592a3d49f0260f36ba0a940843cf15114f50da464b046ebf7ab2c059f4a4caf5'),'tests/ops/test_resume_fixed_part_lookup_v5.py':(ops,'df3559e66a8ee9ac741d813e0f6a280741284d42974194a0c6cf9c18a29399c1')})
assert len(files)==10
payload={n:subprocess.run(['git','show',commit+':'+n],cwd=REPO,check=True,capture_output=True).stdout for n,(commit,pin) in files.items()}
assert all(sha(raw)==files[n][1] for n,raw in payload.items())
current=copy.deepcopy(base)
for n,raw in payload.items():current['files'][n]={'bytes':len(raw),'sha256':sha(raw)}
groups=dict(handoff['groups']);groups['tests.ops.test_resume_fixed_part_lookup_v5']=4
selections=handoff['test_selections']+['tests/ops/test_resume_fixed_part_lookup_v5.py']
prod=sorted(handoff['production_files']);assert len(prod)==3 and all(n in base['files'] for n in prod)
current.update(version='replay-manager-links41-source-manifest-v5',base_image=parent['image'],source_commit=ops,parent_source_commit=base['source_commit'],parent_archive_sha256=parent['archive_sha256'],parent_expected_test_count=42,expected_test_count=41,test_selections=selections,production_runtime_changed_files=prod,modified_nonruntime_files=sorted(n for n in payload if n in base['files'] and n not in prod),new_test_files=sorted(n for n in payload if n not in base['files']),qualification_scope='Frozen manager links 37 plus exact lookup maintenance 4; inherited prior tests not rerun')
assert len(current['files'])==858
m={'version':'replay-manager-links41-candidate-v5','base_image':parent['image'],'parent42_sha256':PARENT_SHA,'parent26_sha256':OLD26_SHA,'source_commit':ops,'manager_fix_commit':fix,'lookup_repair_commit':ops,'base_source_manifest':base,'delta_sha256':{n:sha(v) for n,v in payload.items()},'source_manifest':current,'production_runtime_changed_files':prod,'expected_test_count':41,'test_groups':groups,'test_selections':selections,'local_tests_note':handoff['line_endings_note'],'production_mutations_performed':False}
payload['manifest.json']=wire(m)+b'\n'
out=ROOT/'outputs/replay-manager-links41-v5.tar.gz'
with out.open('wb') as stream:
 with gzip.GzipFile(filename='',mode='wb',fileobj=stream,mtime=0) as gz:
  with tarfile.open(fileobj=gz,mode='w') as tar:
   for n,raw in sorted(payload.items()):
    item=tarfile.TarInfo(n);item.mode=0o644;item.size=len(raw);item.mtime=0;item.uid=item.gid=0;tar.addfile(item,io.BytesIO(raw))
(ROOT/'outputs/replay-manager-links41-v5-manifest.json').write_bytes(payload['manifest.json'])
record={'archive':str(out),'sha256':sha(out.read_bytes()),'bytes':out.stat().st_size,'delta_files':10,'composed_source_files':858,'runtime_sources':141,'production_changed_files':prod,'groups':groups,'tests':41}
(ROOT/'work/replay-manager-links41-build.json').write_bytes(wire(record)+b'\n')
print(json.dumps(record))
