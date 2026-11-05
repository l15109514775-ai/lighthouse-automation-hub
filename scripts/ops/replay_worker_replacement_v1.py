"""Root-only replacement draft. Default plan; never admits or queues a replay.

Activation requires the real completed long download, both isolated Ubuntu
attestations, and idle queue locks. No Docker/core action occurs in plan mode.
"""
from contextlib import ExitStack,contextmanager
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,sys,time,tomllib,uuid

ROOT=Path('/opt/lighthouse-hub');EV=Path('/home/lighthouse-ops/lighthouse-evidence')
INSTALL=Path('/usr/local/lib/lighthouse-replay-video-worker-v1')
NAME='lighthouse-replay-video-worker-v1';UNIT='lighthouse-replay-video-worker-v1.service'
LEGACY='legacy-video-worker-rollback'
IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
CBC='sha256:cbc6cd5076960eba0adb723968bd10257c6691d62e3554cf48ae0ff0df5d8d10'
PILOT='9T5tn1WGlJg';DOWNLOAD='255fa97c-14ad-46ea-8e62-be833cfb5db8'
ENTRY='services/hub/replay_worker_runtime.py'
ENTRY_SHA='e593e5d9024007afa36adb9577f9dfe5d31ac8a627f85046c9413d4e716e70c2'
EXPECTED_RUNTIME={'adapters/bilibili_video_guard.mjs': '1ecb678df412dd4aeb2680b4762e82b04bf7083484da81ee8df90815fc43069c', 'adapters/browser_replay_video.mjs': '6cf718ee0be6e74155edc06cfd3c4f60d35b6d3c00983029051fb99151e401de', 'adapters/media_tools.py': 'b857013db72f25ff994f5b72f7577438049a0d2d964bf1559bd870d90de9bd7b', 'adapters/recovered_submit_gate.mjs': '6933998117e6b1c3e8c5ff455ca7a616233831fae62565ee9512a8f3d4cc277a', 'adapters/replay_native_driver.mjs': '315c068d491786986ee0dcea557c4801ec05122057130139bd0e79f5c282e0dd', 'adapters/short_video_metadata_chain.mjs': '0a096995b314afe88e07f9afd082d1072704cfb519a1d33283067417d4ebae89', 'adapters/short_video_saved_draft_navigation_control.mjs': '4a97c6c9833a9c58705492ccdbd12db2ac00cbbd564f100e8324085e8d09ef0e', 'scheduler/service.py': '53412a81121d98790e4a7a1d425de7517135afea8cb4bf5d83144288a4e9cd97', 'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs': 'f4e74eb6ca72012a44d72d8d1b29900662e001010e51feb0cbf5b461b491fcc6', 'services/hub/config.py': 'b2e5659c523829a23b64934ce46a679ccc98f4e4fc02670e60f41288dacd399b', 'services/hub/cover_normalizer_v4.py': 'bbc133b7469578650069d6ecc32b593c6b141a55e56afcdb52938eb2558b00ae', 'services/hub/db.py': 'a5c8134ba961621793e7f4ad536d785d936e8f53ff439add448d7b0223c48e3d', 'services/hub/errors.py': 'f15856b1a33b29bc0d40e9297705cea759062dd33ff3cf96f99fd07f6a3e362f', 'services/hub/evidence.py': '1ede4fb0b89951394f50f3e31e2e80e579196dac7b9fac9284cd0fafdce567a2', 'services/hub/locks.py': 'ca7433c198f96cfdfa0c5aaea99dc789354a19d60e0f5ea59b21a6b4063dace9', 'services/hub/outcomes.py': '3a55475a66626538afa0e6215964bff7e6cafedf367a934173ced5a7893c16a1', 'services/hub/recovered_submit_ticket.py': 'cdb31fbd7542c6a61272751c446d5650406d75f329e9a48215f9a2259a096e12', 'services/hub/replay_machine_checks.py': '9a03069c063076f621f4d7609bd4932c89185f889cff67e240b8e8d3262eaa91', 'services/hub/replay_video_browser.py': 'fa973d38cdd092abf07b4e3a3b75ffa66fd05043b645fa71d986b3b1e82423bb', 'services/hub/replay_video_cover.py': '51f4367d9fc7a94cb88d95a169f9dce1e105f585f0d4c18a79964880494e3527', 'services/hub/replay_video_web.py': '4b9c36875d684557a4f118104622f17ca78f0537ae56f08b949c989b3980be16', 'services/hub/short_video_web.py': 'e2c6f3db26dd7ca7d076b6a401877c01fabba2a8b8987f8f0a5ad62c1ce25a9c', 'services/hub/worker.py': '5e179ae1fb43474487a9e82da3ef342d356ca4702d0e65aca13cf5000a4c6fa7', 'services/hub/youtube_download.py': 'd14f7d8c522481a6070f86afd46f402b09b6277212821986e89eba3bf0f0337a', 'services/hub/youtube_monitor.py': '22e1424835378dda85317452c7e23f3fcbbeb51a2efc9970fa998d9f95a60ac7'}

