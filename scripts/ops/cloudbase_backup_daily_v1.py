"""Root-only daily acceptance then exact verified-clone cleanup.
No shell/network client, credentials, active DB mutation, or arbitrary paths.
"""
from datetime import datetime,timezone
import argparse,hashlib,json,os,re,stat,subprocess,sys,uuid
from pathlib import Path
ROOT=Path('/opt/lighthouse-hub')
OPS=Path('/usr/local/lib/lighthouse-backup-ops')
STATE=Path('/var/lib/lighthouse-backup-ops')
IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
PINS={'run_cloudbase_backup_sidecar_v1e.py':'0ce1bb9a362a5429ad71631d551b16ab043462295acd54fceb8998107ba4483e',
    'accept_cloudbase_backup_capture_v1.py':'f3a9a0543a236c5d2e94a209d6e2074c7b3828fce6641ce4fa7d780ac5f17efb',
    'prune_cloudbase_verified_clones_v1.py':'58960bfbbeab64776bdfb23f69c83cfb4a80b5b384a5a5d12bcdf6fe91cbcafe'}
PENDING={'accepted_pending_prune','prune_dry_run_started','prune_started','prune_failed'}
def ident(value):assert str(uuid.UUID(value))==value;return value
def digest(data):return hashlib.sha256(data).hexdigest()
def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def safe(path,base):
    assert path.is_absolute() and path.resolve()==path and path.is_relative_to(base)
    assert not path.is_symlink() and not any(p.is_symlink() for p in path.parents)
    return path

def protected(path,*,file=False):
    safe(path,Path('/'))
    for current in (path,*path.parents):
        s=current.stat();assert s.st_uid==0 and s.st_mode&0o022==0
    assert path.is_file() if file else path.is_dir()

def verify_helpers():
    protected(OPS)
    for name,sha in PINS.items():
        path=OPS/name;protected(path,file=True);assert path.stat().st_size<=100000 and digest(path.read_bytes())==sha
    observed=subprocess.check_output(['/usr/bin/docker','image','inspect','-f','{{.Id}}',IMAGE],text=True,timeout=30).strip();assert observed==IMAGE

def read_record(path,base,*,maximum=5_000_000,root_owned=False):
    safe(path,base);s=path.stat();assert stat.S_ISREG(s.st_mode) and s.st_size<=maximum and s.st_mode&0o077==0
    assert s.st_uid==0 if root_owned else s.st_uid in (0,10001)
    data=path.read_bytes();assert len(data)==s.st_size;return json.loads(data),digest(data)

def atomic(path,value):
    protected(path.parent);assert not path.is_symlink();temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial')
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(encoded(value));stream.flush();os.fsync(stream.fileno())
    os.replace(temp,path);fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def save(record):
    record['updated_at']=datetime.now(timezone.utc).isoformat();atomic(STATE/'history'/(record['daily_id']+'.json'),record);atomic(STATE/'latest.json',record)

def call(script,args,timeout):
    # Fixed protected scripts and validated absolute arguments only; no shell or
    # account data in the environment. CloudBase key is mounted only by wrapper.
    env={'PATH':'/usr/bin:/bin','HOME':'/root','PYTHONNOUSERSITE':'1','LANG':'C.UTF-8'}
    result=subprocess.run(['/usr/bin/python3',str(OPS/script),*args],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=timeout,env=env)
    assert len(result.stdout)<=8_000_000 and len(result.stderr)<=1_000_000
    try:value=json.loads(result.stdout)
    except (ValueError,TypeError):raise RuntimeError('OPS_OUTPUT_NOT_JSON') from None
    if result.returncode!=0:raise RuntimeError('OPS_CHILD_FAILED')
    return value

def accept_reference(status,root=ROOT):
    assert status['version']=='cloudbase-backup-host-service-v1e' and status['mode']=='accept' and status['exit_code']==0 and status['image']==IMAGE
    assert status['uid']==10001 and status['offline'] is False and status['credentials_mounted'] is True and status['core_service_images_changed'] is False
    value=status['result'];assert value['state']=='remote_archive_restored_and_verified' and value['sqlite_integrity']==value['sqlite_foreign_keys']=='ok'
    assert value['additional_remote_gets']==value['local_deletions']==0 and value['stage_prune_candidates']==3
    operation=ident(value['operation_id']);path=Path(value['report_file'])
    assert path.name=='restoration-report.json' and path.parent.parent==root/'backups/cloudbase-restore-check'
    acceptance=ident(path.parent.name);safe(path,root/'backups/cloudbase-restore-check')
    return {'report_file':str(path),'operation_id':operation,'acceptance_id':acceptance}

