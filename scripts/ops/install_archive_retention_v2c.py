"""Plan/apply the v2c backup-only overlay; never start backup or change timer.

Requires real 139+9 proof files and a SHA-pinned prepared kit. No credentials,
network operations, image rebuild, core restart or deletion are performed.
"""
from pathlib import Path
import argparse,hashlib,json,os,re,stat,subprocess,time,uuid
import xml.etree.ElementTree as ET

ROOT=Path('/opt/lighthouse-hub');OPS=Path('/usr/local/lib/lighthouse-backup-ops')
BASE=OPS/'retention-v2c';STATE=Path('/var/lib/lighthouse-backup-ops/retention-v2c')
DAILY=Path('/var/lib/lighthouse-backup-ops/retention-v2b/daily-state')
SERVICE='lighthouse-cloudbase-backup.service';UNIT=Path('/etc/systemd/system')/SERVICE
TIMER=Path('/etc/systemd/system/lighthouse-cloudbase-backup.timer')
RELEASE=ROOT/'config/archive-source-retention-v2-release.json'
OLD_UNIT_SHA='07cb3794b26bb118cca7c1c9f648f72fa483d0f53a69e9323a88ca328755edea'
TIMER_SHA='58ac9795fd895077a3f3461e685528b18ac6d44fece78acdc8744fdea643964a'
OLD_RELEASE_SHA='c0e69b264d049f573091fbae8c055f36cab55f191acec263b25d04fdc9f206b3'
AUTH='f3000857-51d3-47bb-871d-465e88cbc817'
IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
RECOVERY=Path('/var/lib/lighthouse-backup-ops/preupload-recovery-v1/6cfa6b1d-46db-4ed9-888e-f6564f07d956')
CORE={'api','scheduler','tweet-worker','video-worker','system-worker'}
RUNTIME={'services/hub/cloudbase_backup_runtime.py','services/hub/archive_source_retention_v2.py',
 'scripts/ops/prune_cloudbase_verified_clones_v1.py','scripts/archive-source-retention-v2.py','services/hub/archive_source_autorun_v2.py'}
HOST={'scripts/ops/cloudbase_backup_daily_v1.py','scripts/ops/cloudbase_archive_registration_hook_v2.py'}
HELPERS={'run_cloudbase_backup_retention_v2c.py','run_archive_source_registry_v2c.py','cloudbase_backup_daily_retention_v2c.py'}


def need(value,code):
    if not value:raise ValueError(code)
def sha(raw):return hashlib.sha256(raw).hexdigest()
def wire(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()


def safe(path):
    path=Path(path);need(path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents)),'UNSAFE_INSTALL_PATH');return path


def protected(path):
    path=safe(path)
    for item in (path,*path.parents):
        info=item.stat();need(info.st_uid==0 and not info.st_mode&0o022,'ROOT_CONTROLLED_PATH_REQUIRED')


