"""Run the frozen readiness/loading delta and retention suite on one isolated image.

Reuses the existing suite and retention validators. No production data mounts,
provider requests, queue changes, or deployment. Arguments are owner-staged files.
"""
from pathlib import Path, PurePosixPath
import argparse, hashlib, json, os, re, subprocess, sys, tarfile, types, uuid

ROOT = Path('/opt/lighthouse-hub')
BASE = 'sha256:b6dd672929e46c1699c0b4281c7dbe047b099019bb9ae9ce832ce79a850eb94c'
TAG = 'lighthouse-hub:manager-links41-f0069dc58e32'
Q_SHA = '401ba7df4a291bc0dbdc46b5f72d6287f4afab3b5da5dc22926d48c2f956c387'
M_SHA = '4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3'
PARENT = Path('/var/lib/lighthouse-backup-ops/replay-manager-links41-v5-tests/3d5179ed-74b6-4fa8-ba9b-2e0c82713bd1/test-result.json')
PARENT_SHA = '8042f5b57c0a32237bc073ca5f22ecab4abcc70a7d789e38a4b33473b5c8923f'
OLD26 = Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/4c571bff-ea24-4029-978b-83d8992aabfd/test-attestation.json')
OLD26_SHA = 'a735028c0e7e6b7a09bab290d9f4697cbe6b8a62609f18ac61ca9b29f0a318bb'
PRODUCTION = {'adapters/replay_part_owner_handoff_v5.mjs', 'adapters/replay_later_result_lookup_v2.mjs',
              'adapters/youtube_readiness_evidence_v3.py', 'adapters/ytdlp_whole_v4.py', 'services/hub/youtube_readiness_v4.py'}

