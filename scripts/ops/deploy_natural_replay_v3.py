"""Plan/activate tested natural replay workers after genuine pilot acceptance.

Never admits a job from the host, resets data, buys resources or alters other
services. Candidate authorization bytes must come from the real pilot issuer.
"""
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,tempfile,time,tomllib,types,uuid
import urllib.request
import xml.etree.ElementTree as ET

IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
HELPER_SHA='50144dbe19d38329b549ca7fb5e64e2ea9454a07d4b48fd5ca02bd6efcf35cb1'
ENTRY='services.hub.replay_natural_worker_runtime_v3'
RELEASE='aa1b6494bdde6898c90b656d934c0bd1ee418c13'
CHANNELS={'UCbbbbbbbbbbbbbbbbbbbbbb','UCaaaaaaaaaaaaaaaaaaaaaa'}
POLICIES=('youtube-highest-natural-download-policy.toml','replay-natural-video-web-policy.toml')
GRANT=Path('/etc/lighthouse-hub/replay-natural-workflow-launch-v3.json')
ACCEPTANCE=Path('/etc/lighthouse-hub/replay-natural-acceptance-v3/pilot.json')
SPECS={
 'natural':('replay-natural-worker-ubuntu-tests-v3','replay-natural-worker-ro-overlay-v3','replay-natural-worker-ro-overlay-v3-','lighthouse-replay-natural-owned-v3-',85,111,
  '5fb66ea8a9ba763e0816fa2358c4cdedfad55b95cd549dc5d30384f13c3cea38'),
 'download':('youtube-natural-readiness-v1d-ubuntu-tests','youtube-natural-readiness-v1d-ro-overlay','youtube-natural-readiness-v1d-','lighthouse-youtube-highest-owned-',280,66,
  '2ddfad8c1056c7c22eacbc81ae23da14d4be0527d2aa77d0c911c7df8ebf5fdb'),
 'integration':('natural-runtime-readiness-v3-ubuntu-tests','natural-runtime-readiness-v3-ro-overlay','natural-runtime-readiness-v3-ro-overlay-','lighthouse-natural-readiness-runtime-owned-v3-',21,116,
  '9169dbe158eb7e41818e5c303d84df0977ffb90e76d606f96565b49776d153e5')}
FLAGS={'youtube_natural_download_enabled':True,'youtube_download_acceptance_only':True,'resource_wait_max_hours':72,'youtube_readiness_max_hours':24}