def validate_report(reference,root=ROOT):
    path=Path(reference['report_file']);assert path==root/'backups/cloudbase-restore-check'/ident(reference['acceptance_id'])/'restoration-report.json'
    report,sha=read_record(path,root)
    assert report['version']=='cloudbase-actual-remote-restore-acceptance-v1' and report['operation_id']==ident(reference['operation_id']) and report['acceptance_id']==reference['acceptance_id']
    assert all(report.get(k) is True for k in ('actual_archive_get_captured','all_entries_verified','credential_exclusions_verified'))
    assert report['sqlite_integrity']==report['sqlite_foreign_keys']=='ok' and report['additional_remote_gets']==report['local_deletions']==0
    assert report['historical_raw_backups_migrated'] is False
    if reference.get('report_sha256'):assert sha==reference['report_sha256']
    return sha

def cleanup(record,*,runner=call,saver=save,validator=validate_report):
    reference=record['acceptance'];assert validator(reference)==reference['report_sha256']
    record['state']='prune_dry_run_started';saver(record)
    args=['--report',reference['report_file']]
    dry=runner('prune_cloudbase_verified_clones_v1.py',args+['--dry-run'],300)
    assert dry['state']=='dry_run_verified' and dry['apply'] is False and dry['deleted_files']==0 and dry['remote_requests']==0
    assert dry['historical_raw_backups_touched'] is False and dry['business_sources_touched'] is False
    journal=str(ROOT/'backups/cloudbase-prune'/(reference['acceptance_id']+'.intent.json'));assert dry['journal_file']==journal
    assert type(dry['candidate_files']) is int and 3<dry['candidate_files']<6005 and dry['candidate_bytes']>0
    record['prune_plan']={k:dry[k] for k in ('candidate_files','candidate_bytes','journal_file')}
    assert validator(reference)==reference['report_sha256']
    record['state']='prune_started';saver(record)
    applied=runner('prune_cloudbase_verified_clones_v1.py',args+['--apply'],300)
    assert applied['state']=='completed' and applied['apply'] is True and applied['remote_requests']==0
    assert all(applied[k]==dry[k] for k in ('candidate_files','candidate_bytes','journal_file')) and applied['deleted_files']==dry['candidate_files']
    assert applied['historical_raw_backups_touched'] is False and applied['business_sources_touched'] is False
    record['state']='completed';record['pruned_files']=applied['deleted_files'];record['completed_at']=datetime.now(timezone.utc).isoformat();saver(record)
    return record

def reference_from_existing(path,root=ROOT):
    path=Path(path);assert path.name=='restoration-report.json' and path.parent.parent==root/'backups/cloudbase-restore-check'
    acceptance=ident(path.parent.name);report,_=read_record(path,root)
    reference={'report_file':str(path),'acceptance_id':acceptance,'operation_id':ident(report['operation_id'])}
    reference['report_sha256']=validate_report(reference,root);return reference


def stage_adoption(reference,previous=None,*,runner=call,saver=save,validator=validate_report):
    assert validator(reference)==reference['report_sha256']
    if previous:
        assert previous['state']=='completed' or previous['state'] in PENDING
        if previous['state'] in PENDING:
            assert previous['acceptance']==reference;return previous
        assert previous.get('acceptance',{}).get('acceptance_id')!=reference['acceptance_id']
    dry=runner('prune_cloudbase_verified_clones_v1.py',['--report',reference['report_file'],'--dry-run'],300)
    assert dry['state']=='dry_run_verified' and dry['apply'] is False and dry['deleted_files']==dry['remote_requests']==0
    assert dry['historical_raw_backups_touched'] is False and dry['business_sources_touched'] is False
    assert dry['journal_file']==str(ROOT/'backups/cloudbase-prune'/(reference['acceptance_id']+'.intent.json'))
    record={'version':'cloudbase-daily-accept-prune-v1','daily_id':str(uuid.uuid4()),'created_at':datetime.now(timezone.utc).isoformat(),
        'image':IMAGE,'helper_pins':PINS,'state':'accepted_pending_prune','acceptance':reference,'adopted_existing_acceptance':True,
        'automatic_mcp_notification_integrated':False,'historical_raw_backups_migrated':False,'business_sources_touched':False}
    saver(record);return record


