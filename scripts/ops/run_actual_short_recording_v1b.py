"""Root orchestration only. Default check; explicit result/deletion record modes.

This wrapper has no SSH, browser, remote deletion or publication implementation.
Only the application-UID child opens SQLite. Existing evidence is never replaced.
"""
from pathlib import Path
import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import uuid

HOME=Path('/home/lighthouse-ops');ROOT=Path('/opt/lighthouse-hub')
IMAGE='sha256:9f19b756ca081bfe033ffa01b0face5c9cb70b3d45de08394567150ac747de6e'
TEST_ID='7ddedd1c-4f10-448a-8186-52048b8dcad5'
TEST_DIR=HOME/'lighthouse-evidence'/('owner-assisted-short-overlay-v1-'+TEST_ID)
OWNED=HOME/('lighthouse-owner-assisted-short-owned-'+TEST_ID)
EVIDENCE_ID='c56c03b4-76a2-4c9c-8bbd-00c41c3be6c0'
JOB='2b3cc3ac-2922-478d-a84a-df8fca441235';BV='BV1cVxb67EMm'
DEST=ROOT/'media'/JOB/'owner-assisted-v1'/EVIDENCE_ID
TEST_SHA='dbcc946e2399636c5d9a635ecc0b22d23ba4e47adbee8acf9cd57f6a6c466f61'
EVIDENCE_SHA='b9c216efcab63272970c5cca56f30ab0a3441cde02f3bba405391026b5130458'
HELPER_SHA='2751b00061738623bcb65e50e18b9f4437244ea10575934d8c6e46daded97d9b'
SHORT='services/hub/short_video_web.py';MODULE='services/hub/owner_assisted_short_result_v1.py'
SHORT_SHA='4fda57470741f09d76c6444118803f651bbd6f6c9b50008fe099ccc7f34b51f1'
MODULE_SHA='9c8a3cceffd96335aafc97ff90685b68fe35bfdd992a932a7b636ef7b62cb7f0'
TEST_FILES={SHORT,MODULE,'tests/test_owner_assisted_short_result_v1.py','tests/test_short_video_web.py',
            'tests/test_short_video_permission_line_removal.py','tests/conftest.py'}
EVIDENCE_FILES={'deletion-native.png','deletion-public.png','deletion.json','native.json','native.png',
 'operator-requests.json','public.json','public.png','recording-plan.json','source-deletion-native.json',
 'source-deletion-public.json','source-manager.json','source-native-form.json','source-native-form.png',
 'source-public.json','source-published-cover.jpg'}
EVIDENCE_FILES.update({'deletion-public-original.jpg', 'native-original.jpg', 'source-native-form-original.jpg', 'public-original.jpg', 'evidence-conversion-provenance.json', 'deletion-native-original.jpg'})


def digest(body):return hashlib.sha256(body).hexdigest()
def safe(path):
    path=Path(path)
    if not path.is_absolute() or path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)):
        raise RuntimeError('RECORDING_PATH_REJECTED')
    return path


def read(path,maximum=1_000_000,expected_sha=None):
    path=safe(path);before=path.stat()
    if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=maximum:
        raise RuntimeError('RECORDING_FILE_REJECTED')
    body=path.read_bytes();after=path.stat()
    if len(body)!=before.st_size or any(getattr(before,key)!=getattr(after,key) for key in
            ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) or expected_sha is not None and digest(body)!=expected_sha:
        raise RuntimeError('RECORDING_FILE_HASH_OR_IDENTITY_CHANGED')
    return body


def archive_files(path,expected_sha,names):
    raw=read(path,maximum=5_000_000,expected_sha=expected_sha)
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        members=archive.getmembers()
        if len(members)!=len(names) or {m.name for m in members}!=names or any(
                not m.isfile() or m.issym() or m.islnk() or not 0<m.size<2_000_000 for m in members):
            raise RuntimeError('RECORDING_ARCHIVE_MEMBERS_REJECTED')
        return {m.name:archive.extractfile(m).read() for m in members}