def read(path,limit=2_097_152):
    path=safe(path);protected(path);info=path.stat()
    need(stat.S_ISREG(info.st_mode) and 0<info.st_size<=limit,'BOUNDED_REGULAR_FILE_REQUIRED')
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
        before=os.fstat(stream.fileno());need(stat.S_ISREG(before.st_mode),'BOUNDED_REGULAR_FILE_REQUIRED')
        raw=stream.read(limit+1);after=os.fstat(stream.fileno())
    need(len(raw)==info.st_size and all(getattr(info,k)==getattr(before,k)==getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')),'INSTALL_INPUT_CHANGED')
    return raw


def run(args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=60)
    need(result.returncode==0,'BACKUP_INSTALL_COMMAND_FAILED');return result.stdout


def core():
    ids=run(['docker','ps','-aq','--filter','label=com.docker.compose.project=lighthouse-hub']).split();need(ids,'CORE_REQUIRED')
    result={}
    for item in json.loads(run(['docker','inspect',*ids])):
        service=item['Config']['Labels'].get('com.docker.compose.service')
        if service not in CORE:continue
        need(service not in result and item['State']['Running'],'EXACT_RUNNING_CORE_REQUIRED')
        result[service]={k:item[k] for k in ('Id','Image')};result[service]['started_at']=item['State']['StartedAt']
    need(set(result)==CORE,'EXACT_FIVE_CORE_REQUIRED');return result


def quiescent():
    value=run(['systemctl','show',SERVICE,'--property=ActiveState','--property=DropInPaths','--property=FragmentPath'])
    fields=dict(line.split('=',1) for line in value.splitlines() if '=' in line)
    need(fields.get('ActiveState') in ('inactive','failed') and fields.get('FragmentPath')==str(UNIT) and not fields.get('DropInPaths'),'INACTIVE_EXACT_BACKUP_UNIT_REQUIRED')
    for condition in ('label=org.lighthouse.backup.sidecar','name=^/lighthouse-archive-registry-'):
        need(not run(['docker','ps','-q','--filter',condition]).strip(),'BACKUP_OR_REGISTRY_RUNNING')


def lock_paths(app=ROOT,daily=DAILY,ops=Path('/var/lib/lighthouse-backup-ops')):
    return ((ops/'daily.lock',0),(daily/'daily.lock',0),(app/'data/cloudbase-backup-state/runner.lock/observer.lock',10001))


def acquire_locks(paths=None):
    import fcntl
    descriptors=[]
    try:
        for file,owner in lock_paths() if paths is None else paths:
            safe(file);fd=os.open(file,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK);descriptors.append(fd)
            info=os.fstat(fd);need(stat.S_ISREG(info.st_mode) and info.st_uid==owner and not info.st_mode&0o022,'EXACT_EXISTING_LOCK_REQUIRED')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return descriptors
    except BaseException:
        for fd in descriptors:os.close(fd)
        raise


def verify_proof(raw,junit,manifest,*,autorun=False):
    value=json.loads(raw);counts={'tests.test_archive_source_autorun_v2':9} if autorun else {
        'tests.test_archive_source_retention_v2':31,'tests.ops.test_cloudbase_archive_registration_hook_v2':5,
        'tests.test_cloudbase_backup_runtime':36,'tests.test_cloudbase_backup':44,'tests.test_cloudbase_cos_backup':23}
    version='archive-source-autorun-v2-ubuntu-tests-v1' if autorun else 'archive-source-retention-v2-ubuntu-tests-v1'
    need(value.get('version')==version and value.get('image')==IMAGE and value.get('tests')==sum(counts.values()),'EXACT_PROOF_REQUIRED')
    need(all(value.get(k)==0 for k in ('failures','errors','skipped','exit_code')),'PASSED_PROOF_REQUIRED')
    need(value.get('network')=='none' and all(value.get(k) is False for k in ('oom_killed','application_data_mounted','production_profile_mounted','secrets_environment_injected')),'ISOLATED_PROOF_REQUIRED')
    need(all(value.get(k) is True for k in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate','mounts_match_isolated_allowlist')),'ISOLATED_PROOF_REQUIRED')
    names={'services/hub/archive_source_retention_v2.py','services/hub/cloudbase_backup_runtime.py','scripts/ops/prune_cloudbase_verified_clones_v1.py'}
    if autorun:names.add('services/hub/archive_source_autorun_v2.py')
    need(value.get('source_files')=={name:manifest['runtime_files'][name] for name in names} and value.get('junit_sha256')==sha(junit),'PROOF_SOURCE_OR_JUNIT_CHANGED')
    groups={}
    for case in ET.fromstring(junit).findall('.//testcase'):
        groups[case.get('classname')]=groups.get(case.get('classname'),0)+1
        need(all(case.find(tag) is None for tag in ('failure','error','skipped')),'FAILED_PROOF_CASE')
    need(groups==counts,'EXACT_PROOF_GROUPS_REQUIRED')


def load_kit(folder,digest):
    raw=read(folder/'manifest.json');need(sha(raw)==digest,'KIT_MANIFEST_CHANGED');manifest=json.loads(raw)
    need(manifest['version']=='archive-retention-host-kit-v2c' and manifest['image']==IMAGE,'KIT_VERSION_CHANGED')
    need(set(manifest['runtime_files'])==RUNTIME and set(manifest['host_files'])==HOST and set(manifest['helper_files'])==HELPERS,'KIT_SCOPE_CHANGED')
    need(manifest['old_unit_sha256']==OLD_UNIT_SHA and manifest['timer_sha256']==TIMER_SHA and manifest['old_release_sha256']==OLD_RELEASE_SHA and
         manifest['authorization_id']==AUTH and manifest['daily_state']==str(DAILY),'KIT_DEPLOYMENT_BINDING_CHANGED')
    expected={'runtime/'+name for name in RUNTIME}|{'host/'+name for name in HOST}|{'helpers/'+name for name in HELPERS}|{
        'readiness/test-attestation.json','readiness/accepted-results.xml','autorun-readiness/test-attestation.json','autorun-readiness/accepted-results.xml',
        'candidate-release.json','previous.service','candidate.service'}
    need(set(manifest['files'])==expected,'KIT_FILE_ALLOWLIST_CHANGED');files={}
    for name,entry in manifest['files'].items():
        raw=read(folder/name);need(sha(raw)==entry['sha256'] and len(raw)==entry['bytes'],'KIT_FILE_CHANGED');files[name]=raw
    need(sha(files['previous.service'])==OLD_UNIT_SHA and files['candidate.service']==files['previous.service'].replace(
         b'cloudbase_backup_daily_retention_v2b.py',b'cloudbase_backup_daily_retention_v2c.py'),'SERVICE_ONLY_EXECSTART_CHANGE_REQUIRED')
    release=json.loads(files['candidate-release.json'])
    need(release['authorization_id']==AUTH and release['version']=='archive-source-retention-release-v2' and
         release['mode']=='verified_archive_members_only' and release['formal_video_launch_claimed'] is False,'EXISTING_AUTHORIZATION_REQUIRED')
    for prefix,key in (('readiness','core_proof'),('autorun-readiness','autorun_proof')):
        proof=manifest[key];actual=Path(proof['root'])
        need(read(actual/'test-attestation.json')==files[prefix+'/test-attestation.json'] and
             read(actual/'accepted-results.xml')==files[prefix+'/accepted-results.xml'],'ACTUAL_SERVER_PROOF_CHANGED')
        need(sha(files[prefix+'/test-attestation.json'])==proof['attestation_sha256'] and sha(files[prefix+'/accepted-results.xml'])==proof['junit_sha256'],'KIT_PROOF_HASH_CHANGED')
        verify_proof(files[prefix+'/test-attestation.json'],files[prefix+'/accepted-results.xml'],manifest,autorun=key=='autorun_proof')
    return manifest,files


def latest_state(manifest):
    latest=DAILY/'latest.json'
    if not latest.exists():return {'exists':False}
    raw=read(latest);value=json.loads(raw)
    need(value['version']=='cloudbase-daily-accept-prune-v1' and value['image']==IMAGE,'DAILY_RECORD_CHANGED')
    pins=value['helper_pins'];need(pins in (manifest['legacy_helper_pins'],manifest['new_helper_pins']),'DAILY_PINS_UNKNOWN')
    need(value['state']=='completed','UNFINISHED_DAILY_RECONCILIATION_REQUIRED')
    need(re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',value['daily_id']),'DAILY_ID_INVALID')
    history=read(DAILY/'history'/(value['daily_id']+'.json'));need(history==raw,'COMPLETED_DAILY_HISTORY_CHANGED')
    return {'exists':True,'sha256':sha(raw),'daily_id':value['daily_id'],'state':'completed','pins':'v2c' if pins==manifest['new_helper_pins'] else 'v2b'}


def gap_guard(files):
    # Execute only the fixed prepared validator, never the daily main or imports.
    import ast
    text=files['helpers/cloudbase_backup_daily_retention_v2c.py'].decode();tree=ast.parse(text)
    names={'RECOVERY_DAILY','RECOVERY_OPERATION','LEGACY_PINS'};nodes=[]
    for node in tree.body:
        if isinstance(node,ast.FunctionDef) and node.name=='verify_recovered_gap':nodes.append(node)
        elif isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in names for t in node.targets):nodes.append(node)
    scope={'Path':Path,'json':json,'hashlib':hashlib,'protected':protected}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),'<pinned-recovery-validator>','exec'),scope)
    class Daily:STATE=DAILY
    scope['verify_recovered_gap'](Daily)


def preflight(folder,digest):
    manifest,files=load_kit(folder,digest);quiescent()
    need(sha(read(TIMER))==TIMER_SHA,'TIMER_CHANGED')
    unit=read(UNIT);release=read(RELEASE)
    need(unit in (files['previous.service'],files['candidate.service']),'EXACT_OLD_UNIT_REQUIRED')
    need(sha(release)==OLD_RELEASE_SHA or release==files['candidate-release.json'],'EXACT_RELEASE_REQUIRED')
    need(json.loads(release)['authorization_id']==AUTH,'EXISTING_AUTHORIZATION_REQUIRED')
    latest=latest_state(manifest)
    if not latest['exists']:gap_guard(files)
    need(json.loads(run(['docker','image','inspect',IMAGE]))[0]['Id']==IMAGE,'PINNED_IMAGE_REQUIRED')
    timer_state=run(['systemctl','show',TIMER.name,'--property=ActiveState','--property=UnitFileState'])
    plan={'version':'archive-retention-install-v2c','kit_sha256':digest,'unit_sha256':sha(unit),'release_sha256':sha(release),
          'latest':latest,'core_before':core(),'timer_sha256':TIMER_SHA,'timer_state':timer_state,
          'runtime_files':manifest['runtime_files'],'authorization_id':AUTH,'backup_started':False,'timer_changed':False,'external_requests':0}
    return plan,manifest,files


def directory(path,mode=0o755,gid=0):
    safe(path)
    if not path.exists():protected(path.parent);path.mkdir();os.chown(path,0,gid);os.chmod(path,mode)
    protected(path);info=path.stat();need(stat.S_ISDIR(info.st_mode) and stat.S_IMODE(info.st_mode)==mode and info.st_gid==gid,'EXACT_INSTALL_DIRECTORY_REQUIRED')


def sync_directories(*folders):
    for folder in folders:
        fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)


