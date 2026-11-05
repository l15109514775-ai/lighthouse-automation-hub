"""Default-plan isolated tweet/API V4 configuration; the original video hub is preserved."""
from copy import deepcopy
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,sys,time,tomllib,types,uuid

ROOT=Path('/opt/lighthouse-hub');INSTALL=Path('/usr/local/lib/lighthouse-x-relations-v1')
STATE=Path('/var/lib/lighthouse-x-relations-v4');CONFIG=ROOT/'config/tweet-runtime-v4.toml'
POLICY=ROOT/'config/machine-tweet-policy-api-relations-v1.json'
POLICY_SHA='c056b1b0d6c3232f431539e4585c130eb1b16419ca0c34543a58bd4a67220941'
OPERATOR_SHA='0518420bb62817b8d4384b8868ad7ff96fcae3d4c274efc3ae68cd21ef8e6f64'
SCHEDULER='scheduler/service.py';SCHEDULER_SHA='2afcaf01cf53f702768ccf6d90517a4730b25b8bec33ea261d66662f8c4143c4'
TARGETS={'2116836170986664205':'1265902865598119961','2116863407962108290':'1265903024571678722'}
SERVICES=('api','tweet-worker','scheduler');UNCHANGED=('video-worker','system-worker')
def sha(raw):return hashlib.sha256(raw).hexdigest()
def load(file):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents))
    with file.open('rb') as stream:raw=stream.read(200001)
    assert sha(raw)==OPERATOR_SHA;m=types.ModuleType('qualified_x_overlay');m.__file__=str(file)
    exec(compile(raw,str(file),'exec'),m.__dict__);return m
def shadow_config(raw):
    original=tomllib.loads(raw.decode('utf-8-sig'));assert original['policy_version']=='machine-tweet-v3-api'
    assert original['tweet_poll_minutes']==30 and original['target_bilibili_site_uid']=='1000000000000000'
    assert original['x_api_enabled'] is True and original['x_web_enabled'] is False and original['machine_tweet_enabled'] is True
    hosts=original['allowed_media_hosts'];assert isinstance(hosts,list) and hosts and 'abs.twimg.com' not in hosts
    text=raw.decode('utf-8-sig');text,count=re.subn(r'(?m)^policy_version\s*=.*$', 'policy_version = "machine-tweet-v4-api-relations"',text);assert count==1
    text,count=re.subn(r'(?m)^allowed_media_hosts\s*=.*$', 'allowed_media_hosts = '+json.dumps([*hosts,'abs.twimg.com']),text);assert count==1
    body=(b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'')+text.encode()
    changed=tomllib.loads(body.decode('utf-8-sig'));expected={**original,'policy_version':'machine-tweet-v4-api-relations','allowed_media_hosts':[*hosts,'abs.twimg.com']}
    assert changed==expected;return body
def candidate(before,qualified,scheduler_file):
    after=deepcopy(before)
    for service in ('api','tweet-worker'):
        value=after['services'][service];config=[v for v in value['volumes'] if v.get('target')==str(ROOT/'config')]
        assert len(config)==1 and config[0]['type']=='bind' and config[0]['source']==str(ROOT/'config') and config[0].get('read_only') is True
        value['environment']={**value.get('environment',{}),'HUB_CONFIG':str(CONFIG)}
    api=after['services']['api'];targets={str(ROOT/n) for n in qualified}
    assert len(qualified)==14
    for name,file in qualified.items():
        selected=[v for v in api['volumes'] if v.get('target')==str(ROOT/name)]
        assert len(selected)<=1 and all(v['type']=='bind' and v.get('read_only') is True for v in selected)
    api['volumes']=[v for v in api['volumes'] if v.get('target') not in targets]
    api['volumes'] += [{'type':'bind','source':str(file),'target':str(ROOT/name),'read_only':True,'bind':{'create_host_path':False}} for name,file in qualified.items()]
    scheduler=after['services']['scheduler'];assert not any(v.get('target')==str(ROOT/SCHEDULER) for v in scheduler['volumes'])
    scheduler['volumes'].append({'type':'bind','source':str(scheduler_file),'target':str(ROOT/SCHEDULER),'read_only':True,'bind':{'create_host_path':False}})
    assert all(after['services'][s]==before['services'][s] for s in UNCHANGED)
    return after