def sha(body):return hashlib.sha256(body).hexdigest()
def wire(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def safe(path):
    path=Path(path);assert path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents));return path
def read(path,maximum=500000):
    path=safe(path);before=path.stat();assert stat.S_ISREG(before.st_mode) and 0<before.st_size<=maximum
    body=path.read_bytes();after=path.stat();assert len(body)==before.st_size and all(getattr(before,k)==getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'));return body
def protected(path):
    safe(path)
    for p in (path,*path.parents):assert p.stat().st_uid==0 and p.stat().st_mode&0o022==0
def atomic(path,body,mode=0o600,original=None):
    path=safe(path);part=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    fd=os.open(part,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as out:out.write(body);out.flush();os.fchmod(out.fileno(),mode);os.fsync(out.fileno())
    if original:os.chown(part,original.st_uid,original.st_gid);os.chmod(part,stat.S_IMODE(original.st_mode))
    os.replace(part,path);fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
def run(args,timeout=90):
    p=subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout)
    if p.returncode:raise RuntimeError('REPLAY_REPLACEMENT_COMMAND_FAILED')
    assert len(p.stdout)<5_000_000 and len(p.stderr)<1_000_000;return p.stdout
def compose(*args,path=None):return run(['/usr/bin/docker','compose','--project-directory',str(ROOT),'-f',str(path or ROOT/'compose.yaml'),*args])
def parsed(path):return json.loads(compose('config','--format','json',path=path))

def legacy_compose(raw):
    lines=raw.decode('utf-8').splitlines(keepends=True);ending='\r\n' if b'\r\n' in raw else '\n'
    start=next(i for i,s in enumerate(lines) if s.rstrip('\r\n')=='  video-worker:');end=start+1
    while end<len(lines):
        s=lines[end]
        if s.strip() and len(s)-len(s.lstrip(' '))<=2:break
        end+=1
    block=lines[start+1:end];assert block[0].strip()=='<<: *runtime'
    assert not any(re.match(r'    (profiles|restart):',s) for s in block)
    block[1:1]=['    profiles: ["'+LEGACY+'"]'+ending,'    restart: "no"'+ending]
    lines[start+1:end]=block;return ''.join(lines).encode()

def assert_legacy_delta(before,after):
    expected=deepcopy(before);expected['services']['video-worker']['profiles']=[LEGACY]
    expected['services']['video-worker']['restart']='no';assert after==expected

def attest(path,digest,extension=False):
    path=safe(path);assert path.name=='test-attestation.json' and path.parent.parent==EV
    prefix='replay-runtime-extension-v1-' if extension else 'replay-worker-overlay-v1-'
    assert path.parent.name.startswith(prefix);ident=path.parent.name[len(prefix):];assert str(uuid.UUID(ident))==ident
    body=read(path,250000);assert re.fullmatch('[a-f0-9]{64}',digest or '') and sha(body)==digest
    value=json.loads(body);expected=22 if extension else 265
    assert value['version']==('replay-runtime-extension-ubuntu-tests-v1' if extension else 'replay-worker-overlay-ubuntu-tests-v1')
    assert value['image']==IMAGE and value['tests']==expected and value['full_suite_passed'] is True
    assert value['failures']==value['errors']==value['skipped']==value['exit_code']==0 and value['oom_killed'] is False
    assert value['network']=='none' and value['application_data_mounted'] is False and value['production_profile_mounted'] is False
    assert value['secrets_environment_injected'] is False and value['whole_tree_deployed'] is False
    assert all(value[k] is True for k in ('runtime_readonly_overlays_only','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','no_pid_or_oom_denial'))
    assert value['real_uploads']==value['publications']==0
    manifest=value['manifest'];assert manifest['expected_total_tests']==expected and manifest['expected_test_counts']==value['groups']
    assert sum(value['groups'].values())==expected
    if extension:
        assert value['groups']=={'tests/test_replay_worker_runtime.py':22}
        assert manifest['files'][ENTRY]['sha256']==ENTRY_SHA
    else:
        assert len(manifest['files'])==39
        assert {n:manifest['files'][n]['sha256'] for n in EXPECTED_RUNTIME}==EXPECTED_RUNTIME
    return value,{'file':str(path),'sha256':digest,'tests':expected}

def source_bundle(upstream,extension):
    data={}
    for name,digest in {**EXPECTED_RUNTIME,ENTRY:ENTRY_SHA}.items():
        parent=extension.parent if name==ENTRY else upstream.parent
        body=read(parent/'overlay'/name,200000);assert sha(body)==digest;data[name]=body
    return data

QUERY=r'''
from pathlib import Path
import hashlib,json,os,sqlite3,sys
assert os.getuid()==10001
db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True);db.row_factory=sqlite3.Row;db.execute('PRAGMA query_only=ON')
def decoded(row):
 value=dict(row)
 for k in ('payload','result'):value[k]=json.loads(value[k])
 return value
job=db.execute('SELECT * FROM jobs WHERE job_id=?',(sys.argv[2],)).fetchone()
active=[dict(r) for r in db.execute("SELECT job_id,job_type,state FROM jobs WHERE queue IN ('video','system') AND state IN ('processing','publish_unknown')")]
new=[dict(r) for r in db.execute("SELECT job_id,state FROM jobs WHERE job_type='replay_video_publish' AND state IN ('pending','retry_scheduled','processing','publish_unknown','awaiting_moderation','upload_pending')")]
short=[dict(r) for r in db.execute("SELECT * FROM jobs WHERE source_id='0H6_bTvWeDQ' ORDER BY job_id")]
heartbeat=db.execute("SELECT at FROM heartbeats WHERE name='worker:video'").fetchone()
print(json.dumps({'download':decoded(job) if job else None,'active':active,'new_replay_unresolved':new,'video_heartbeat':heartbeat[0] if heartbeat else None,
 'short_sha256':hashlib.sha256(json.dumps(short,sort_keys=True,separators=(',',':')).encode()).hexdigest()}));db.close()
'''
def dbquery():
    def app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
    p=subprocess.run(['/usr/bin/python3','-c',QUERY,str(ROOT/'data/hub.sqlite3'),DOWNLOAD],capture_output=True,text=True,timeout=20,preexec_fn=app,
        env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
    assert p.returncode==0;return json.loads(p.stdout)
def completed_download(value):
    job=value['download'];assert job and job['job_id']==DOWNLOAD and job['source_id']==PILOT
    assert job['source_platform']=='youtube' and job['job_type']=='youtube_download' and job['state']=='completed'
    state=job['result']['youtube_download_v1'];assert state['state']=='verified' and state['version']=='youtube-replay-download-v1'
    asset=state['source_asset'];file=safe(Path(asset['file']))
    assert file==ROOT/'media'/DOWNLOAD/'youtube-download-v1/original.mp4' and 0<asset['bytes']<=3_800_000_000
    assert file.stat().st_size==asset['bytes'];digest=hashlib.sha256()
    with file.open('rb') as source:
        for part in iter(lambda:source.read(1024*1024),b''):digest.update(part)
    assert digest.hexdigest()==asset['sha256']
    raw=read(file.parent/'verified.json',262144);receipt=json.loads(raw)
    assert receipt['source_asset']==asset and receipt['receipt']==state['receipt']
    assert receipt['binding_sha256']==sha(wire(state['binding'])) and receipt['receipt']['source_to_merged_packet_coverage_verified'] is True
    return {'job_id':DOWNLOAD,'state':'completed','asset_bytes':asset['bytes'],'asset_sha256':asset['sha256'],'verified_receipt_sha256':sha(raw)}

@contextmanager
def locks():
    import fcntl
    with ExitStack() as stack:
        for queue in ('system','video'):
            file=safe(ROOT/'data/locks'/queue/'observer.lock');fd=os.open(file,os.O_RDWR|os.O_NOFOLLOW);stack.callback(os.close,fd)
            s=os.fstat(fd);assert s.st_uid==10001 and stat.S_ISREG(s.st_mode) and s.st_mode&0o022==0
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield

def policy():
    value=tomllib.loads(read(ROOT/'config/replay-video-web-policy.toml').decode('utf-8-sig'))
    assert value['enabled'] is True and value['source_scope']=='pilot_single' and value['acceptance_video_ids']==[PILOT]
    assert value.get('automation_enabled',False) is False and value['target_site_uid']=='1000000000000000';return value
def key_file(path):
    path=safe(Path(path));s=path.stat();assert stat.S_ISREG(s.st_mode) and 1<=s.st_size<=512 and s.st_uid==0
    assert stat.S_IMODE(s.st_mode)==0o640 and s.st_gid==10001;protected(path);return path

def docker_create(key_path,names):
    command=['/usr/bin/docker','create','--pull=never','--name',NAME,'--init','--restart=no','--read-only','--user','10001:10001',
        '--cap-drop','ALL','--security-opt','no-new-privileges:true','--security-opt','seccomp='+str(ROOT/'config/browser-seccomp.json'),
        '--memory','1400m','--cpus','1.5','--pids-limit','256','--shm-size','256m',
        '--log-driver','local','--log-opt','max-size=10m','--log-opt','max-file=3',
        '--label','lighthouse.replay-runtime=v1','--tmpfs','/tmp:rw,size=192m,mode=1777',
        '--tmpfs',str(ROOT/'data/browser-profiles')+':rw,size=1m,mode=0700,uid=10001,gid=10001']
    for name,value in {'HUB_ROOT':str(ROOT),'HUB_CONFIG':str(ROOT/'config/hub.toml'),'DRY_RUN':'true',
        'HOME':'/tmp/replay/home','XDG_CONFIG_HOME':'/tmp/replay/config','XDG_CACHE_HOME':'/tmp/replay/cache',
        'YOUTUBE_API_KEY_FILE':'/run/secrets/youtube-api-key'}.items():command+=['-e',name+'='+value]
    for source,target,ro in ((ROOT/'data',ROOT/'data',False),(ROOT/'media',ROOT/'media',False),(ROOT/'config',ROOT/'config',True),
        (ROOT/'data/browser-profiles/bilibili-video',ROOT/'data/browser-profiles/bilibili-video',False),
        (ROOT/'data/browser-profiles/youtube',ROOT/'data/browser-profiles/youtube',True),(key_path,Path('/run/secrets/youtube-api-key'),True)):
        command+=['--mount','type=bind,source='+str(source)+',target='+str(target)+(',readonly' if ro else '')]
    for name in names:command+=['--mount','type=bind,source='+str(INSTALL/'code'/name)+',target='+str(ROOT/name)+',readonly']
    return command+[IMAGE,'python','-m','services.hub.replay_worker_runtime']

def unit_text():
    return ('[Unit]\nDescription=Pilot-scoped replacement video Worker\nAfter=docker.service network-online.target\nRequires=docker.service\nWants=network-online.target\n\n'
        '[Service]\nType=exec\nUser=root\nGroup=root\nUMask=0077\n'
        'ExecStartPre=/usr/bin/python3 '+str(INSTALL/'replacement.py')+' --verify-installed\n'
        'ExecStart=/usr/bin/docker start -a '+NAME+'\nExecStop=/usr/bin/docker stop --time 45 '+NAME+'\n'
        'Restart=always\nRestartSec=10\nTimeoutStartSec=180\nTimeoutStopSec=60\nNoNewPrivileges=true\n'
        'ProtectSystem=strict\nProtectHome=true\nPrivateTmp=true\nReadWritePaths=/run/docker.sock\n'
        'StandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=multi-user.target\n').encode()

def verify_installed():
    file=INSTALL/'manifest.json';protected(file);value=json.loads(read(file,250000))
    assert value['state']=='replacement_installed' and value['image']==IMAGE and value['entry_sha256']==ENTRY_SHA
    protected(INSTALL/'replacement.py');assert sha(read(INSTALL/'replacement.py'))==value['helper_sha256']
    for name,digest in {**EXPECTED_RUNTIME,ENTRY:ENTRY_SHA}.items():
        file=INSTALL/'code'/name;protected(file);assert sha(read(file))==digest
    assert sha(read(ROOT/'compose.yaml'))==value['compose_after_sha256']
    assert run(['/usr/bin/docker','inspect','--format','{{.State.Running}}',value['legacy_container_id']]).strip()=='false'
    assert json.loads(run(['/usr/bin/docker','inspect','--format','{{json .HostConfig.RestartPolicy}}',value['legacy_container_id']]))['Name']=='no'
    assert run(['/usr/bin/docker','inspect','--format','{{.Image}}',value['replacement_container_id']]).strip()==IMAGE
    policy();key_file(value['youtube_key_file'])
    print(json.dumps({'state':'installed_gate_verified','legacy_running':False,'pilot_source':PILOT,'new_sources_admitted':0}))

def activate(args):
    upstream=safe(Path(args.upstream_attestation));extension=safe(Path(args.extension_attestation))
    _,upref=attest(upstream,args.upstream_sha256);_,exref=attest(extension,args.extension_sha256,True)
    code=source_bundle(upstream,extension);key=key_file(args.youtube_key_file);policy()
    path=ROOT/'compose.yaml';original=read(path);original_stat=path.stat();changed=legacy_compose(original);before=parsed(path)
    ident=str(uuid.uuid4());candidate=ROOT/('.compose-replay-replacement-'+ident+'.yaml');atomic(candidate,changed)
    inode=(candidate.stat().st_dev,candidate.stat().st_ino)
    try:assert_legacy_delta(before,parsed(candidate))
    finally:
        if candidate.is_file() and not candidate.is_symlink() and (candidate.stat().st_dev,candidate.stat().st_ino)==inode:candidate.unlink()
    legacy=compose('ps','-a','-q','video-worker').strip();assert re.fullmatch('[a-f0-9]{12,64}',legacy)
    assert run(['/usr/bin/docker','inspect','--format','{{.Image}}',legacy]).strip()==CBC
    restart=json.loads(run(['/usr/bin/docker','inspect','--format','{{json .HostConfig.RestartPolicy}}',legacy]))
    assert restart['Name']=='unless-stopped'
    state=dbquery();completed=completed_download(state)
    fixed={p:read(p) for p in (ROOT/'config/hub.toml',ROOT/'config/youtube-download-policy.toml',ROOT/'config/replay-video-web-policy.toml')}
    summary={'version':'replay-worker-replacement-installation-v1','image':IMAGE,'entry_sha256':ENTRY_SHA,
        'upstream_test_attestation':upref,'extension_test_attestation':exref,'completed_download':completed,'legacy_container_id':legacy,
        'legacy_original_restart':restart,'compose_before_sha256':sha(original),'compose_after_sha256':sha(changed),
        'new_sources_admitted':0,'all_automatic_scope_enabled':False,'youtube_key_value_copied_or_logged':False,
        'operating_config_sha256':{str(p):sha(body) for p,body in fixed.items()}}
    if not args.activate:print(json.dumps({**summary,'state':'verified_plan_only','services_changed':False}));return 0
    assert not (INSTALL/'manifest.json').exists()
    safe(INSTALL);INSTALL.mkdir(parents=True,mode=0o755,exist_ok=True);INSTALL.chmod(0o755);protected(INSTALL)
    for name,body in code.items():
        target=INSTALL/'code'/name;safe(target);target.parent.mkdir(parents=True,mode=0o755,exist_ok=True)
        if target.exists():assert read(target)==body;protected(target)
        else:atomic(target,body,mode=0o644)
    self_body=read(Path(__file__).resolve());atomic(INSTALL/'replacement.py',self_body,mode=0o644)
    backup=ROOT/'backups/replay-worker-replacement'/ident;safe(backup);backup.mkdir(parents=True,mode=0o700);backup.chmod(0o700)
    atomic(backup/'compose.yaml.before',original)
    report=backup/'installation-report.json';phase='queue_locks';mutated=False;legacy_touched=False;replacement=None
    def record(state,**extra):atomic(report,wire({**summary,'state':state,'phase':phase,'at':datetime.now(timezone.utc).isoformat(),**extra}))
    def fixed_config():assert all(read(p)==body for p,body in fixed.items())
    try:
        with locks():
            current=dbquery();completed_download(current);assert not current['active'] and not current['new_replay_unresolved']
            assert current['short_sha256']==state['short_sha256'] and read(path)==original;fixed_config()
            phase='legacy_disabled';legacy_touched=True
            run(['/usr/bin/docker','update','--restart=no',legacy]);run(['/usr/bin/docker','stop','--time','45',legacy])
            phase='canonical_legacy_profile';mutated=True;atomic(path,changed,original=original_stat);assert_legacy_delta(before,parsed(path))
            phase='create_replacement';replacement=run(docker_create(key,code)).strip();assert re.fullmatch('[a-f0-9]{64}',replacement)
            manifest={**summary,'state':'replacement_installed','youtube_key_file':str(key),'replacement_container_id':replacement,
                'helper_sha256':sha(self_body),'source_sha256':{**EXPECTED_RUNTIME,ENTRY:ENTRY_SHA},'rollback_directory':str(backup)}
            atomic(INSTALL/'manifest.json',wire(manifest))
            atomic(Path('/etc/systemd/system')/UNIT,unit_text(),mode=0o644)
            run(['/usr/bin/systemctl','daemon-reload']);run(['/usr/bin/systemctl','enable',UNIT]);started_at=time.time();run(['/usr/bin/systemctl','start',UNIT])
            deadline=time.monotonic()+15
            while run(['/usr/bin/docker','inspect','--format','{{.State.Running}}',replacement]).strip()!='true':
                assert time.monotonic()<deadline;time.sleep(.2)
            assert dbquery()['short_sha256']==state['short_sha256']
            record('replacement_started',replacement_container_id=replacement)
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            import urllib.request
            try:
                with urllib.request.urlopen('http://127.0.0.1:8080/ready',timeout=3) as response:
                    observed=json.loads(response.read(8193));assert observed['status']=='ready' and observed['stale_services']==[]
                    heartbeat=dbquery()['video_heartbeat'];assert heartbeat is not None and heartbeat>=started_at
                    assert run(['/usr/bin/docker','inspect','--format','{{.State.Running}}',replacement]).strip()=='true'
                    fixed_config();break
            except Exception:time.sleep(1)
        else:raise RuntimeError('REPLAY_REPLACEMENT_READY_TIMEOUT')
        record('replacement_ready',replacement_container_id=replacement,ready=observed)
        print(json.dumps({'state':'replacement_ready','manifest_file':str(INSTALL/'manifest.json'),'report_file':str(report),'new_sources_admitted':0}));return 0
    except BaseException as error:
        failed_phase=phase;phase='rollback';ok=False
        try:
            with locks():
                current=dbquery();assert not current['active'] and not current['new_replay_unresolved'];fixed_config()
                if replacement:run(['/usr/bin/systemctl','stop',UNIT]);run(['/usr/bin/systemctl','disable',UNIT])
                if mutated:atomic(path,original,original=original_stat);assert parsed(path)==before
                if legacy_touched:run(['/usr/bin/docker','update','--restart=unless-stopped',legacy]);run(['/usr/bin/docker','start',legacy])
                assert dbquery()['short_sha256']==state['short_sha256'];ok=True
        except BaseException:pass
        if ok and (INSTALL/'manifest.json').is_file():
            value=json.loads(read(INSTALL/'manifest.json',250000));value['state']='rolled_back'
            atomic(INSTALL/'manifest.json',wire(value))
        record('installation_failed_rolled_back' if ok else 'installation_failed_rollback_not_verified',failed_phase=failed_phase,
            rollback_verified=ok,error_code=type(error).__name__,original_data_or_media_modified=False)
        print(json.dumps({'state':'installation_failed','rollback_verified':ok,'report_file':str(report),'error_code':type(error).__name__}));return 2

def rollback():
    path=INSTALL/'manifest.json';protected(path);value=json.loads(read(path,250000))
    assert value['state']=='replacement_installed' and value['image']==IMAGE
    backup=safe(Path(value['rollback_directory']));assert backup.parent==ROOT/'backups/replay-worker-replacement'
    ident=backup.name;assert str(uuid.UUID(ident))==ident
    original=read(backup/'compose.yaml.before');assert sha(original)==value['compose_before_sha256']
    with locks():
        # Acquire before stopping: never interrupt an active upload merely to
        # make a rollback possible. A pending/new-kind lookup also blocks it.
        current=dbquery();assert not current['active'] and not current['new_replay_unresolved']
        run(['/usr/bin/systemctl','stop',UNIT])
        after=dbquery();assert not after['active'] and not after['new_replay_unresolved']
        run(['/usr/bin/systemctl','disable',UNIT])
        canonical=ROOT/'compose.yaml';assert sha(read(canonical))==value['compose_after_sha256']
        atomic(canonical,original,original=canonical.stat())
        run(['/usr/bin/docker','update','--restart=unless-stopped',value['legacy_container_id']])
        run(['/usr/bin/docker','start',value['legacy_container_id']])
        assert dbquery()['short_sha256']==current['short_sha256']
        value['state']='rolled_back';value['rolled_back_at']=datetime.now(timezone.utc).isoformat();atomic(path,wire(value))
    print(json.dumps({'state':'legacy_restored','replacement_stopped':True,'data_or_media_deleted':False,'new_sources_admitted':0}))

def main():
    if sys.flags.optimize:raise RuntimeError('OPTIMIZED_PYTHON_UNSUPPORTED')
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group();mode.add_argument('--verify-installed',action='store_true');mode.add_argument('--activate',action='store_true');mode.add_argument('--rollback',action='store_true')
    for name in ('upstream-attestation','upstream-sha256','extension-attestation','extension-sha256','youtube-key-file'):parser.add_argument('--'+name)
    args=parser.parse_args()
    if args.verify_installed:return verify_installed()
    assert args.rollback or all(getattr(args,name) for name in ('upstream_attestation','upstream_sha256','extension_attestation','extension_sha256','youtube_key_file'))
    import fcntl
    file=safe(Path('/run/lighthouse-replay-worker-replacement.lock'));fd=os.open(file,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        assert os.fstat(fd).st_uid==0 and os.fstat(fd).st_mode&0o077==0
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return rollback() if args.rollback else activate(args)
    finally:os.close(fd)

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as error:print(json.dumps({'state':'operation_stopped','error_code':type(error).__name__,'consult_installation_report':True,'new_sources_admitted':0}));raise SystemExit(2) from None
