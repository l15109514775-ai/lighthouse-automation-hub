"""Root default-plan deployment of the tested tweet-only source overlay. No policy activation."""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,io,json,os,stat,sys,tarfile,time,types,uuid
import xml.etree.ElementTree as ET

ROOT=Path('/opt/lighthouse-hub');EV=Path('/home/lighthouse-ops/lighthouse-evidence')
INSTALL=Path('/usr/local/lib/lighthouse-x-relations-v1');STATE=Path('/var/lib/lighthouse-x-relations-v1')
HELPER_SHA='9e9c868ec104e9582925f0cae8b8e5cb936d914665257647ca4d685d46687058'
ARCHIVE_SHA='cbe76dc6accd0dbefa65319fca93236bb843d9b29f39239f8f2549d5e032345e'
MANIFEST_SHA='74ea2516efc7925264fc225e50623f23d348497c4f4fbd264f8d16b31d664951'
HUB_SHA='e2b874d7ffb40600083ec534fc8ae449b4dd6f42ff9b775dacbe1d6e89b43df8'
CORE_SHA='01b073e7e725ea78c182ea5bc821ad73427f8dbab0bb5b14fc90daa799fb39b4'
CBC='sha256:cbc6cd5076960eba0adb723968bd10257c6691d62e3554cf48ae0ff0df5d8d10'
PROD=('adapters/x.py','services/hub/source_receipts.py','services/hub/x_api_discovery.py','adapters/model.py',
 'adapters/publication_window.mjs','adapters/render_machine_official_relations.mjs','services/hub/config.py',
 'services/hub/machine_routes.py','services/hub/machine_tweets_api.py','services/hub/machine_tweets_v2.py',
 'services/hub/model_contract.py','services/hub/model_revision.py','services/hub/pipelines.py','services/hub/readiness.py')
def sha(raw):return hashlib.sha256(raw).hexdigest()
def qualification_security(d,hc):
    assert hc['Init'] is True and hc['CapDrop']==['ALL']
    options=hc['SecurityOpt'];assert len(options)==2 and any(v in ('no-new-privileges:true','no-new-privileges') for v in options)
    values=[v[len('seccomp='):] for v in options if v.startswith('seccomp=')];assert len(values)==1
    profile=ROOT/'config/browser-seccomp.json'
    assert values[0]==str(profile) or json.loads(values[0])==json.loads(d.read(profile))
def load_helper(file):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents))
    with file.open('rb') as stream:raw=stream.read(200001)
    assert sha(raw)==HELPER_SHA
    module=types.ModuleType('x_relations_frozen_helper');module.__file__=str(file)
    exec(compile(raw,str(file),'exec'),module.__dict__);return module
