"""Build one COPY layer over 06ed; qualify 42 then the unchanged 26 offline.

Only new candidate images and owned evidence are written. No deploy, service
action, live application mount, browser profile, credential injection or API.
"""
from pathlib import Path, PurePosixPath
from collections import Counter
import argparse, hashlib, json, os, re, subprocess, tarfile, time, types, uuid
import xml.etree.ElementTree as ET

ROOT=Path('/opt/lighthouse-hub')
BASE='sha256:06ed6b8e6adff8fd2a43e5345c8d1d1d1f62c463fd79d362d2df4dfdac1705e4'
TAG='lighthouse-hub:whole-workspace53-7d4b5a688a27'
COMMIT='551819ae38b1304669bb54daa13368a665276295'
FILES={'services/hub/replay_parts_worker_runtime_v5.py':'006b94856a9ae062a1835d2a71bcefc985df242386ca570e47d4846a78f3766f',
       'tests/test_replay_part_moderation_contention_v5.py':'b9882a897bccc7ac8fbf330c80ee6eddfeaccc9a14ab3237cbed91689f1c4f2f'}
GROUPS={'tests.test_replay_part_moderation_contention_v5':10,'tests.test_replay_parts_runtime_v5':9,'tests.test_replay_part_moderation_v5':23}
SELECT=['tests/'+v.split('.')[1]+'.py' for v in GROUPS]
PARENT=Path('/home/lighthouse-ops/lighthouse-evidence/source-candidate-c0c335eb-bdc4-460c-ae4e-dc42493c018a/test-result.json')
INVENTORY=Path('/home/lighthouse-ops/lighthouse-evidence/youtube-real-inventory54-3f83b16b-e15f-42d0-9cfa-b8d3012bf63e/test-result.json')
WORKSPACE=Path('/home/lighthouse-ops/lighthouse-evidence/whole-workspace53-5b06a922-4759-4ad4-9787-9b47c4037ad4/test-result.json')
OWNER=Path('/var/lib/lighthouse-backup-ops/replay-parts-retention-v5-tests/owner-authorization-1fb26fc9.json')
OWNER_SHA='8869d3ad9f68c317030ebbfdf5211075a624563e0adf9f653317a03747910eb9'
WORKSPACE_HELPER_SHA='eb416ff99e63b664312b8bf561439bd429ef3f6ab00186c447d38ee9cfc3b598'