def sha(raw): return hashlib.sha256(raw).hexdigest()
def wire(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
def load(file, pin, name):
    raw = file.read_bytes(); assert sha(raw) == pin
    module = types.ModuleType(name); module.__file__ = str(file)
    exec(compile(raw, str(file), 'exec'), module.__dict__); return module

def updated_sources(m, current, previous):
    # The frozen helper deliberately pins the old handoff source. Retain its
    # exact 141-path scope while explicitly qualifying this observed fix.
    prior = m.source_files(previous)
    selected = {n for n in current['files'] if n.startswith(('services/hub/', 'adapters/')) and n.endswith('.py')} | set(m.NODE_SOURCES)
    assert selected == set(prior) and len(selected) == 141
    result = {n:current['files'][n]['sha256'] for n in sorted(selected)}
    assert all(re.fullmatch('[a-f0-9]{64}', v) for v in result.values())
    assert {n for n in result if result[n] != prior[n]} == PRODUCTION
    return result

DEPENDENCIES = r'''import hashlib,json,os,shutil,stat
from pathlib import Path
roots=[Path(p) for p in ('/opt/venv','/opt/lighthouse-hub/compositor/runtime','/usr/local','/usr/lib','/usr/bin')]
for name in ('python','node','ffmpeg','ffprobe'):
 p=shutil.which(name);assert p;roots.append(Path(p).resolve())
entries={}
def visit(p):
 s=p.lstat();v={'mode':stat.S_IMODE(s.st_mode),'uid':s.st_uid,'gid':s.st_gid}
 if p.is_symlink():v.update(type='link',target=os.readlink(p))
 elif p.is_file():
  h=hashlib.sha256()
  with p.open('rb') as f:
   while b:=f.read(1048576):h.update(b)
  v.update(type='file',bytes=s.st_size,sha256=h.hexdigest())
 elif p.is_dir():v.update(type='directory')
 else:raise ValueError('UNEXPECTED_DEPENDENCY_TYPE')
 entries[str(p)]=v
for p in roots:
 assert p.exists();visit(p)
 if p.is_dir() and not p.is_symlink():
  for folder,dirs,files in os.walk(p,followlinks=False):
   for name in sorted(dirs+files):visit(Path(folder)/name)
visit(Path('/opt/lighthouse-hub'))
raw=json.dumps(entries,sort_keys=True,separators=(',',':')).encode()
print(json.dumps({'sha256':hashlib.sha256(raw).hexdigest(),'entries':len(entries),'roots':[str(p) for p in roots]}))
'''

def flatten_base(q,m,area,before):
    base=m.inspect_image(BASE);config=base['Config']
    assert set(config)=={'User','Env','Cmd','WorkingDir','Labels','ArgsEscaped'} and config['ArgsEscaped'] is True
    flat_context=area/'flat-context';flat_context.mkdir(mode=0o700)
    lines=['FROM '+TAG+' AS inherited','FROM scratch','COPY --from=inherited / /']
    lines += ['ENV '+value.split('=',1)[0]+'='+json.dumps(value.split('=',1)[1]) for value in config['Env']]
    lines += ['LABEL '+name+'='+json.dumps(value) for name,value in config['Labels'].items()]
    lines += ['USER '+config['User'],'WORKDIR '+config['WorkingDir'],'CMD '+json.dumps(config['Cmd'])]
    dockerfile='\n'.join(lines)+'\n';(flat_context/'Dockerfile').write_text(dockerfile)
    tag='lighthouse-hub:readiness-flat-'+BASE.removeprefix('sha256:')[:12]
    built=q.run(['docker','build','--network=none','--pull=false','-t',tag,str(flat_context)],timeout=600)
    m.save(area/'flatten-build.log',(built.stdout+'\n'+built.stderr).encode())
    flat_id=q.run(['docker','image','inspect','-f','{{.Id}}',tag]).stdout.strip()
    flat=m.inspect_image(flat_id)
    assert len(flat['RootFS']['Layers'])==2 and flat['Config']==config
    assert all(flat.get(k)==base.get(k) for k in ('Os','Architecture','Variant'))
    old=json.loads(q.run(['docker','exec','--user','10001:10001',before['video-worker']['id'],'python','-c',DEPENDENCIES],timeout=180).stdout)
    checked=json.loads(q.run(['docker','run','--rm','--pull=never','--network=none','--read-only','--user=10001:10001',
        '--cap-drop=ALL','--security-opt=no-new-privileges:true','--memory=512m','--cpus=1','--pids-limit=64',
        '--entrypoint=python',flat['Id'],'-c',DEPENDENCIES],timeout=180).stdout)
    assert checked==old and checked['entries']>1000
    return {'strategy':'buildkit-merged-rootfs-copy-v1','original_image':BASE,'original_layer_count':len(base['RootFS']['Layers']),
        'flat_image':flat['Id'],'flat_layer_count':2,'config_exact_match':True,
        'config_sha256':sha(wire(config)),'platform_exact_match':True,'dockerfile_sha256':sha(dockerfile.encode()),
        'dependency_tree':checked,'dependency_tree_equal':True,'production_container_exported':False}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('archive', 'suite-helper', 'retention-helper'): parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--sha256', required=True); args = parser.parse_args()
    assert os.name == 'posix' and os.geteuid() == 0 and __debug__; os.umask(0o077)
    q = load(args.suite_helper, Q_SHA, 'frozen_suite'); m = load(args.retention_helper, M_SHA, 'frozen_retention')
    parent_raw = m.read(PARENT, protected=True); old_raw = m.read(OLD26, protected=True)
    assert sha(parent_raw) == PARENT_SHA and sha(old_raw) == OLD26_SHA
    parent = json.loads(parent_raw); old26 = json.loads(old_raw)
    assert parent['passed'] and parent['image'] == old26['image'] == BASE
    parent_xml = m.read(Path(parent['junit_file'])); old_xml = m.read(Path(old26['junit_host_file']))
    assert sha(parent_xml) == parent['junit_sha256'] and sha(old_xml) == old26['junit_sha256']
    assert m.junit(parent_xml, 41) == parent['test_groups']
    assert m.junit(old_xml, 26, group=m.GROUP) == old26['test_groups']
    assert old26['full_suite_passed'] and old26['source_files'] == m.source_files(parent['source_manifest'])
    owner = json.loads(m.read(q.OWNER, protected=True)); assert m.owner(owner) == q.OWNER_SHA
    before = q.production(); assert before['video-worker']['image'] == BASE
    assert args.archive.parent == Path('/home/lighthouse-ops') and args.archive.resolve() == args.archive
    assert sha(m.read(args.archive, maximum=2_000_000)) == args.sha256
    area = m.root_directory(Path('/var/lib/lighthouse-backup-ops/replay-readiness-loading-v5-tests') / str(uuid.uuid4()))
    context = area / 'context'; context.mkdir(mode=0o700)
    payload = context / 'payload'; payload.mkdir(mode=0o755)
    with tarfile.open(args.archive, 'r:gz') as tar:
        entries = tar.getmembers(); manifest = json.load(tar.extractfile('manifest.json'))
        assert manifest['version'] == 'replay-readiness-loading-candidate-v5' and manifest['base_image'] == BASE
        assert manifest['parent_sha256'] == PARENT_SHA and manifest['parent_retention_sha256'] == OLD26_SHA
        assert manifest['base_source_manifest'] == parent['source_manifest']
        current = manifest['source_manifest']; pins = manifest['delta_sha256']; groups = manifest['test_groups']
        assert re.fullmatch('[a-f0-9]{40}', manifest['source_commit']) and current['source_commit'] == manifest['source_commit']
        assert manifest['production_runtime_changed_files'] == sorted(PRODUCTION)
        assert current['expected_test_count'] == sum(groups.values()) > 0
        assert all(re.fullmatch(r'tests[.](?:ops[.])?test_[a-z0-9_]+', k) and type(v) is int and v > 0 for k,v in groups.items())
        assert len(entries) == len(pins) + 1 and {e.name for e in entries} == set(pins) | {'manifest.json'}
        assert set(PRODUCTION) <= set(pins) and len(pins) <= 30
        assert all(n in PRODUCTION or n.startswith(('tests/', 'scripts/ops/resume_')) for n in pins)
        old = parent['source_manifest']['files']; new = current['files']
        assert set(old) <= set(new) and {n for n in old if old[n] != new[n]} == set(pins) & set(old)
        assert set(new) - set(old) == set(pins) - set(old)
        old_sources = m.source_files(parent['source_manifest']); new_sources = updated_sources(m, current, parent['source_manifest'])
        assert set(old_sources) == set(new_sources) and {n for n in old_sources if old_sources[n] != new_sources[n]} == PRODUCTION
        for item in entries:
            rel = PurePosixPath(item.name)
            assert item.isfile() and not rel.is_absolute() and '..' not in rel.parts and 0 < item.size < 1_500_000
            if item.name == 'manifest.json': continue
            raw = tar.extractfile(item).read()
            assert sha(raw) == pins[item.name] == new[item.name]['sha256'] and len(raw) == new[item.name]['bytes']
            target = payload / item.name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(raw); target.chmod(0o644)
    for d in payload.rglob('*'):
        if d.is_dir(): d.chmod(0o755)
    (payload / 'candidate-manifest.json').write_bytes(wire(current) + b'\n'); (payload / 'candidate-manifest.json').chmod(0o644)
    assert q.run(['docker','image','inspect','-f','{{.Id}}',TAG]).stdout.strip() == BASE
    flattened=flatten_base(q,m,area,before)
    flat_tag='lighthouse-hub:readiness-flat-'+BASE.removeprefix('sha256:')[:12]
    assert q.run(['docker','image','inspect','-f','{{.Id}}',flat_tag]).stdout.strip()==flattened['flat_image']
    (context / 'Dockerfile').write_text('FROM ' + flat_tag + '\nCOPY --chown=0:0 payload/ /opt/lighthouse-hub/\n')
    tag = 'lighthouse-hub:readiness-loading-' + args.sha256[:12]
    build = q.run(['docker','build','--network=none','--pull=false','-t',tag,str(context)], timeout=240)
    m.save(area/'build.log', (build.stdout + '\n' + build.stderr).encode())
    image = q.run(['docker','image','inspect','-f','{{.Id}}',tag]).stdout.strip()
    base = m.inspect_image(BASE); candidate = m.inspect_image(image)
    assert candidate['RootFS']['Layers'][:-1] == m.inspect_image(flattened['flat_image'])['RootFS']['Layers']
    assert len(candidate['RootFS']['Layers'])==3 and candidate['Config']==base['Config']
    seccomp = ROOT/'config/browser-seccomp.json'; assert sha(m.read(seccomp)) == current['files']['config/browser-seccomp.json']['sha256']
    result = q.suite(m, image, current, manifest['test_selections'], groups, area, seccomp)
    result.update(version='replay-readiness-loading-ubuntu-v5', base_image=BASE, source_commit=manifest['source_commit'],
                  archive_sha256=args.sha256, manifest=manifest, source_manifest=current,
                  image_build_strategy='flattened-base-plus-delta-v1',flattened_base=flattened,source_layer_delta_from_flat=1,
                  parent_sha256=PARENT_SHA, parent_retention_sha256=OLD26_SHA,
                  production_before=before, production_after=q.production())
    result['production_all_five_unchanged'] = before == result['production_after']
    result['passed'] = result['passed'] and result['production_all_five_unchanged']
    raw = wire(result) + b'\n'; m.save(area/'test-result.json', raw)
    print(json.dumps({'stage':'readiness_loading','file':str(area/'test-result.json'),'sha256':sha(raw),'image':image,'passed':result['passed']}), flush=True)
    assert result['passed']
    folder = m.root_directory(m.BASE/str(uuid.uuid4())); m.save(folder/'browser-seccomp.json', m.read(seccomp))
    checked = q.suite(m,image,current,[m.SELECTION],{m.GROUP:26},folder,folder/'browser-seccomp.json')
    proof = {'version':m.VERSION,'tests':checked['tests'],'test_groups':checked['test_groups'],'exit_code':checked['exit_code'],
             'failures':checked['failure'],'errors':checked['error'],'skipped':checked['skipped'],'network':'none',
             'application_data_mounted':False,'production_profile_mounted':False,'secrets_environment_injected':False,
             'oom_killed':checked['oom_killed'],'timed_out':checked['timed_out'],'full_suite_passed':checked['passed'],
             'no_pid_or_oom_denial':checked['resource_limits_passed'],'runtime_matches_declared_candidate':checked['sources_unchanged'],
             'mounts_match_isolated_allowlist':checked['runtime_matches_allowlist'],'mounted_sources_unchanged':checked['sources_unchanged'],
             'source_files':new_sources,'image':image,'container_id':checked['container_id'],
             'qualified_parent_sha256':sha(raw),'source_archive_sha256':args.sha256,'source_commit':manifest['source_commit'],
             'junit_file':str(m.CONTAINER_PROOF/'results.xml'),'junit_host_file':checked['junit_file'],'junit_sha256':checked['junit_sha256'],
             'resource_before':checked['resource_before'],'resource_after':checked['resource_after'],'duration_seconds':checked['seconds'],
             'host_file':str(folder/'test-attestation.json'),'source_manifest_sha256':sha(wire(current)),
             'qualification_lineage':{'prior_lineage':old26['qualification_lineage'],'readiness_loading_sha256':sha(raw),'runtime_changed_files':sorted(PRODUCTION)},
             'production_before':before,'production_after':q.production()}
    proof['production_all_five_unchanged'] = proof['production_before'] == proof['production_after']
    praw = wire(proof) + b'\n'; m.save(folder/'test-attestation.json',praw)
    assert proof['full_suite_passed'] and proof['production_all_five_unchanged']
    release = m.release_template(owner,proof,praw); m.save(folder/'release-template.json',wire(release)+b'\n')
    print(json.dumps({'stage':'retention26','file':str(folder/'test-attestation.json'),'sha256':sha(praw),'image':image,
                      'release_template':str(folder/'release-template.json'),
                      'release_template_sha256':sha(wire(release)+b'\n'),'tests':26,'passed':True}),flush=True)

if __name__ == '__main__':
    try: main()
    except subprocess.CalledProcessError as error:
        if error.stderr: sys.stderr.write(error.stderr[-12000:])
        raise