def qualification(d,file,expected,archive):
    file=d.safe(Path(file));assert file.parent.parent==EV and file.name=='test-attestation.json'
    ident=str(uuid.UUID(file.parent.name.removeprefix('x-relations-v1-')));assert file.parent.name=='x-relations-v1-'+ident
    raw=d.read(file,300000);assert sha(raw)==expected;a=json.loads(raw)
    assert a['version']=='x-relations-ubuntu-tests-v1' and a['image']==CBC and a['archive_sha256']==ARCHIVE_SHA
    assert a['passed'] is True and a['tests']==177 and a['failures']==a['errors']==a['skipped']==a['exit_code']==0
    assert a['network']=='none' and all(a[k] is False for k in ('oom_killed','timed_out','production_data_mounted','credentials_injected'))
    assert all(a[k] is True for k in ('mounted_sources_match','isolated_mounts_verified','no_pid_or_oom_denial'))
    archive_raw=d.read(Path(archive),8000000);assert sha(archive_raw)==ARCHIVE_SHA
    with tarfile.open(fileobj=io.BytesIO(archive_raw),mode='r:gz') as tar:
        members=tar.getmembers();names=[x.name for x in members]
        assert len(names)==33 and len(set(names))==33 and all(x.isfile() and 0<x.size<500000 for x in members)
        manifest_raw=tar.extractfile('manifest.json').read();assert sha(manifest_raw)==MANIFEST_SHA;m=json.loads(manifest_raw)
        assert m==a['manifest'] and m['version']=='x-relations-ro-candidate-v1' and m['base_image']==CBC
        assert m['production_files']==list(PROD) and set(m['runtime_source_sha256'])==set(PROD) and len(m['files'])==32
        assert set(names)==set(m['files'])|{'manifest.json'} and m['expected_min_tests']==177
        contents={n:tar.extractfile(n).read() for n in m['files']}
    assert all(sha(v)==m['files'][n]['sha256'] and len(v)==m['files'][n]['bytes'] for n,v in contents.items())
    assert all(m['runtime_source_sha256'][n]==m['files'][n]['sha256'] for n in PROD)
    assert m['files']['compositor/src/core.mjs']['sha256']==CORE_SHA
    owned=Path('/home/lighthouse-ops')/('x-relations-owned-'+ident);junit=d.read(owned/'results.xml',2000000)
    assert sha(junit)==a['junit_sha256'] and d.read(file.parent/'results.xml',2000000)==junit
    cases=list(ET.fromstring(junit).iter('testcase'));assert len(cases)==a['tests']
    assert all(c.find(k) is None for c in cases for k in ('failure','error','skipped'))
    assert {c.get('classname','').removeprefix('tests.').removeprefix('ops.') for c in cases}=={
        Path(n).stem for n in m['test_selections']}
    assert sha(d.read(file.parent/'tests.log',3000000))==a['tests_log_sha256']
    hashes={n:v['sha256'] for n,v in m['files'].items()}
    for suffix in ('before','after'):
        assert json.loads(d.read(owned/('sources-'+suffix+'.json'),300000))==hashes
        resources=json.loads(d.read(owned/('resources-'+suffix+'.json'),30000));assert resources==a['resources_'+suffix]
        assert all(int(resources[key].get(item,0))==0 for key,item in (('pids.events','max'),('memory.events','oom'),('memory.events','oom_kill')))
    actual=json.loads(d.read(file.parent/'container.json',1000000))
    assert actual['Image']==CBC and actual['Config']['User']=='10001:10001' and actual['HostConfig']['ReadonlyRootfs'] is True
    assert actual['HostConfig']['NetworkMode']=='none' and actual['State']['ExitCode']==0 and not actual['State']['OOMKilled']
    hc=actual['HostConfig'];assert hc['Memory']==1100*1024**2 and hc['PidsLimit']==256 and hc['ShmSize']==256*1024**2 and hc['NanoCpus']==1000000000
    qualification_security(d,hc)
    mounts={x['Source']:(x['Destination'],x['RW']) for x in actual['Mounts'] if x['Type']=='bind'}
    required={str(owned):('/owned',True),str(file.parent/'check.py'):('/run/check.py',False),str(file.parent/'overlay/manifest.json'):('/run/candidate.json',False)}
    required.update({str(file.parent/'overlay'/n):(str(ROOT/n),False) for n in m['files']})
    assert mounts==required and all(x['Type']=='bind' or x['Type']=='tmpfs' and x['Destination']=='/tmp' for x in actual['Mounts'])
    for n in m['files']:assert sha(d.read(file.parent/'overlay'/n))==hashes[n]
    return {n:contents[n] for n in PROD},m,{'file':str(file),'sha256':expected,'tests':len(cases),'manifest_sha256':MANIFEST_SHA}
def candidate(before,folder):
    after=deepcopy(before);volumes=after['services']['tweet-worker']['volumes']
    assert set(before['services'])=={'tweet-worker','api','scheduler','video-worker','system-worker'}
    for name in PROD:
        target=str(ROOT/name);matches=[v for v in volumes if v.get('target')==target];assert len(matches)<=1
        if matches:
            assert matches[0].get('type')=='bind' and matches[0].get('read_only') is True
            volumes.remove(matches[0])
        volumes.append({'type':'bind','source':str(folder/name),'target':target,'read_only':True,'bind':{'create_host_path':False}})
    old=deepcopy(before);new=deepcopy(after)
    for value in (old,new):value['services']['tweet-worker']['volumes']=[v for v in value['services']['tweet-worker']['volumes'] if v.get('target') not in {str(ROOT/n) for n in PROD}]
    assert old==new;return after