DB=r'''
import json,os,sqlite3
from pathlib import Path
assert os.getuid()==10001
db=sqlite3.connect(Path('/opt/lighthouse-hub/data/hub.sqlite3').as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
rows=[dict(r) for r in db.execute("SELECT p.*,j.state job_state,j.external_publish_id,json_extract(j.result,'$.website_publication_evidence') evidence,json_extract(j.result,'$.machine_web.artifact_sha256') artifact_sha256 FROM publications p JOIN jobs j ON j.job_id=p.job_id WHERE p.platform='bilibili' AND p.source_platform='x' AND p.source_id IN ('2116836170986664205','2116863407962108290') AND p.account_id='site_uid:1000000000000000' ORDER BY p.source_id")]
legacy=[dict(r) for r in db.execute("SELECT job_id,source_id,state FROM jobs j WHERE source_platform='x' AND job_type='tweet_process' AND (policy_version='machine-tweet-v3-api' OR policy_version LIKE 'machine-tweet-v3-api:source-%') AND state IN ('pending','processing','retry_scheduled','upload_pending','auth_expired','awaiting_review') AND CAST(strftime('%s',json_extract(payload,'$.post.created_at')) AS INTEGER)>=CAST(strftime('%s','now') AS INTEGER)-7200 AND NOT EXISTS(SELECT 1 FROM publications p WHERE p.platform='bilibili' AND p.source_platform='x' AND p.source_id=j.source_id AND p.account_id='site_uid:1000000000000000' AND p.state<>'rejected')")]
print(json.dumps({'publications':rows,'unpublished_fresh_v3':legacy}));db.close()
'''
def published(d):
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    proc=subprocess.run(['/usr/bin/python3','-c',DB],preexec_fn=app,capture_output=True,text=True,timeout=20,env={'PATH':'/usr/bin:/bin','HOME':'/tmp'})
    assert proc.returncode==0;value=json.loads(proc.stdout);assert not value['unpublished_fresh_v3']
    rows=value['publications'];assert len(rows)==2 and {r['source_id'] for r in rows}==set(TARGETS)
    result=[]
    for row in rows:
        assert row['kind']=='dynamic_website_machine_v4_api_relations' and row['state']==row['job_state']=='completed'
        assert row['external_id']==row['external_publish_id']==TARGETS[row['source_id']] and row['external_url']=='https://t.bilibili.com/'+row['external_id']
        ref=json.loads(row['evidence']);file=d.safe(Path(ref['file']));assert file.is_relative_to(ROOT/'media')
        raw=d.read(file,2000000);assert sha(raw)==ref['sha256'];e=json.loads(raw)
        assert e['site_uid']=='1000000000000000' and e['url']==row['external_url'] and e['artifact_sha256']==row['artifact_sha256'] and e['visible_in_owner_browser'] is True
        screenshot=d.safe(Path(e['screenshot_file']));assert screenshot.is_relative_to(ROOT/'media') and sha(d.read(screenshot,20000000))==e['screenshot_sha256']
        result.append({'source_id':row['source_id'],'job_id':row['job_id'],'url':row['external_url'],'evidence':ref})
    return result
def identities(d):
    values={s:d.inspect(d.cid(s)) for s in UNCHANGED};assert all(v['State']['Running'] for v in values.values())
    return {s:{'id':v['Id'],'pid':v['State']['Pid'],'started_at':v['State']['StartedAt']} for s,v in values.items()}
def fingerprints(d):
    result={}
    for name in SERVICES:
        value=d.inspect(d.cid(name))
        result[name]={'id':value['Id'],'image':value['Image'],'pid':value['State']['Pid'],'started_at':value['State']['StartedAt'],
            'config_fields_sha256':{k:sha(d.wire(v)) for k,v in value['Config'].items()},
            'host_fields_sha256':{k:sha(d.wire(v)) for k,v in value['HostConfig'].items()},
            'mounts':[{k:v[k] for k in ('Type','Source','Destination','RW') if k in v} for v in value['Mounts']]}
    return result
def delta_qualification(d,args):
    raw=d.read(Path(args.delta_evidence),300000);assert sha(raw)==args.delta_sha256;a=json.loads(raw)
    assert a['version']=='x-relations-v4-normal-delta-tests-v1' and a['passed'] is True and a['tests']==4
    assert a['failures']==a['errors']==a['skipped']==a['exit_code']==0 and a['network']=='none'
    assert a['production_data_mounted'] is a['credentials_injected'] is False and a['read_only'] is True
    assert a['sources'][SCHEDULER]==SCHEDULER_SHA and a['sources']['scripts/ops/activate_x_relations_v4.py']==sha(Path(__file__).read_bytes())
    junit=d.read(Path(a['junit_file']),1000000);assert sha(junit)==a['junit_sha256']
    import xml.etree.ElementTree as ET
    cases=list(ET.fromstring(junit).iter('testcase'));assert len(cases)==4 and all(c.find(k) is None for c in cases for k in ('failure','error','skipped'))
    return {'file':str(args.delta_evidence),'sha256':args.delta_sha256,'sources':a['sources']}