def sha(raw):return hashlib.sha256(raw).hexdigest()
def wire(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def run(args,timeout=60):return subprocess.run(args,check=True,capture_output=True,text=True,timeout=timeout)
def inspect(cid):return json.loads(run(['docker','inspect',cid]).stdout)[0]
def identity(info):return {'id':info['Id'],'image':info['Image'],'pid':info['State']['Pid'],'started_at':info['State']['StartedAt'],'running':info['State']['Running']}
def production():
    found={}
    for name in ('api','scheduler','tweet-worker','system-worker','video-worker'):
        cid=run(['docker','compose','--project-directory',str(ROOT),'-f',str(ROOT/'compose.yaml'),'ps','-q',name]).stdout.strip()
        assert re.fullmatch('[a-f0-9]{64}',cid)
        found[name]=identity(inspect(cid));assert found[name]['running']
    return found


BOOT=r'''from pathlib import Path
import hashlib,json,os,sys
for key in ('HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME'):Path(os.environ[key]).mkdir(parents=True,exist_ok=True)
assert os.getuid()==os.getgid()==10001 and set(os.getgroups())<={10001}
root=Path('/opt/lighthouse-hub');manifest=json.loads((root/'candidate-manifest.json').read_bytes())
def sources():
 return {n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n,v in manifest['files'].items() if (root/n).is_file() and not (root/n).is_symlink() and (root/n).stat().st_size==v['bytes']}
def counters():
 return {n:{k:int(v) for k,v in (s.split() for s in (Path('/sys/fs/cgroup')/n).read_text().splitlines())} for n in ('pids.events','memory.events')}
expected={n:v['sha256'] for n,v in manifest['files'].items()};before=sources();assert before==expected
Path('/owned/source-before.json').write_text(json.dumps(before));Path('/owned/resource-before.json').write_text(json.dumps(counters()))
import pytest
status=pytest.main(['-q','-p','no:cacheprovider',*sys.argv[1:],'--basetemp=/owned/pytest','--junitxml=/owned/results.xml'])
Path('/owned/source-after.json').write_text(json.dumps(sources()));Path('/owned/resource-after.json').write_text(json.dumps(counters()))
raise SystemExit(status)
'''


def suite(m,image,manifest,selections,groups,folder,seccomp):
    owned=folder/'owned';owned.mkdir(mode=0o700);os.chown(owned,10001,10001);owned.chmod(0o700)
    command=['docker','create','--pull=never','--init','--name','contention-tests-'+folder.name,
        '--network=none','--read-only','--user=10001:10001','--cap-drop=ALL',
        '--security-opt=no-new-privileges:true','--security-opt=seccomp='+str(seccomp),
        '--memory=1400m','--cpus=1','--pids-limit=256','--shm-size=256m',
        '--tmpfs=/tmp:rw,size=256m,mode=1777','--tmpfs='+str(ROOT/'media')+':rw,size=32m,mode=0700,uid=10001,gid=10001',
        '--env=HOME=/tmp/home','--env=XDG_CONFIG_HOME=/tmp/config','--env=XDG_CACHE_HOME=/tmp/cache','--env=PYTHONDONTWRITEBYTECODE=1',
        '--mount=type=bind,source='+str(owned)+',target=/owned','--entrypoint=python',image,'-c',BOOT,*selections]
    cid=run(command).stdout.strip();info=inspect(cid);profile=json.loads(m.read(seccomp))
    m.validate_isolation(info,image=image,owned=owned,seccomp=seccomp,seccomp_profile=profile)
    started=time.time();run(['docker','start',cid]);timed_out=False
    try:code=int(run(['docker','wait',cid],timeout=900).stdout.strip())
    except subprocess.TimeoutExpired:
        timed_out=True;current=inspect(cid);assert current['Id']==cid and current['Image']==image
        run(['docker','stop','--time','10',cid]);code=124
    logs=run(['docker','logs',cid]);m.save(folder/'tests.log',(logs.stdout+'\n'+logs.stderr).encode())
    final=inspect(cid);m.validate_isolation(final,image=image,owned=owned,seccomp=seccomp,seccomp_profile=profile)
    xml=m.read(owned/'results.xml');cases=list(ET.fromstring(xml).iter('testcase'))
    actual=dict(Counter(c.attrib['classname'] for c in cases));counts={k:sum(c.find(k) is not None for c in cases) for k in ('failure','error','skipped')}
    snapshots={n:json.loads(m.read(owned/(n+'.json'))) for n in ('source-before','source-after','resource-before','resource-after')}
    for n in snapshots:m.save(folder/(n+'.json'),m.read(owned/(n+'.json')))
    expected={n:v['sha256'] for n,v in manifest['files'].items()}
    unchanged=snapshots['source-before']==snapshots['source-after']==expected
    resources=all(c['pids.events'].get('max',-1)==c['memory.events'].get('oom',-1)==c['memory.events'].get('oom_kill',-1)==0 for c in (snapshots['resource-before'],snapshots['resource-after']))
    passed=code==0 and len(cases)==sum(groups.values()) and actual==groups and not any(counts.values()) and unchanged and resources and not timed_out and not final['State']['OOMKilled']
    m.save(folder/'results.xml',xml)
    return {'passed':passed,'tests':len(cases),'test_groups':actual,'exit_code':code,'failure':counts['failure'],'error':counts['error'],'skipped':counts['skipped'],
        'source_hashes':expected,'source_before':snapshots['source-before'],'source_after':snapshots['source-after'],
        'resource_before':snapshots['resource-before'],'resource_after':snapshots['resource-after'],'sources_unchanged':unchanged,
        'resource_limits_passed':resources,'network':'none','production_data_mounted':False,'production_profile_mounted':False,
        'credentials_injected':False,'oom_killed':final['State']['OOMKilled'],'timed_out':timed_out,
        'runtime_matches_allowlist':True,'image':image,'container_id':cid,'junit_file':str(folder/'results.xml'),'junit_sha256':sha(xml),'seconds':time.time()-started}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('archive','helper','inventory-helper','workspace-helper'):p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--sha256',required=True);a=p.parse_args()
    assert os.name=='posix' and os.geteuid()==0 and __debug__;os.umask(0o077)
    raw=a.workspace_helper.read_bytes();assert sha(raw)==WORKSPACE_HELPER_SHA
    w=types.ModuleType('frozen_workspace53_qualifier');w.__file__=str(a.workspace_helper);exec(compile(raw,str(a.workspace_helper),'exec'),w.__dict__)
    previous=w.load_adapter(a.inventory_helper);m=previous.load(a.helper)
    praw=m.read(PARENT);iraw=m.read(INVENTORY);wraw=m.read(WORKSPACE)
    assert sha(praw)==w.PARENT_SHA and sha(iraw)==w.INVENTORY_SHA and sha(wraw)==w.WORKSPACE_SHA
    parent=json.loads(praw);inventory=json.loads(iraw);workspace=json.loads(wraw)
    m.validate_parent(parent,m.read(Path(parent['junit_file'])))
    prior=previous.validate_delta(m,inventory,m.read(Path(inventory['junit_file'])),parent)
    lineage53=w.validate_workspace(m,workspace,m.read(Path(workspace['junit_file'])),inventory,prior)
    authorization=json.loads(m.read(OWNER,protected=True));assert m.owner(authorization)==OWNER_SHA
    base=m.inspect_image(BASE);assert workspace['image']==BASE and run(['docker','image','inspect','-f','{{.Id}}',TAG]).stdout.strip()==BASE
    old_sources=m.source_files(workspace['source_manifest']);assert len(old_sources)==141
    before_production=production();assert before_production['video-worker']['image']==BASE
    assert a.archive.parent==Path('/home/lighthouse-ops') and a.archive.resolve()==a.archive and not a.archive.is_symlink()
    assert sha(m.read(a.archive,maximum=500000))==a.sha256
    area=m.root_directory(Path('/var/lib/lighthouse-backup-ops/replay-moderation-contention42-v5-tests')/str(uuid.uuid4()))
    context=area/'context';context.mkdir(mode=0o700);payload=context/'payload';payload.mkdir(mode=0o700)
    with tarfile.open(a.archive,'r:gz') as tar:
        entries=tar.getmembers();assert len(entries)==3 and {v.name for v in entries}==set(FILES)|{'manifest.json'}
        manifest=json.load(tar.extractfile('manifest.json'))
        assert manifest['version']=='replay-moderation-contention42-candidate-v5' and manifest['source_commit']==COMMIT
        assert manifest['base_image']==BASE and manifest['delta_sha256']==FILES and manifest['test_groups']==GROUPS
        assert manifest['base_source_manifest']==workspace['source_manifest']
        current=manifest['source_manifest'];old=workspace['source_manifest']['files'];new=current['files']
        assert len(old)==852 and len(new)==853 and current['source_commit']==COMMIT and current['test_selections']==SELECT and current['expected_test_count']==42
        assert {n for n in old if old[n]!=new[n]}=={'services/hub/replay_parts_worker_runtime_v5.py'}
        assert set(new)-set(old)=={'tests/test_replay_part_moderation_contention_v5.py'}
        assert all(new[n]['sha256']==v for n,v in FILES.items())
        assert {n for n,v in m.source_files(current).items() if v!=old_sources[n]}=={'services/hub/replay_parts_worker_runtime_v5.py'}
        for v in entries:
            name=PurePosixPath(v.name);assert v.isfile() and not v.issym() and not v.islnk() and not name.is_absolute() and '..' not in name.parts and 0<v.size<400000
            if v.name=='manifest.json':continue
            data=tar.extractfile(v).read();assert sha(data)==FILES[v.name] and len(data)==new[v.name]['bytes']
            file=payload/v.name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(data);file.chmod(0o644)
    payload.chmod(0o755)
    for directory in payload.rglob('*'):
        if directory.is_dir():directory.chmod(0o755)
    (payload/'candidate-manifest.json').write_bytes(wire(current)+b'\n');(payload/'candidate-manifest.json').chmod(0o644)
    (context/'Dockerfile').write_text('FROM '+TAG+'\nCOPY --chown=0:0 payload/ /opt/lighthouse-hub/\n')
    target='lighthouse-hub:replay-contention42-'+a.sha256[:12]
    build=run(['docker','build','--network=none','--pull=false','-t',target,str(context)],timeout=240)
    m.save(area/'build.log',(build.stdout+'\n'+build.stderr).encode())
    image=run(['docker','image','inspect','-f','{{.Id}}',target]).stdout.strip();candidate=m.inspect_image(image)
    assert candidate['RootFS']['Layers'][:-1]==base['RootFS']['Layers'] and len(candidate['RootFS']['Layers'])==len(base['RootFS']['Layers'])+1
    assert all(candidate['Config'].get(k)==base['Config'].get(k) for k in ('Cmd','Entrypoint','User','Env','WorkingDir','Volumes','Labels','Healthcheck'))
    seccomp=ROOT/'config/browser-seccomp.json';assert sha(m.read(seccomp))==current['files']['config/browser-seccomp.json']['sha256']
    result=suite(m,image,current,SELECT,GROUPS,area,seccomp)
    result.update(version='replay-moderation-contention42-ubuntu-v5',base_image=BASE,source_commit=COMMIT,archive_sha256=a.sha256,
        manifest=manifest,source_manifest=current,parent_53_source_manifest=workspace['source_manifest'],
        source_layer_delta=1,parent_qualification_lineage=lineage53,production_before=before_production,production_after=production())
    result['production_video_unchanged']=result['production_before']['video-worker']==result['production_after']['video-worker']
    result['production_all_five_unchanged']=result['production_before']==result['production_after']
    result['passed']=result['passed'] and result['production_all_five_unchanged']
    result_raw=wire(result)+b'\n';m.save(area/'test-result.json',result_raw)
    print(json.dumps({'stage':'contention42','file':str(area/'test-result.json'),'sha256':sha(result_raw),'image':image,'passed':result['passed']}),flush=True)
    assert result['passed']
    folder=m.root_directory(m.BASE/str(uuid.uuid4()))
    m.save(folder/'contention42-qualified-run.json',result_raw);m.save(folder/'contention42-results.xml',m.read(Path(result['junit_file'])))
    m.save(folder/'browser-seccomp.json',m.read(seccomp));seccomp=folder/'browser-seccomp.json'
    line={'version':'retention276-inventory54-workspace53-contention42-lineage-v5','prior_lineage':lineage53,
        'contention42_run_sha256':sha(result_raw),'contention42_source_commit':COMMIT,'composed_source_manifest_sha256':sha(wire(current)),
        'retention_runtime_source_count':141,'runtime_changed_files_from_workspace53':['services/hub/replay_parts_worker_runtime_v5.py'],
        'parent_276_54_53_not_rerun':True}
    m.save(folder/'lineage.json',wire(line)+b'\n')
    checked=suite(m,image,current,[m.SELECTION],{m.GROUP:26},folder,seccomp)
    after_production=production();unchanged=checked['sources_unchanged'];resources=checked['resource_limits_passed']
    proof={'version':m.VERSION,'tests':checked['tests'],'test_groups':checked['test_groups'],'exit_code':checked['exit_code'],
        'failures':checked['failure'],'errors':checked['error'],'skipped':checked['skipped'],'network':'none',
        'application_data_mounted':False,'production_profile_mounted':False,'secrets_environment_injected':False,
        'oom_killed':checked['oom_killed'],'timed_out':checked['timed_out'],'full_suite_passed':checked['passed'],
        'no_pid_or_oom_denial':resources,'runtime_matches_declared_candidate':unchanged,'mounts_match_isolated_allowlist':True,
        'mounted_sources_unchanged':unchanged,'source_files':m.source_files(current),'image':image,'container_id':checked['container_id'],
        'qualified_parent_sha256':sha(result_raw),'source_archive_sha256':a.sha256,'source_commit':COMMIT,
        'junit_file':str(m.CONTAINER_PROOF/'results.xml'),'junit_host_file':checked['junit_file'],'junit_sha256':checked['junit_sha256'],
        'resource_before':checked['resource_before'],'resource_after':checked['resource_after'],'duration_seconds':checked['seconds'],
        'host_file':str(folder/'test-attestation.json'),'source_manifest_sha256':sha(wire(current)),
        'qualification_helper_sha256':sha(m.read(Path(__file__).resolve())),'qualification_lineage':line,
        'original_qualification_helper_sha256':w.HELPER_SHA,'production_before':before_production,'production_after':after_production,
        'production_all_five_unchanged':after_production==before_production}
    raw=wire(proof)+b'\n';m.save(folder/'test-attestation.json',raw)
    assert proof['full_suite_passed'] and resources and unchanged and proof['production_all_five_unchanged']
    template=m.release_template(authorization,proof,raw);assert template['authorization_sha256']==OWNER_SHA
    m.save(folder/'release-template.json',wire(template)+b'\n')
    print(json.dumps({'stage':'retention26','state':'qualified_not_installed','file':str(folder/'test-attestation.json'),
        'sha256':sha(raw),'image':image,'tests':checked['tests'],'release_template':str(folder/'release-template.json'),
        'release_template_sha256':sha(m.read(folder/'release-template.json')),'readonly_mount':str(folder)+':'+str(m.CONTAINER_PROOF)+':ro'}),flush=True)


if __name__=='__main__':main()