DB=r'''
import hashlib,json,sqlite3,time
from pathlib import Path
db=sqlite3.connect(Path('/opt/lighthouse-hub/data/hub.sqlite3').as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
rows=[dict(r) for r in db.execute("SELECT job_id,state,attempts,stage,error_code,lease_owner,lease_until FROM jobs WHERE queue='tweet' ORDER BY job_id")]
value={'rows':rows,'processing':sum(r['state']=='processing' for r in rows),'unresolved':db.execute("SELECT COUNT(*) FROM publications WHERE source_platform='x' AND state IN ('dispatching','unknown')").fetchone()[0],
'runnable':db.execute("SELECT COUNT(*) FROM jobs WHERE queue='tweet' AND state IN ('pending','retry_scheduled') AND next_run<=?",(time.time()+120,)).fetchone()[0]}
db.close();print(json.dumps(value))
'''
def observation(d):
    import subprocess
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    result=subprocess.run(['/usr/bin/python3','-c',DB],preexec_fn=app,capture_output=True,text=True,timeout=20,env={'PATH':'/usr/bin:/bin','HOME':'/tmp'})
    assert result.returncode==0;return json.loads(result.stdout)
def identities(d):
    values={n:d.inspect(d.cid(n)) for n in d.OTHER};assert all(v['State']['Running'] for v in values.values())
    return {n:{'id':v['Id'],'pid':v['State']['Pid'],'started_at':v['State']['StartedAt']} for n,v in values.items()}
def hashes(d,cid,names):
    code="from pathlib import Path;import hashlib,json;root=Path('/opt/lighthouse-hub');names="+repr(names)+";print(json.dumps({n:hashlib.sha256((root/n).read_bytes()).hexdigest() if (root/n).is_file() else None for n in names}))"
    return json.loads(d.run(['/usr/bin/docker','exec',cid,'python','-c',code]))
def runtime_unchanged(old,new,folder,*,hostname=None):
    assert new['Image']==old['Image']==CBC and new['State']['Running']
    assert all(new['Config'][k]==old['Config'][k] for k in ('User','Cmd','Entrypoint','WorkingDir'))
    for value in (old,new):
        assert value['Config']['Hostname']==(hostname if hostname is not None else value['Id'][:12])
        assert len({s.split('=',1)[0] for s in value['Config']['Env']})==len(value['Config']['Env'])
    assert sorted(new['Config']['Env'])==sorted(old['Config']['Env'])
    host=lambda v:{k:x for k,x in v['HostConfig'].items() if k not in ('Binds','Mounts')}
    assert host(new)==host(old)
    nonchanged=lambda v:[x for x in v['Mounts'] if x['Destination'] not in {str(ROOT/n) for n in PROD}]
    assert sorted(nonchanged(old),key=lambda x:x['Destination'])==sorted(nonchanged(new),key=lambda x:x['Destination'])
    for name in PROD:
        selected=[x for x in new['Mounts'] if x['Destination']==str(ROOT/name)]
        assert len(selected)==1 and selected[0]['Type']=='bind' and selected[0]['Source']==str(folder/name) and selected[0]['RW'] is False
def settings_probe(d,overlay):
    name='lighthouse-x-relations-preflight-'+uuid.uuid4().hex;cid=None
    cmd=['/usr/bin/docker','create','--pull=never','--name',name,'--label','lighthouse.x-relations='+MANIFEST_SHA,
        '--network','none','--read-only','--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--memory','256m','--pids-limit','64','--env','HUB_ROOT='+str(ROOT),'--env','HUB_CONFIG='+str(ROOT/'config/hub.toml'),
        '--mount','type=bind,source='+str(ROOT/'config/hub.toml')+',target='+str(ROOT/'config/hub.toml')+',readonly']
    for n in PROD:cmd+=['--mount','type=bind,source='+str(overlay/n)+',target='+str(ROOT/n)+',readonly']
    try:
        cid=d.run(cmd+[CBC,'python','-c',"from services.hub.config import Settings;s=Settings.load();assert s.config['policy_version']=='machine-tweet-v3-api';print('settings-compatible')"]).strip()
        assert len(cid)==64;d.run(['/usr/bin/docker','start',cid]);assert d.run(['/usr/bin/docker','wait',cid],timeout=45).strip()=='0'
        return True
    finally:
        if cid:
            value=d.inspect(cid);assert value['Id']==cid and value['Image']==CBC and value['Config']['Labels']['lighthouse.x-relations']==MANIFEST_SHA
            if value['State']['Running']:d.run(['/usr/bin/docker','stop','--time','10',cid])