def preflight(d,m,area,files):
    name='lighthouse-x-v4-config-'+uuid.uuid4().hex;cid=None
    cmd=['/usr/bin/docker','create','--pull=never','--name',name,'--label','lighthouse.x-v4-config=1','--network','none','--read-only','--user','10001:10001','--cap-drop','ALL',
        '--security-opt','no-new-privileges:true','--memory','256m','--pids-limit','64','--env','HUB_ROOT='+str(ROOT),'--env','HUB_CONFIG=/run/tweet-config.toml']
    for source,target in [(area/'tweet-runtime-v4.toml',Path('/run/tweet-config.toml')),(POLICY,POLICY),*[(p,ROOT/n) for n,p in files.items()]]:
        cmd+=['--mount','type=bind,source='+str(source)+',target='+str(target)+',readonly']
    code="from services.hub.config import Settings;from services.hub.machine_tweets_api import MachineTweetsAPIReference;s=Settings.load();p=MachineTweetsAPIReference(s,None).policy();assert s.config['policy_version']=='machine-tweet-v4-api-relations' and s.config['tweet_poll_minutes']==30 and p['max_post_age_seconds']==7200;print('valid')"
    try:
        cid=d.run(cmd+[m.CBC,'python','-c',code]).strip();assert len(cid)==64;d.run(['/usr/bin/docker','start',cid]);assert d.run(['/usr/bin/docker','wait',cid],timeout=45).strip()=='0'
    finally:
        if cid:
            actual=d.inspect(cid);assert actual['Image']==m.CBC and actual['Config']['Labels']['lighthouse.x-v4-config']=='1'
            if actual['State']['Running']:d.run(['/usr/bin/docker','stop','--time','10',cid])