def write(path,raw,mode=0o644,gid=0,*,replace=False):
    safe(path);protected(path.parent)
    if path.exists():
        if read(path)==raw:return
        need(replace,'EXISTING_V2C_FILE_CHANGED')
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode),'wb') as stream:
        os.fchmod(stream.fileno(),mode);os.fchown(stream.fileno(),0,gid);stream.write(raw);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path);sync_directories(path.parent)


def materialize(files):
    directory(BASE);directory(STATE,0o700);directory(STATE/'install-history',0o700)
    for name,raw in files.items():
        if name.startswith(('runtime/','host/')):target=BASE/name;mode=0o444;gid=0
        elif name.startswith('helpers/'):target=OPS/Path(name).name;mode=0o444;gid=0
        elif name.startswith(('readiness/','autorun-readiness/')):target=STATE/name;mode=0o640;gid=10001
        else:continue
        missing=[];parent=target.parent
        while not parent.exists():missing.append(parent);parent=parent.parent
        for item in reversed(missing):directory(item,0o750 if gid else 0o755,gid)
        write(target,raw,mode,gid)


def apply(plan,manifest,files):
    materialize(files);history=STATE/'install-history'/plan['kit_sha256'];directory(history,0o700)
    unit_before=read(UNIT);release_before=read(RELEASE)
    write(history/'previous.service',files['previous.service'],0o600)
    if sha(release_before)==OLD_RELEASE_SHA:write(history/'previous.release.json',release_before,0o600)
    else:need(sha(read(history/'previous.release.json'))==OLD_RELEASE_SHA,'PREVIOUS_RELEASE_REQUIRED')
    try:
        write(RELEASE,files['candidate-release.json'],0o640,10001,replace=True)
        checked=json.loads(run(['/usr/bin/python3',str(OPS/'run_archive_source_registry_v2c.py'),'--verify-release','--authorization-id',AUTH]))
        need(checked['exit_code']==0 and checked['network']=='none' and checked['result']['test_attestation_sha256']==manifest['core_proof']['attestation_sha256'],'NEW_RELEASE_NOT_VERIFIED')
        write(UNIT,files['candidate.service'],replace=True);run(['systemd-analyze','verify',str(UNIT)]);run(['systemctl','daemon-reload'])
        need(sha(read(TIMER))==TIMER_SHA and run(['systemctl','show',TIMER.name,'--property=ActiveState','--property=UnitFileState'])==plan['timer_state'],'TIMER_CHANGED_DURING_INSTALL')
        need(core()==plan['core_before'],'CORE_CHANGED_DURING_INSTALL')
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        write(UNIT,unit_before,replace=True);write(RELEASE,release_before,0o640,10001,replace=True);run(['systemctl','daemon-reload']);raise
    return {'state':'installed_next_timer_not_run','core_unchanged':True,'timer_unchanged':True,'backup_started':False,'old_paths_preserved':True}