def deploy(d,args):
    bodies,m,proof=qualification(d,args.attestation_file,args.attestation_sha256,args.archive)
    d.protected(ROOT/'compose.yaml');original=d.read(ROOT/'compose.yaml',2000000);before=d.parsed();oldcid=d.cid('tweet-worker');old=d.inspect(oldcid)
    assert old['Image']==CBC and old['State']['Running'] and old['Config']['User']=='10001:10001'
    fixed={str(p):sha(d.read(p,2000000)) for p in [ROOT/'config/hub.toml',*sorted((ROOT/'config').glob('*policy*'))] if p.is_file()}
    assert fixed[str(ROOT/'config/hub.toml')]==HUB_SHA
    others=identities(d);names=sorted({*PROD,'compositor/src/core.mjs','compositor/src/template.mjs',
        *[x['Destination'][len(str(ROOT))+1:] for x in old['Mounts'] if x['Type']=='bind' and x['Destination'].startswith(str(ROOT)+'/') and Path(x['Source']).is_file()]})
    oldhash=hashes(d,oldcid,names);assert oldhash['compositor/src/core.mjs']==CORE_SHA
    folder=INSTALL/MANIFEST_SHA;after=candidate(before,folder);checks=observation(d)
    assert checks['processing']==checks['unresolved']==checks['runnable']==0
    summary={'version':'x-relations-overlay-deployment-v1','qualification':proof,'production_sources':m['runtime_source_sha256'],
        'state':'verified_plan_only','policy_activated':False,'hub_sha256':HUB_SHA,'other_four':others,'job_rows_sha256':sha(d.wire(checks['rows'])),
        'source_hashes_before':oldhash,'fixed_config_sha256':fixed,'tweet_before':{'id':oldcid,'pid':old['State']['Pid'],'started_at':old['State']['StartedAt']},
        'settings_compatible':settings_probe(d,Path(args.attestation_file).parent/'overlay')}
    if not args.activate:return summary
    d.directory(INSTALL,0o755);d.directory(folder,0o755)
    for name,body in bodies.items():
        file=folder/name;d.safe(file)
        for parent in reversed(file.relative_to(folder).parents):
            if str(parent)!='.':d.directory(folder/parent,0o755)
        if file.exists():d.protected(file);assert d.read(file)==body
        else:d.atomic(file,body,mode=0o644)
    d.directory(STATE,0o700);area=STATE/str(uuid.uuid4());d.directory(area,0o700)
    d.atomic(area/'compose-before.yaml',original);d.atomic(area/'deployment-report.json',d.wire({**summary,'state':'activation_prepared'}))
    with d.tweet_lock():
        assert observation(d)==checks and identities(d)==others and d.read(ROOT/'compose.yaml',2000000)==original
        assert all(sha(d.read(Path(p),2000000))==v for p,v in fixed.items())
        d.compose('stop','--timeout','45','tweet-worker',timeout=60);assert not d.inspect(d.cid('tweet-worker'))['State']['Running']
        assert observation(d)==checks
        d.atomic(ROOT/'compose.yaml',d.wire(after),original=(ROOT/'compose.yaml').stat());assert d.parsed()==after
        d.atomic(area/'deployment-report.json',d.wire({**summary,'state':'compose_overlay_installed_restart_pending'}))
    start=time.time();d.up();newcid=d.cid('tweet-worker');current=d.inspect(newcid)
    expected={**oldhash,**m['runtime_source_sha256']};assert hashes(d,newcid,names)==expected
    runtime_unchanged(old,current,folder,hostname=before['services']['tweet-worker'].get('hostname'))
    ready=d.wait_ready(start);assert identities(d)==others and observation(d)['rows']==checks['rows']
    assert all(sha(d.read(Path(p),2000000))==v for p,v in fixed.items())
    result={**summary,'state':'activated_tweet_source_overlay','container_id':newcid,'ready':ready,
        'compose_after_sha256':sha(d.read(ROOT/'compose.yaml',2000000)),'jobs_unchanged':True,'evidence':str(area)}
    d.atomic(area/'deployment-report.json',d.wire(result));return result
def main():
    assert os.name=='posix' and os.geteuid()==0 and not sys.flags.optimize;os.umask(0o077)
    p=argparse.ArgumentParser();p.add_argument('--base-helper',required=True);p.add_argument('--attestation-file',required=True)
    p.add_argument('--attestation-sha256',required=True);p.add_argument('--archive',required=True);p.add_argument('--activate',action='store_true');args=p.parse_args()
    d=load_helper(args.base_helper)
    with d.deployment_lock():result=deploy(d,args)
    print(json.dumps(result))
if __name__=='__main__':
    try:main()
    except Exception as error:print(json.dumps({'state':'stopped_reconcile_before_retry','error_class':type(error).__name__,'automatic_rollback':False}));raise SystemExit(2) from None