def verify_tests(test_archive):
    archived=archive_files(test_archive,TEST_SHA,TEST_FILES|{'manifest.json'})
    manifest=json.loads(archived['manifest.json'])
    attest_file=TEST_DIR/'test-attestation.json';raw=read(attest_file);attest=json.loads(raw)
    if (attest['version']!='owner-assisted-short-overlay-ubuntu-tests-v1' or attest['image']!=IMAGE or
            attest['archive_sha256']!=TEST_SHA or attest['manifest']!=manifest or attest['tests']!=23 or
            attest['groups']!={'tests/test_owner_assisted_short_result_v1.py':23} or
            attest['failures']!=0 or attest['errors']!=0 or attest['skipped']!=0 or attest['exit_code']!=0 or
            attest['oom_killed'] is not False or attest['network']!='none'):
        raise RuntimeError('EXACT_23_TEST_ATTESTATION_REQUIRED')
    for name in ('full_suite_passed','runtime_readonly_overlays_only','runtime_matches_declared_candidate',
                 'mounts_match_isolated_allowlist','no_pid_or_oom_denial'):
        if attest[name] is not True:raise RuntimeError('TEST_RUNTIME_GATE_FAILED')
    for name in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','whole_tree_deployed'):
        if attest[name] is not False:raise RuntimeError('TEST_ISOLATION_GATE_FAILED')
    for name in TEST_FILES:
        body=read(TEST_DIR/'overlay'/name,expected_sha=manifest['files'][name]['sha256'])
        if body!=archived[name] or len(body)!=manifest['files'][name]['bytes']:
            raise RuntimeError('TESTED_OVERLAY_SOURCE_CHANGED')
    if digest(archived[SHORT])!=SHORT_SHA or digest(archived[MODULE])!=MODULE_SHA:
        raise RuntimeError('EXACT_LEGACY_SHORT_AND_ASSISTED_MODULE_REQUIRED')
    read(OWNED/'results.xml',maximum=2_000_000,expected_sha=attest['junit_sha256'])
    read(TEST_DIR/'tests.log',maximum=5_000_000,expected_sha=attest['tests_log_sha256'])
    container=json.loads(read(TEST_DIR/'container.json'))
    if (container['Id']!=attest['container_id'] or container['Image']!=IMAGE or container['State']['ExitCode']!=0 or
            container['State']['OOMKilled'] is not False or container['Config']['User']!='10001:10001'):
        raise RuntimeError('TEST_CONTAINER_EVIDENCE_CHANGED')
    return {'file':str(attest_file),'sha256':digest(raw),'tests':23,'module_sha256':MODULE_SHA,'short_sha256':SHORT_SHA},archived


def open_destination():
    """Walk by directory fds with NOFOLLOW, creating only the two fixed dirs."""
    allowed={DEST.parent,DEST};fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    current=Path('/')
    try:
        for component in DEST.parts[1:]:
            current=current/component
            try:child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            except FileNotFoundError:
                if current not in allowed:raise RuntimeError('JOB_MEDIA_PARENT_MISSING') from None
                os.mkdir(component,mode=0o700,dir_fd=fd)
                child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
            if current in allowed:os.fchmod(fd,0o700);os.fchown(fd,10001,10001)
        return fd
    except BaseException:
        os.close(fd);raise


def install_evidence(bodies):
    descriptor=open_destination();created=[];matched=[]
    try:
        if set(os.listdir(descriptor))-EVIDENCE_FILES:raise RuntimeError('UNEXPECTED_EXISTING_EVIDENCE_FILES')
        for name,body in bodies.items():
            try:fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=descriptor)
            except FileExistsError:
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=descriptor)
                try:
                    before=os.fstat(fd)
                    if not stat.S_ISREG(before.st_mode) or before.st_size!=len(body):raise RuntimeError('EXISTING_EVIDENCE_DIFFERS')
                    existing=bytearray()
                    while part:=os.read(fd,65536):existing.extend(part)
                    after=os.fstat(fd)
                    if bytes(existing)!=body or any(getattr(before,key)!=getattr(after,key) for key in
                            ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):raise RuntimeError('EXISTING_EVIDENCE_DIFFERS')
                    os.fchmod(fd,0o600);os.fchown(fd,10001,10001)
                finally:os.close(fd)
                matched.append(name)
            else:
                with os.fdopen(fd,'wb') as stream:
                    stream.write(body);stream.flush();os.fchmod(stream.fileno(),0o600);os.fchown(stream.fileno(),10001,10001);os.fsync(stream.fileno())
                created.append(name)
        os.fsync(descriptor);actual=os.stat(DEST,follow_symlinks=False);opened=os.fstat(descriptor)
        if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev,actual.st_ino)!=(opened.st_dev,opened.st_ino):
            raise RuntimeError('EVIDENCE_DIRECTORY_CHANGED')
    finally:os.close(descriptor)
    return {'created':created,'matched_existing':matched,'directory':str(DEST),'uid':10001,'directory_mode':'0700','file_mode':'0600'}