def execute(previous=None,*,runner=call,saver=save,validator=validate_report):
    if previous:assert previous['state'] in PENDING|{'completed','accept_started','accept_failed','reconciliation_required'}
    if previous and previous['state'] in PENDING:
        record=previous
    else:
        if previous and previous['state'] in ('accept_started','accept_failed','reconciliation_required'):
            raise RuntimeError('ACCEPT_INTERRUPTED_RECONCILIATION_REQUIRED')
        record={'version':'cloudbase-daily-accept-prune-v1','daily_id':str(uuid.uuid4()),'created_at':datetime.now(timezone.utc).isoformat(),
            'image':IMAGE,'helper_pins':PINS,'state':'accept_started','automatic_mcp_notification_integrated':False,
            'historical_raw_backups_migrated':False,'business_sources_touched':False}
        saver(record)
        try:
            status=runner('run_cloudbase_backup_sidecar_v1e.py',['--accept'],960)
            reference=accept_reference(status);reference['report_sha256']=validator(reference)
            record['acceptance']=reference;record['state']='accepted_pending_prune';saver(record)
        except BaseException:
            # No report means no prune. The runtime ledger independently blocks
            # new writes after unknown PUTs; a nonzero result is not verification.
            record['state']='accept_failed';record['error_code']='ACCEPT_FAILED_OR_UNVERIFIED';saver(record);raise
    try:return cleanup(record,runner=runner,saver=saver,validator=validator)
    except BaseException:
        record['state']='prune_failed';record['error_code']='VERIFIED_CLONE_PRUNE_NOT_COMPLETED';saver(record);raise

def main():
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
    import fcntl
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group();mode.add_argument('--verify-only',action='store_true');mode.add_argument('--run-once',action='store_true');mode.add_argument('--adopt-existing-acceptance');args=parser.parse_args()
    verify_helpers()
    if not args.run_once and not args.adopt_existing_acceptance:
        print(json.dumps({'state':'verified_not_run','image':IMAGE,'helper_pins':PINS,'remote_requests':0,'local_deletions':0}));return 0
    for folder in (STATE,STATE/'history'):
        safe(folder,Path('/'))
        if not folder.exists():folder.mkdir(mode=0o700)
        protected(folder);assert folder.stat().st_mode&0o077==0
    lock=STATE/'daily.lock';assert not lock.is_symlink();fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        assert os.fstat(fd).st_uid==0;fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        previous=None
        if (STATE/'latest.json').exists():
            previous,_=read_record(STATE/'latest.json',STATE,root_owned=True)
            assert previous['version']=='cloudbase-daily-accept-prune-v1' and previous['image']==IMAGE and previous['helper_pins']==PINS
            ident(previous['daily_id'])
        if args.adopt_existing_acceptance:
            reference=reference_from_existing(args.adopt_existing_acceptance)
            record=stage_adoption(reference,previous)
            print(json.dumps({'state':'existing_acceptance_staged_for_cleanup','daily_id':record['daily_id'],'operation_id':reference['operation_id'],
                'additional_remote_requests':0,'local_deletions':0,'next_run':'resume_exact_prune_only'}));return 0
        record=execute(previous)
        print(json.dumps({'state':'accepted_restored_pruned','daily_id':record['daily_id'],'operation_id':record['acceptance']['operation_id'],
            'acceptance_id':record['acceptance']['acceptance_id'],'pruned_files':record['pruned_files'],'additional_remote_gets':0,
            'history_file':str(STATE/'history'/(record['daily_id']+'.json')),'automatic_mcp_notification_integrated':False}));return 0
    finally:os.close(fd)
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        if os.name=='posix' and os.geteuid()==0 and STATE.is_dir():
            try:atomic(STATE/'last-error.json',{'version':'cloudbase-daily-accept-prune-error-v1','at':datetime.now(timezone.utc).isoformat(),
                'error_code':type(exc).__name__,'automatic_mcp_notification_integrated':False})
            except Exception:pass
        print(json.dumps({'state':'stopped_safely','error_code':type(exc).__name__,'automatic_mcp_notification_integrated':False}));raise SystemExit(2) from None