def rollback(plan,manifest,files):
    history=STATE/'install-history'/plan['kit_sha256'];old_release=read(history/'previous.release.json')
    need(sha(old_release)==OLD_RELEASE_SHA,'PREVIOUS_RELEASE_CHANGED')
    latest=latest_state(manifest);retained=None;unit_before=read(UNIT);release_before=read(RELEASE)
    try:
        if latest.get('pins')=='v2c':
            target=history/('completed-latest-'+latest['daily_id']+'.json');need(not target.exists(),'ROLLBACK_RECORD_ALREADY_EXISTS')
            need(sha(read(DAILY/'latest.json'))==latest['sha256'],'LATEST_CHANGED')
            write(history/('rollback-intent-'+latest['daily_id']+'.json'),wire({'state':'preserve_completed_latest_before_rollback','latest':latest,
                'unit_sha256':sha(unit_before),'release_sha256':sha(release_before)})+b'\n',0o600)
            os.replace(DAILY/'latest.json',target);retained=str(target);sync_directories(DAILY,history)
        write(UNIT,files['previous.service'],replace=True);write(RELEASE,old_release,0o640,10001,replace=True);run(['systemctl','daemon-reload'])
        need(sha(read(TIMER))==TIMER_SHA and run(['systemctl','show',TIMER.name,'--property=ActiveState','--property=UnitFileState'])==plan['timer_state'] and
             core()==plan['core_before'],'STATE_CHANGED_DURING_ROLLBACK')
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        write(UNIT,unit_before,replace=True);write(RELEASE,release_before,0o640,10001,replace=True)
        if retained and not (DAILY/'latest.json').exists():
            need(sha(read(Path(retained)))==latest['sha256'],'RETAINED_LATEST_CHANGED')
            os.replace(Path(retained),DAILY/'latest.json');sync_directories(DAILY,history)
        run(['systemctl','daemon-reload']);raise
    return {'state':'rolled_back_old_runtime_health_not_asserted','core_unchanged':True,'timer_unchanged':True,
            'completed_latest_preserved_at':retained,'files_deleted':0,'historical_ledgers_preserved':True}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--kit',type=Path,required=True)
    p.add_argument('--manifest-sha256',required=True);p.add_argument('--expected-operator-sha256',required=True);p.add_argument('--plan-sha256')
    mode=p.add_mutually_exclusive_group();mode.add_argument('--apply',action='store_true');mode.add_argument('--rollback',action='store_true');a=p.parse_args()
    locks=[]
    try:
        need(os.name=='posix' and os.geteuid()==0 and __debug__,'ROOT_LINUX_OPERATOR_REQUIRED')
        need(all(re.fullmatch('[a-f0-9]{64}',v) for v in (a.manifest_sha256,a.expected_operator_sha256)),'EXACT_SHA256_REQUIRED')
        need(sha(read(Path(__file__).absolute()))==a.expected_operator_sha256,'OPERATOR_HASH_CHANGED')
        need(bool(a.apply or a.rollback)==bool(a.plan_sha256),'REVIEWED_PLAN_REQUIRED')
        if a.apply or a.rollback:locks=acquire_locks()
        plan,manifest,files=preflight(a.kit,a.manifest_sha256);checksum=sha(wire(plan))
        if not a.apply and not a.rollback:print(json.dumps({'state':'planned_not_installed','plan_sha256':checksum,'plan':plan}));return
        need(checksum==a.plan_sha256,'INSTALL_PLAN_CHANGED')
        result=rollback(plan,manifest,files) if a.rollback else apply(plan,manifest,files)
        result.update(version='archive-retention-install-v2c',at=time.time(),plan_sha256=checksum)
        receipt=STATE/'install-history'/a.manifest_sha256/('result-'+uuid.uuid4().hex+'.json');write(receipt,wire(result)+b'\n',0o600)
        print(json.dumps({**result,'receipt_file':str(receipt)}))
    except (OSError,ValueError,KeyError,TypeError,AssertionError,ET.ParseError,subprocess.SubprocessError) as error:
        code=str(error) if isinstance(error,ValueError) and re.fullmatch('[A-Z_]+',str(error)) else 'V2C_INSTALL_FAILED'
        print(json.dumps({'state':'stopped','error_code':code,'backup_started':False}));raise SystemExit(1) from None
    finally:
        for fd in locks:os.close(fd)


if __name__=='__main__':main()