def deploy(args):
    m=load(args.operator_file);d=m.load_helper(args.base_helper);qualification=delta_qualification(d,args)
    _,manifest,original_qualification=m.qualification(d,args.attestation_file,args.attestation_sha256,args.archive)
    path=ROOT/'compose.yaml';d.protected(path);original=d.read(path,2000000);before=d.parsed()
    hub=d.read(ROOT/'config/hub.toml',2000000);assert sha(hub)==m.HUB_SHA;shadow=shadow_config(hub)
    d.protected(POLICY);assert sha(d.read(POLICY))==POLICY_SHA
    accepted=published(d);protected=identities(d);folder=INSTALL/m.MANIFEST_SHA;files={n:folder/n for n in manifest['production_files']}
    for n,file in files.items():d.protected(file);assert sha(d.read(file))==manifest['runtime_source_sha256'][n]
    assert m.hashes(d,d.cid('tweet-worker'),list(files))==manifest['runtime_source_sha256']
    d.directory(STATE,0o700);area=STATE/str(uuid.uuid4());d.directory(area,0o700)
    d.atomic(area/'tweet-runtime-v4.toml',shadow,mode=0o644)
    scheduler=d.read(Path(args.scheduler_file));assert sha(scheduler)==SCHEDULER_SHA
    target=INSTALL/'normal-v4-scheduler'/SCHEDULER;after=candidate(before,files,target)
    preflight(d,m,area,files)
    old_runtime={s:d.inspect(d.cid(s)) for s in SERVICES};assert all(v['State']['Running'] and v['Image']==m.CBC for v in old_runtime.values())
    summary={'version':'x-relations-normal-v4-deployment-v1','state':'verified_plan_only','original_qualification':original_qualification,'delta_qualification':qualification,
        'accepted_publications':accepted,'shadow_config_sha256':sha(shadow),'global_hub_sha256':m.HUB_SHA,'protected_video_system':protected,
        'services_changed':list(SERVICES),'source_policy':'machine-tweet-v4-api-relations','normal_age_seconds':7200,'poll_minutes':30,'history_cursors_reset':False}
    d.atomic(area/'deployment-report.json',d.wire(summary))
    if not args.activate:return area,summary
    for parent in (INSTALL/'normal-v4-scheduler',target.parent):d.directory(parent,0o755)
    if target.exists():d.protected(target);assert d.read(target)==scheduler
    else:d.atomic(target,scheduler,mode=0o644)
    d.atomic(area/'compose-before.yaml',original);d.atomic(area/'runtime-before.json',d.wire(fingerprints(d)))
    d.atomic(area/'global-hub-before.toml',hub);d.atomic(area/'policy-before.json',d.read(POLICY))
    with d.tweet_lock():
        assert not m.observation(d)['processing'] and published(d)==accepted and identities(d)==protected
        assert d.read(path,2000000)==original and d.read(ROOT/'config/hub.toml',2000000)==hub
        d.compose('stop','--timeout','45','scheduler',timeout=60);d.compose('stop','--timeout','45','api','tweet-worker',timeout=90)
        d.safe(CONFIG)
        if CONFIG.exists():d.protected(CONFIG);assert d.read(CONFIG,2000000)==shadow and CONFIG.stat().st_gid==10001 and stat.S_IMODE(CONFIG.stat().st_mode)==0o640
        else:d.atomic(CONFIG,shadow,mode=0o640);os.chown(CONFIG,0,10001)
        d.atomic(path,d.wire(after),original=path.stat());assert d.parsed()==after
        d.atomic(area/'deployment-report.json',d.wire({**summary,'state':'new_config_installed_restart_pending'}))
    start=time.time();d.compose('up','-d','--no-deps','--no-build','--pull','never','--force-recreate',*SERVICES,timeout=180)
    for service in SERVICES:
        actual=d.inspect(d.cid(service));old=old_runtime[service]
        assert actual['Image']==m.CBC and actual['State']['Running'] and actual['Config']['User']==old['Config']['User']=='10001:10001'
        for key in ('Cmd','Entrypoint','WorkingDir'):assert actual['Config'][key]==old['Config'][key]
        old_env=dict(v.split('=',1) for v in old['Config']['Env'])
        if service in ('api','tweet-worker'):old_env['HUB_CONFIG']=str(CONFIG)
        assert dict(v.split('=',1) for v in actual['Config']['Env'])==old_env
        for key in ('Memory','MemorySwap','NanoCpus','PidsLimit','ShmSize','ReadonlyRootfs','Init','CapDrop','Privileged','SecurityOpt','PortBindings','Tmpfs'):
            assert actual['HostConfig'][key]==old['HostConfig'][key]
        wanted={v['target']:(v['source'],not v.get('read_only',False)) for v in after['services'][service]['volumes']}
        observed={v['Destination']:(v['Source'],v['RW']) for v in actual['Mounts'] if v['Type']=='bind'}
        assert observed==wanted
    for service in ('api','tweet-worker'):
        cid=d.cid(service);actual=d.inspect(cid);assert actual['Image']==m.CBC and actual['State']['Running']
        assert dict(v.split('=',1) for v in actual['Config']['Env'])['HUB_CONFIG']==str(CONFIG)
        assert m.hashes(d,cid,list(files))==manifest['runtime_source_sha256']
    assert m.hashes(d,d.cid('scheduler'),[SCHEDULER])=={SCHEDULER:SCHEDULER_SHA}
    ready=d.wait_ready(start);assert identities(d)==protected and d.read(ROOT/'config/hub.toml',2000000)==hub
    d.atomic(area/'runtime-after.json',d.wire(fingerprints(d)))
    result={**summary,'state':'normal_v4_active_first_official_poll_not_yet_asserted','ready':ready,'shadow_config':str(CONFIG)}
    d.atomic(area/'deployment-report.json',d.wire(result));return area,result
def main():
    assert os.name=='posix' and os.geteuid()==0 and not sys.flags.optimize;os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('operator-file','base-helper','archive','attestation-file','attestation-sha256','scheduler-file','delta-evidence','delta-sha256'):p.add_argument('--'+name,required=True)
    p.add_argument('--activate',action='store_true');args=p.parse_args();m=load(args.operator_file);d=m.load_helper(args.base_helper)
    with d.deployment_lock():area,result=deploy(args)
    print(json.dumps({'state':result['state'],'evidence':str(area),'global_hub_changed':False,'video_changed':False}))
if __name__=='__main__':
    try:main()
    except Exception as error:print(json.dumps({'state':'stopped_reconcile_before_retry','error_class':type(error).__name__,'automatic_rollback':False}));raise SystemExit(2) from None