def persist(path,value):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:
        stream.write((json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode());stream.flush();os.fchmod(stream.fileno(),0o600);os.fsync(stream.fileno())
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def command(args,*,timeout=90):
    return subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,text=True,encoding='utf-8',timeout=timeout,
        env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':'C.UTF-8','LC_ALL':'C.UTF-8'})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group();mode.add_argument('--check',action='store_true');mode.add_argument('--record-result',action='store_true');mode.add_argument('--record-deletion',action='store_true')
    parser.add_argument('--evidence-archive',default=str(HOME/'actual-short-assisted-and-owner-deletion-evidence-v1b.tar.gz'))
    parser.add_argument('--test-archive',default=str(HOME/'owner-assisted-short-overlay-v1.tar.gz'))
    parser.add_argument('--helper',default=str(HOME/'apply_actual_short_assisted_record_v1b.py'))
    args=parser.parse_args()
    if os.name!='posix' or os.geteuid()!=0:raise RuntimeError('ROOT_ORCHESTRATOR_REQUIRED')
    os.umask(0o077)
    operation='record-result' if args.record_result else 'record-deletion' if args.record_deletion else 'check'
    test_evidence,tested=verify_tests(Path(args.test_archive))
    bodies=archive_files(Path(args.evidence_archive),EVIDENCE_SHA,EVIDENCE_FILES)
    helper=read(args.helper,expected_sha=HELPER_SHA)
    plan=json.loads(bodies['recording-plan.json'])
    if plan['server_directory']!=str(DEST) or plan['job_id']!=JOB or plan['external_id']!=BV:
        raise RuntimeError('FIXED_RECORDING_PLAN_REQUIRED')
    for name,reference in plan['files'].items():
        if name not in bodies or reference!={'file':str(DEST/name),'bytes':len(bodies[name]),'sha256':digest(bodies[name])}:
            raise RuntimeError('RECORDING_PLAN_FILE_BINDING_CHANGED')
    if set(plan['files'])!=EVIDENCE_FILES-{'recording-plan.json'}:raise RuntimeError('EXACT_EVIDENCE_PLAN_FILES_REQUIRED')
    op=HOME/'lighthouse-evidence'/('actual-short-recording-v1b-'+str(uuid.uuid4()));op.mkdir(mode=0o700)
    staged=op/'apply.py';staged.write_bytes(helper);staged.chmod(0o444)
    extraction=install_evidence(bodies)
    intent={'version':'actual-short-recording-operation-v1b','operation':operation,'job_id':JOB,'external_id':BV,
        'test_attestation':test_evidence,'evidence_archive_sha256':EVIDENCE_SHA,'helper_sha256':HELPER_SHA,
        'evidence_installation':extraction,'deletion_origin':'owner_manual_native_web','browser_action_executed':False}
    persist(op/'intent.json',intent)
    inspected=command(['docker','image','inspect','--format','{{.Id}}',IMAGE])
    if inspected.returncode or inspected.stdout.strip()!=IMAGE:raise RuntimeError('PINNED_IMAGE_REQUIRED')
    name='lighthouse-short-record-'+op.name[-36:-28]
    argv=['docker','create','--pull=never','--name',name,'--init','--restart=no','--network','none','--read-only',
        '--user','10001:10001','--cap-drop','ALL','--security-opt','no-new-privileges:true','--memory','256m','--cpus','0.5','--pids-limit','64',
        '--tmpfs','/tmp:rw,size=32m,mode=1777','--tmpfs',str(ROOT/'data/browser-profiles')+':rw,size=1m,mode=0700,uid=10001,gid=10001',
        '-e','HUB_ROOT='+str(ROOT),'-e','HOME=/tmp/short-record-home','-e','PYTHONDONTWRITEBYTECODE=1']
    mounts=[(ROOT/'data',ROOT/'data',False),(ROOT/'media',ROOT/'media',False),(ROOT/'config',ROOT/'config',True),
        (TEST_DIR/'overlay'/SHORT,ROOT/SHORT,True),(TEST_DIR/'overlay'/MODULE,ROOT/MODULE,True),(staged,Path('/run/actual-short-record.py'),True)]
    for source,target,readonly in mounts:
        safe(source);argv+=['--mount','type=bind,source='+str(source)+',target='+str(target)+(',readonly' if readonly else '')]
    argv += [IMAGE,'python','/run/actual-short-record.py','--evidence-dir',str(DEST)]
    if operation!='check':argv+=['--'+operation]
    created=command(argv)
    if created.returncode or not re.fullmatch(r'[a-f0-9]{64}',created.stdout.strip()):
        persist(op/'result.json',{**intent,'state':'container_create_failed','exit_code':created.returncode})
        raise RuntimeError('RECORDING_CONTAINER_CREATE_FAILED')
    cid=created.stdout.strip();persist(op/'container-created.json',{'container_id':cid,'container_name':name,'operation':operation})
    candidate=command(['docker','inspect',cid])
    c=json.loads(candidate.stdout)[0] if candidate.returncode==0 else None
    if not c:raise RuntimeError('CREATED_CONTAINER_INSPECTION_REQUIRED')
    host=c['HostConfig'];actual_mounts={m['Source']:(m['Destination'],m['RW']) for m in c['Mounts'] if m['Type']=='bind'}
    expected_mounts={str(source):(str(target),not readonly) for source,target,readonly in mounts}
    child_env=dict(value.split('=',1) for value in c['Config']['Env'])
    if (c['Image']!=IMAGE or c['Config']['User']!='10001:10001' or host['NetworkMode']!='none' or
            host['ReadonlyRootfs'] is not True or host['Memory']!=256*1024**2 or host['PidsLimit']!=64 or
            host.get('Init') is not True or 'ALL' not in host.get('CapDrop',[]) or
            not any(value in ('no-new-privileges','no-new-privileges:true') for value in host.get('SecurityOpt',[])) or
            actual_mounts!=expected_mounts or str(ROOT/'data/browser-profiles') not in host.get('Tmpfs',{}) or
            any(child_env.get(key) for key in ('OPENAI_API_KEY','X_BEARER_TOKEN','YOUTUBE_API_KEY','BILIBILI_ACCESS_TOKEN','EVENT_ENCRYPTION_KEY'))):
        raise RuntimeError('CREATED_CONTAINER_ISOLATION_GATE_FAILED')
    timed_out=False;failure=None
    try:
        started=command(['docker','start',cid])
        if started.returncode:raise RuntimeError('RECORDING_CONTAINER_START_FAILED')
        command(['docker','wait',cid],timeout=90)
    except BaseException as error:
        timed_out=isinstance(error,subprocess.TimeoutExpired);failure=type(error).__name__
    inspected=command(['docker','inspect',cid]);container=json.loads(inspected.stdout)[0] if inspected.returncode==0 else None
    logs=command(['docker','logs',cid]);stdout=logs.stdout;stderr=logs.stderr
    if len(stdout)>32768 or len(stderr)>32768:raise RuntimeError('RECORDING_DIAGNOSTICS_LIMIT_EXCEEDED')
    persist(op/'application-output.json',{'stdout':stdout,'stderr':stderr})
    state=container['State'] if container else {}
    application=None
    try:application=json.loads(stdout.strip())
    except (ValueError,TypeError):pass
    success=bool(not failure and not timed_out and container and state.get('Running') is False and state.get('ExitCode')==0 and
        state.get('OOMKilled') is False and isinstance(application,dict) and application.get('job_id')==JOB)
    if success and operation=='check':success=application.get('state')=='checked_recording_plan_only' and application.get('database_written') is False
    elif success and operation=='record-result':success=application.get('state')=='completed' and application.get('external_id')==BV and type(application.get('changed')) is bool
    elif success and operation=='record-deletion':success=application.get('external_id')==BV and application.get('remote_state')=='owner_deleted' and type(application.get('changed')) is bool
    receipt={**intent,'state':'operation_confirmed' if success else 'operation_failed_or_unknown_reconcile_before_retry',
        'container_id':cid,'container_name':name,'container_running':state.get('Running'),'application_exit_code':state.get('ExitCode'),
        'oom_killed':state.get('OOMKilled'),'wait_timed_out':timed_out,'orchestration_error_type':failure,
        'application_result':application,'result_file':str(op/'result.json'),'database_opened_by_root':False,
        'network':'none','browser_profile_masked':True,'secret_environment_injected':False,'local_media_deleted':False,
        'automatic_retry_performed':False}
    persist(op/'result.json',receipt)
    if container:persist(op/'container-inspect.json',container)
    print(json.dumps(receipt,ensure_ascii=False))
    if not success:raise SystemExit(2)


if __name__=='__main__':
    main()