def digest(raw):return hashlib.sha256(raw).hexdigest()
def protected(file):
    assert file.is_absolute() and file.resolve()==file
    for entry in (file,*file.parents):
        info=entry.lstat();assert not stat.S_ISLNK(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022

def helper(file):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not file.is_symlink()
    raw=file.read_bytes();assert digest(raw)==HELPER_SHA
    result=types.ModuleType('pinned_highest_deployer');result.__file__=str(file);exec(compile(raw,str(file),'exec'),result.__dict__);return result

def gate(path,sha,kind,d):
    version,mversion,prefix,ownedprefix,total,filecount,archive=SPECS[kind]
    path=d.safe(path);assert path.name=='test-attestation.json' and path.parent.parent==d.EV and path.parent.name.startswith(prefix)
    ident=path.parent.name[len(prefix):];assert str(uuid.UUID(ident))==ident
    assert re.fullmatch('[a-f0-9]{64}',sha) and digest(d.read(path,500000))==sha
    value=json.loads(d.read(path,500000));m=value['manifest']
    assert value['version']==version and value['image']==IMAGE and value['full_suite_passed'] is True
    assert value['archive_sha256']==archive and value['tests']==total and value['failures']==value['errors']==value['skipped']==value['exit_code']==0
    assert value['oom_killed'] is False and value['network']=='none'
    assert all(value[k] is True for k in ('runtime_readonly_overlays_only','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','no_pid_or_oom_denial'))
    assert all(value[k] is False for k in ('whole_tree_deployed','application_data_mounted','production_profile_mounted','secrets_environment_injected'))
    assert m['version']==mversion and m['base_image']==IMAGE and m['expected_total_tests']==total and len(m['files'])==filecount
    assert m['expected_test_counts']==value['groups'] and sum(value['groups'].values())==total
    assert m['whole_tree_deployment'] is m['application_data_included'] is m['credentials_included'] is False
    owned=d.HOME/(ownedprefix+ident);raw=d.read(owned/'results.xml',20_000_000);assert digest(raw)==value['junit_sha256']
    cases=list(ET.fromstring(raw).iter('testcase'));counts={name:0 for name in value['groups']}
    assert len(cases)==total and all(not any(c.find(k) is not None for k in ('failure','error','skipped')) for c in cases)
    for c in cases:
        matched=[name for name in counts if c.attrib.get('classname','').endswith(Path(name).stem)];assert len(matched)==1;counts[matched[0]]+=1
    assert counts==value['groups'] and digest(d.read(path.parent/'tests.log',20_000_000))==value['tests_log_sha256']
    assert json.loads(d.read(path.parent/'overlay/manifest.json'))==m
    for name,record in m['files'].items():
        assert not Path(name).is_absolute() and '..' not in Path(name).parts
        raw=d.read(path.parent/'overlay'/name,500000);assert len(raw)==record['bytes'] and digest(raw)==record['sha256']
    container=json.loads(d.read(path.parent/'container.json',5_000_000));host=container['HostConfig']
    assert container['Id']==value['container_id'] and container['Image']==IMAGE and container['Config']['User']=='10001:10001'
    assert container['State']['ExitCode']==0 and container['State']['OOMKilled'] is False and host.get('Init') is True
    assert host['ReadonlyRootfs'] is True and host['NetworkMode']=='none' and host['PidsLimit']==256 and host['Memory']==1400*1024**2 and host['ShmSize']==256*1024**2
    expected={str(owned),str(path.parent/'check-and-test.py'),str(path.parent/'overlay/manifest.json')}|{str(path.parent/'overlay'/name) for name in m['files']}
    binds=[x for x in container['Mounts'] if x['Type']=='bind'];assert len(binds)==len(expected) and {x['Source'] for x in binds}==expected
    assert all(not x['RW'] or x['Source']==str(owned) for x in binds)
    assert all(x['Type']=='bind' or x['Type']=='tmpfs' and x['Destination']=='/tmp' for x in container['Mounts'])
    for label in ('before','after'):
        resources=json.loads(d.read(owned/('resource-'+label+'.json')));assert resources==value['resource_snapshot_'+label]
        assert int(resources['pids.events'].get('max',0))==int(resources['memory.events'].get('oom',0))==int(resources['memory.events'].get('oom_kill',0))==0
    return value,{'file':str(path),'sha256':sha,'tests':total}

def merged_sources(base,download,integration):
    old=base['manifest']['files'];new=integration['manifest']['files'];d=download['manifest']['files']
    assert integration['manifest']['source_freeze_commit']==download['manifest']['source_commit']==RELEASE
    assert integration['manifest']['previous_85_archive_sha256']==base['archive_sha256']
    assert set(old)<=set(new) and set(d)<=set(new)
    assert all(new[name]['sha256']==entry['sha256'] for name,entry in d.items())
    changed={name for name in new if name not in old or new[name]['sha256']!=old[name]['sha256']}
    assert changed==set(integration['manifest']['changed_from_85'])
    return new

def candidate_hub(raw):
    text=raw.decode('utf-8-sig');before=tomllib.loads(text);lines=text.splitlines(keepends=True);ending='\r\n' if '\r\n' in text else '\n'
    boundary=next((i for i,line in enumerate(lines) if line.lstrip().startswith('[')),len(lines));head=lines[:boundary];tail=lines[boundary:]
    for name,value in FLAGS.items():
        matched=[i for i,line in enumerate(head) if re.match(r'^\s*'+re.escape(name)+r'\s*=',line)]
        assert len(matched)<=1;encoded='true' if value is True else str(value)
        if matched:
            i=matched[0];head[i]=re.sub(r'(^\s*'+re.escape(name)+r'\s*=\s*)(?:true|false|[0-9]+)',lambda m:m[1]+encoded,head[i],count=1)
        else:
            if head and not head[-1].endswith(('\n','\r')):head[-1]+=ending
            head.append(name+' = '+encoded+ending)
    output=''.join(head+tail);expected=deepcopy(before);expected.update(FLAGS);assert tomllib.loads(output)==expected
    return (b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'')+output.encode()

def stage(directory,d):
    directory=d.safe(directory);protected(directory);assert directory.is_dir()
    sources={name:directory/name for name in POLICIES};sources.update(grant=directory/'workflow-launch.json',snapshot=directory/'pilot.json')
    bodies={}
    for name,file in sources.items():
        protected(file);info=file.stat();assert info.st_gid==10001 and stat.S_IMODE(info.st_mode)==0o640
        bodies[name]=d.read(file,1048576 if name=='snapshot' else 65536)
    download,replay=(tomllib.loads(bodies[name].decode('utf-8-sig')) for name in POLICIES)
    grant=json.loads(bodies['grant']);snapshot=json.loads(bodies['snapshot'])
    start=datetime.fromisoformat(download['activated_at'].replace('Z','+00:00'));other=datetime.fromisoformat(replay['activated_at'].replace('Z','+00:00'))
    assert start.tzinfo is not None and start==other and set(download['allowed_channel_ids'])==set(replay['allowed_channel_ids'])==CHANNELS
    assert download['daily_http_requests']==6000 and download['monthly_http_requests']==150000
    assert download['enabled'] is replay['enabled'] is replay['automation_enabled'] is True
    assert download['operating_not_before']==replay['operating_not_before']=='2026-10-31T16:00:00Z'
    assert grant['policy_sha256']==digest(bodies[POLICIES[1]]) and grant['pilot_acceptance_snapshot_sha256']==digest(bodies['snapshot'])
    assert snapshot['accepted_at']==grant['accepted_at'] and start.timestamp()<=datetime.fromisoformat(grant['accepted_at'].replace('Z','+00:00')).timestamp()
    return sources,bodies,start.timestamp()

PRECHECK=r'''
from pathlib import Path
import json,os,socket,threading
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.worker import Worker
from services.hub.replay_moderation_common import QueueView
def refuse(*args,**kwargs):raise RuntimeError('NATURAL_PREFLIGHT_CONSUMPTION_OR_NETWORK_FORBIDDEN')
socket.socket.connect=refuse;Store.claim=Store.recover=Store.enqueue=Store.finish=refuse
Worker.once=Worker.run=QueueView.claim=QueueView.recover=threading.Thread.start=refuse
from services.hub.replay_natural_worker_runtime_v3 import build_worker
from services.hub.replay_worker_runtime import load_youtube_key
assert os.getuid()==os.getgid()==10001
load_youtube_key('/run/secrets/youtube-api-key')
s=Settings.load();db=Store('/run/preflight-db/hub.sqlite3');db.init()
worker,service,downloader=build_worker(s,db)
worker.resource_waits.initialize();worker.readiness_waits.initialize()
assert {'youtube_highest_prepare_v3','youtube_download_v3','replay_natural_video_publish_v3','youtube_highest_readiness_observation_v3'}<=set(worker.handlers)
assert db.query('PRAGMA integrity_check')[0]['integrity_check']=='ok' and not db.query('PRAGMA foreign_key_check')
print(json.dumps({'ready':True,'policy_sha256':service.policy()['_sha256'],'claim_called':False,'network_enabled':False,'only_backup_initialized':True}))
'''

def preflight(d,overlay,code,key,sources,hub):
    command=['/usr/bin/docker','run','--rm','--pull=never','--network=none','--read-only','--user','10001:10001','--cap-drop=ALL','--security-opt','no-new-privileges:true',
        '--init','--pids-limit=256','--memory=1400m','--shm-size=256m','--tmpfs','/tmp:rw,size=192m,mode=1777','-e','HUB_ROOT='+str(d.ROOT),
        '-e','HUB_CONFIG='+str(d.ROOT/'config/hub.toml'),'-e','HOME=/tmp/natural-preflight','-e','PYTHONDONTWRITEBYTECODE=1']
    mounts=[(d.ROOT/'config',d.ROOT/'config'),(key,'/run/secrets/youtube-api-key'),(sources['grant'],GRANT),(sources['snapshot'],ACCEPTANCE)]
    mounts.extend((sources[name],d.ROOT/'config'/name) for name in POLICIES)
    mounts.extend((overlay/name,d.ROOT/name) for name in code)
    with tempfile.TemporaryDirectory(prefix='lighthouse-natural-preflight-',dir='/run') as folder:
        folder=Path(folder);folder.chmod(0o755);dbfolder=folder/'database';dbfolder.mkdir(mode=0o700)
        file,backup=d.online_snapshot(dbfolder);os.chown(file,10001,10001);file.chmod(0o600);os.chown(dbfolder,10001,10001);dbfolder.chmod(0o700)
        candidate=folder/'hub.toml';candidate.write_bytes(hub);os.chown(candidate,0,10001);candidate.chmod(0o640);mounts.append((candidate,d.ROOT/'config/hub.toml'))
        for source,target in mounts:command+=['--mount','type=bind,source='+str(source)+',target='+str(target)+',readonly']
        command+=['--mount','type=bind,source='+str(dbfolder)+',target=/run/preflight-db']
        result=json.loads(d.run(command+[IMAGE,'python','-c',PRECHECK],timeout=90));assert result['ready'] is True
        result['database_snapshot']=backup
    return result

QUERY=r'''
from pathlib import Path
import json,os,sqlite3,sys
assert os.getuid()==10001
db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True);db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
def scalar(sql,args=()):return db.execute(sql,args).fetchone()[0]
counts={'video_processing':scalar("SELECT COUNT(*) FROM jobs WHERE queue='video' AND state='processing'"),
 'old_lookup_runnable':scalar("SELECT COUNT(*) FROM jobs WHERE job_type='replay_highest_moderation_lookup_v2b' AND state IN ('pending','processing','retry_scheduled')"),
 'old_unresolved_publish':scalar("SELECT COUNT(*) FROM jobs WHERE job_type='replay_highest_video_publish_v2' AND state NOT IN ('completed','failed')"),
 'natural_jobs':scalar("SELECT COUNT(*) FROM jobs WHERE job_type IN ('youtube_highest_prepare_v3','youtube_download_v3','replay_natural_video_publish_v3','youtube_highest_readiness_observation_v3','replay_natural_moderation_lookup_v3')"),
 'pilot_completed':scalar("SELECT COUNT(*) FROM jobs WHERE job_id=? AND state='completed' AND job_type='replay_highest_video_publish_v2'",(sys.argv[2],))}
counts['heartbeats']={name:at for name,at in db.execute("SELECT name,at FROM heartbeats WHERE name IN ('worker:video','worker:video-main-v3','worker:video-moderation-v3')")}
print(json.dumps(counts));db.close()
'''

def database_status(d,pilot):
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    result=subprocess.run(['/usr/bin/python3','-c',QUERY,str(d.ROOT/'data/hub.sqlite3'),pilot],capture_output=True,text=True,timeout=20,
        preexec_fn=app,cwd='/tmp',env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    assert result.returncode==0;return json.loads(result.stdout)

def idle(status):return status['video_processing']==status['old_lookup_runnable']==status['old_unresolved_publish']==0 and status['pilot_completed']==1

def core_processes(d):
    result={}
    for name in d.CORE:
        value=d.inspect(d.container_id(name));assert value['State']['Running'] and not value['State']['OOMKilled']
        result[name]={'id':value['Id'],'pid':value['State']['Pid'],'started_at':value['State']['StartedAt']}
    return result

def current_gate(d,before,current,legacy):
    service=deepcopy(before['services']['video-worker'])
    for item in service['volumes']:item.setdefault('read_only',False)
    d.validate_running(current,service,{})
    assert service['command'] in (['python','-m',d.ENTRY],['python','-m','services.hub.replay_highest_moderation_v2b'])
    known={name:entry['sha256'] for name,entry in legacy['manifest']['files'].items() if d.runtime_source(name)};assert len(known)==42
    handler='services/hub/replay_video_browser.py'
    extra={'services/hub/replay_moderation_common.py':'beb29443f24316c37859d4fdba94f4a0a9ad505a327af58323ac30f26728c9e1',
        'services/hub/replay_highest_moderation_v2b.py':'f285c85f2d576d221f5b760902c661e8942c51fd6eb4f7e33a7e416baa5529cd'}
    codevolumes={v['target']:v for v in service['volumes'] if v['target'].startswith(str(d.ROOT)+'/') and d.runtime_source(v['target'][len(str(d.ROOT))+1:])}
    expected=set(known)|(set(extra) if service['command'][-1].endswith('moderation_v2b') else set())
    assert set(codevolumes)=={str(d.ROOT/name) for name in expected}
    for name in expected:
        item=codevolumes[str(d.ROOT/name)];source=Path(item['source']);protected(source);assert item['read_only'] is True
        if name in extra:
            assert source==d.INSTALL/'deltas'/('moderation-8ea790fdcf5f6a6625d36c5fc309d57d37abc66d')/name
            expected_sha=extra[name]
        elif name==handler and source!=d.INSTALL/'code'/name:
            assert source in (d.INSTALL/'deltas'/'243855ac107715350b59941b4118b415ac416b7d'/name,
                d.INSTALL/'deltas'/'moderation-8ea790fdcf5f6a6625d36c5fc309d57d37abc66d'/name)
            expected_sha='a7cd7e743d50c7be226c5b087422b825707676494df974d4c7027d5bc9d52446'
        else:assert source==d.INSTALL/'code'/name;expected_sha=known[name]
        assert digest(d.read(source,500000))==expected_sha

def install_file(d,file,body,*,mode=0o640,group=10001):
    # Reject a bad pre-existing ancestor before mkdir/chmod can follow it.
    d.safe(file)
    for ancestor in file.parents:
        if ancestor.exists():protected(ancestor)
    file.parent.mkdir(parents=True,mode=0o750,exist_ok=True)
    for directory in (file.parent,*file.parent.parents):
        if directory in (Path('/'),Path('/etc'),Path('/opt'),Path('/usr'),Path('/usr/local'),Path('/usr/local/lib')):break
        info=directory.lstat();assert info.st_uid==0 and not stat.S_ISLNK(info.st_mode) and not info.st_mode&0o022
        if directory.is_relative_to(d.INSTALL):directory.chmod(0o755)
        elif directory.is_relative_to(Path('/etc/lighthouse-hub')):os.chown(directory,0,10001);directory.chmod(0o750)
    d.atomic(file,body,mode=mode);os.chown(file,0,group);file.chmod(mode);protected(file)
    for path in (file,file.parent):
        fd=os.open(path,os.O_RDONLY|(os.O_DIRECTORY if path.is_dir() else 0))
        try:os.fsync(fd)
        finally:os.close(fd)

def role_lock(d,name):
    """Open app-data locks through directory descriptors, never root chown
    through an app-writable symlink. Existing ordinary locks stay untouched.
    """
    assert name in ('video','moderation');protected(d.ROOT)
    flags=os.O_RDONLY|getattr(os,'O_DIRECTORY',0)|getattr(os,'O_NOFOLLOW',0);opened=[];returned=None
    try:
        root=os.open(d.ROOT,flags);opened.append(root)
        data=os.open('data',flags,dir_fd=root);opened.append(data)
        locks=os.open('locks',flags,dir_fd=data);opened.append(locks)
        created=False
        try:os.mkdir(name,mode=0o700,dir_fd=locks);created=True
        except FileExistsError:pass
        folder=os.open(name,flags,dir_fd=locks);opened.append(folder);info=os.fstat(folder)
        if created:
            assert info.st_uid==0;os.fchown(folder,10001,10001);os.fchmod(folder,0o700)
        else:assert info.st_uid==10001 and not info.st_mode&0o022
        try:lock=os.open('observer.lock',os.O_RDONLY|getattr(os,'O_NOFOLLOW',0),dir_fd=folder)
        except FileNotFoundError:
            lock=os.open('observer.lock',os.O_RDWR|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600,dir_fd=folder)
            os.fchown(lock,10001,10001);os.fchmod(lock,0o600)
        opened.append(lock);info=os.fstat(lock)
        assert stat.S_ISREG(info.st_mode) and info.st_uid==10001 and not info.st_mode&0o022 and info.st_nlink==1
        returned=lock;return lock
    finally:
        for fd in reversed(opened):
            if fd!=returned:os.close(fd)

def rollback_if_safe(d,pilot,backups,original,compose_stat,processes,*,locks_held=False):
    """Stop only under both idle role locks; recheck admissions after stop.

    Producers run before Worker.once, so a pre-stop job-count check alone is
    insufficient. If a producer admitted work during shutdown, resume the
    new service and retain all new configuration instead of exposing that
    job to the old consumer.
    """
    descriptors=[]
    try:
        if not locks_held:
            import fcntl
            for name in ('video','moderation'):
                fd=role_lock(d,name)
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:os.close(fd);return False
                descriptors.append(fd)
        before=database_status(d,pilot)
        if before['natural_jobs'] or before['video_processing'] or core_processes(d)!=processes:return False
        d.compose('stop','--timeout','45','video-worker')
        after=database_status(d,pilot)
        if after['natural_jobs'] or after['video_processing']:
            d.compose('up','-d','--no-deps','--no-build','video-worker');return False
        for file,previous in backups.items():
            if previous is None:file.unlink(missing_ok=True)
            else:d.atomic(file,previous[0],original=previous[1])
        d.atomic(d.ROOT/'compose.yaml',original,original=compose_stat);d.compose('up','-d','--no-deps','--no-build','video-worker')
        return True
    finally:
        for fd in reversed(descriptors):os.close(fd)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for kind in ('natural','download','integration','legacy'):
        parser.add_argument('--'+kind+'-attestation',required=True);parser.add_argument('--'+kind+'-sha256',required=True)
    parser.add_argument('--candidate-directory',required=True);parser.add_argument('--deployer-file',required=True);parser.add_argument('--activate',action='store_true')
    args=parser.parse_args();assert os.name=='posix' and os.geteuid()==0;d=helper(args.deployer_file)
    values={};references={}
    for kind in SPECS:values[kind],references[kind]=gate(Path(getattr(args,kind+'_attestation')),getattr(args,kind+'_sha256'),kind,d)
    legacy,lref=d.gate(Path(args.legacy_attestation),args.legacy_sha256);references['legacy']=lref
    files=merged_sources(values['natural'],values['download'],values['integration'])
    overlay=Path(args.integration_attestation).parent/'overlay'
    code={name:d.read(overlay/name,500000) for name in files if d.runtime_source(name)}
    assert all(digest(body)==files[name]['sha256'] for name,body in code.items()) and 45<len(code)<70
    sources,bodies,activation=stage(Path(args.candidate_directory),d);pilot=json.loads(bodies['grant'])['acceptance_job_id']
    protected(d.ROOT/'compose.yaml');original=d.read(d.ROOT/'compose.yaml');compose_stat=(d.ROOT/'compose.yaml').stat();before=d.compose_config();current=d.inspect(d.container_id('video-worker'))
    already=before['services']['video-worker']['command']==['python','-m',ENTRY]
    if not already:current_gate(d,before,current,legacy)
    processes=core_processes(d)
    key=[v['source'] for v in before['services']['video-worker']['volumes'] if v['target']=='/run/secrets/youtube-api-key'];assert len(key)==1;key=d.key_file(key[0])
    hub_file=d.ROOT/'config/hub.toml';protected(hub_file);hub=d.read(hub_file);hub_candidate=candidate_hub(hub)
    prepared=preflight(d,overlay,code,key,sources,hub_candidate)
    status=database_status(d,pilot);assert status['pilot_completed']==1
    code_root=d.INSTALL/'natural-v3'/RELEASE/'code'
    service=d.new_video_service(key,code,code_root=code_root);service['command']=['python','-m',ENTRY]
    service['volumes']+=[d.bind(GRANT,GRANT,True),d.bind(ACCEPTANCE,ACCEPTANCE,True)]
    proposed=d.candidate_config(before,service)
    report={'version':'natural-replay-v3-deployment-v1','state':'verified_plan_only','references':references,'runtime_sources':len(code),
        'release_commit':RELEASE,'preflight':prepared,'database_before':status,'other_core_processes':processes,'activation_at':datetime.fromtimestamp(activation,timezone.utc).isoformat(),
        'candidate_hub_sha256':digest(hub_candidate),'candidate_policy_sha256':{name:digest(bodies[name]) for name in POLICIES},
        'grant_sha256':digest(bodies['grant']),'snapshot_sha256':digest(bodies['snapshot']),'jobs_reset':False,'jobs_created_by_installer':0,
        'activated':False,'new_paid_services':False,'automatic_history_backfill':False,'top_level_config_values_changed':FLAGS}
    if already:
        normalized=deepcopy(service)
        for volume in normalized['volumes']:volume.setdefault('read_only',False)
        d.validate_running(current,normalized,{})
        assert hub==hub_candidate and all(d.read(d.ROOT/'config'/name,65536)==bodies[name] for name in POLICIES)
        assert d.read(GRANT,65536)==bodies['grant'] and d.read(ACCEPTANCE,1048576)==bodies['snapshot']
        for name,body in code.items():protected(code_root/name);assert d.read(code_root/name,500000)==body
        report.update(state='already_applied_verified',activated=True,video_container_id=current['Id']);print(json.dumps(report));return
    if not args.activate:print(json.dumps(report));return
    assert 0<=time.time()-activation<=300 # A stale plan is not 'activate now'.
    import fcntl
    globalfd=os.open('/run/lighthouse-natural-replay-v3-deploy.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600);fcntl.flock(globalfd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    descriptors=[];keep_global=False;cutover_started=False
    try:
        for name in ('video','moderation'):
            fd=role_lock(d,name)
            try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:os.close(fd);report['state']='deferred_active_video_or_lookup';print(json.dumps(report));return
            descriptors.append(fd)
        fresh=database_status(d,pilot)
        if not idle(fresh):report['state']='deferred_unresolved_or_processing_video';print(json.dumps(report));return
        assert fresh['natural_jobs']==0 and d.read(d.ROOT/'compose.yaml')==original and core_processes(d)==processes
        # Reopen private staging after preflight; never install changed bytes.
        for name,file in sources.items():assert d.read(file,1048576)==bodies[name]
        evidence=d.EV/('natural-replay-v3-deploy-'+str(uuid.uuid4()));d.private_directory(evidence)
        d.atomic(evidence/'compose-before.yaml',original);d.atomic(evidence/'video-before.json',d.wire(d.filtered(current)))
        targets={hub_file:hub_candidate,**{d.ROOT/'config'/name:bodies[name] for name in POLICIES},GRANT:bodies['grant'],ACCEPTANCE:bodies['snapshot']}
        backups={}
        for i,(file,body) in enumerate(targets.items()):
            if file.exists():protected(file);backups[file]=(d.read(file,1048576),file.stat());d.atomic(evidence/('config-before-'+str(i)),backups[file][0])
            else:backups[file]=None
        for name,body in code.items():
            file=code_root/name
            if file.exists():protected(file);assert d.read(file,500000)==body
            else:install_file(d,file,body,mode=0o644,group=0)
        d.atomic(evidence/'runtime-source-manifest.json',d.wire({name:digest(body) for name,body in code.items()}))
        cutover_started=True;d.compose('stop','--timeout','45','video-worker');assert not d.inspect(current['Id'])['State']['Running'];assert idle(database_status(d,pilot))
        for file,body in targets.items():install_file(d,file,body)
        d.atomic(d.ROOT/'compose.yaml',d.wire(proposed),original=compose_stat);d.assert_other_services(before,d.compose_config())
        started=time.time();d.compose('up','-d','--no-deps','--no-build','video-worker');updated=d.inspect(d.container_id('video-worker'));keep_global=True
    except BaseException:
        report.update(state='requires_reconciliation',automatic_rollback_performed=False)
        if cutover_started and 'backups' in locals():
            try:
                if rollback_if_safe(d,pilot,backups,original,compose_stat,processes,locks_held=True):report.update(state='rolled_back_no_natural_jobs',automatic_rollback_performed=True)
            except BaseException:report['rollback_error']='ROLLBACK_REQUIRES_OPERATOR_RECONCILIATION'
        if 'evidence' in locals():d.atomic(evidence/'deployment-report.json',d.wire(report))
        print(json.dumps(report));raise RuntimeError('NATURAL_DEPLOY_REQUIRES_RECONCILIATION') from None
    finally:
        for fd in reversed(descriptors):os.close(fd)
        if not keep_global:os.close(globalfd)
    try:
        expected=deepcopy(service)
        for volume in expected['volumes']:volume.setdefault('read_only',False)
        d.validate_running(updated,expected,{})
        assert all(digest(d.read(code_root/name,500000))==digest(body) for name,body in code.items())
        assert all(d.read(file,1048576)==body for file,body in targets.items())
        deadline=time.monotonic()+60
        while True:
            fresh=database_status(d,pilot);beats=fresh['heartbeats']
            if all(beats.get(name,0)>=started for name in ('worker:video','worker:video-main-v3','worker:video-moderation-v3')):break
            assert d.inspect(updated['Id'])['State']['Running'] and time.monotonic()<deadline;time.sleep(1)
        with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=5) as response:assert response.status==200
        assert core_processes(d)==processes;d.assert_other_services(before,d.compose_config())
        report.update(state='active',activated=True,video_container_id=updated['Id'],actual_service_started_at=updated['State']['StartedAt'],ready=True,other_core_processes_unchanged=True)
    except BaseException:
        report.update(state='requires_reconciliation',automatic_rollback_performed=False)
        # A new admitted job makes an old consumer unsafe; never reset it or
        # stop its live work to manufacture a rollback.
        try:
            if rollback_if_safe(d,pilot,backups,original,compose_stat,processes):report.update(state='rolled_back_no_natural_jobs',automatic_rollback_performed=True)
        except BaseException:report['rollback_error']='ROLLBACK_REQUIRES_OPERATOR_RECONCILIATION'
        d.atomic(evidence/'deployment-report.json',d.wire(report));os.close(globalfd);print(json.dumps(report));raise RuntimeError('NATURAL_DEPLOY_NOT_ACCEPTED') from None
    d.atomic(evidence/'video-after.json',d.wire(d.filtered(updated)));d.atomic(evidence/'deployment-report.json',d.wire(report));os.close(globalfd);print(json.dumps(report))

if __name__=='__main__':main()
