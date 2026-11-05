"""Collect bounded metadata-only CloudBase v1e evidence. No DB/media/profile/keys.
Root executes once; no API, upload, service mutation, cleanup or arbitrary shell.
"""
from datetime import datetime,timezone
import hashlib,io,json,os,re,stat,subprocess,tarfile,uuid
from pathlib import Path
ROOT=Path('/opt/lighthouse-hub');EV=Path('/home/lighthouse-ops/lighthouse-evidence');OPS=Path('/usr/local/lib/lighthouse-backup-ops');STATE=Path('/var/lib/lighthouse-backup-ops')
IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
SENSITIVE_KEYS={'secretid','secretkey','authorization','cookie','password','passwd','accesstoken','refreshtoken','apikey','sessdata','bilijct'}
LITERAL=re.compile(br'(?i)(?:\bAKID[A-Za-z0-9]{12,}\b|\bsk-[A-Za-z0-9_-]{12,}\b|Bearer[ \t]+[A-Za-z0-9._~+/-]{16,}|SESSDATA=[A-Za-z0-9%._-]{8,})')
FILES={};TOTAL=0
UNIT_SHA256={
    'lighthouse-cloudbase-backup.service':'a844b59a1e57bc612d7b1478df191b2595009d2b2d5fc5ff514450e3144c8df6',
    'lighthouse-cloudbase-backup-failure.service':'def20c7cc0c5303c6d8ba0be62aba896a498cb31c7f3d946f2bf94712ad0d357',
    'lighthouse-cloudbase-backup.timer':'58ac9795fd895077a3f3461e685528b18ac6d44fece78acdc8744fdea643964a'}

def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def digest(data):return hashlib.sha256(data).hexdigest()
def ident(value):assert str(uuid.UUID(value))==value;return value

def scan(value):
    if isinstance(value,dict):
        for key,item in value.items():
            normalized=re.sub('[^a-z]','',key.lower())
            assert not (normalized in SENSITIVE_KEYS and isinstance(item,str) and item)
            scan(item)
    elif isinstance(value,list):
        for item in value:scan(item)

def read(path,maximum=5_000_000):
    path=Path(path);assert path.is_absolute() and path.resolve()==path and not path.is_symlink() and not any(p.is_symlink() for p in path.parents)
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0))
    try:
        before=os.fstat(fd);assert stat.S_ISREG(before.st_mode) and before.st_size<=maximum
        data=b''
        while True:
            part=os.read(fd,1048576)
            if not part:break
            data+=part;assert len(data)<=maximum
        after=os.fstat(fd);assert (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)==(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns)
    finally:os.close(fd)
    assert len(data)==before.st_size and not LITERAL.search(data)
    return data

def add(name,data,source=None):
    global TOTAL
    assert re.fullmatch('[A-Za-z0-9_.@/-]{1,240}',name) and not name.startswith('/') and '..' not in Path(name).parts and name not in FILES
    assert len(FILES)<120 and TOTAL+len(data)<=25_000_000;TOTAL+=len(data)
    FILES[name]={'data':data,'source':str(source) if source else 'derived_metadata_only'}

def json_file(name,path,*,optional=False):
    if optional and not Path(path).exists():return None
    data=read(path);value=json.loads(data);scan(value);add(name,data,path);return value

def text_file(name,path,*,optional=False):
    if optional and not Path(path).exists():return
    data=read(path,2_000_000);add(name,data,path)

def unit_file(name,path):
    # Generic credential patterns cannot prove arbitrary unit text is safe.
    # Only these pre-reviewed public templates may be copied as raw evidence.
    assert name in UNIT_SHA256
    data=read(path,65536);assert digest(data)==UNIT_SHA256[name]
    add('units/'+name,data,path)

def main():
    import pwd
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
    # Exact retained acceptance pointer; never hand-transcribe an acceptance UUID.
    current=EV/'cloudbase-backup-v1e'
    pointer=json_file('evidence/real-remote-backup-restore-verified.json',current/'real-remote-backup-restore-verified.json')
    result=pointer['result'];assert result['state']=='remote_archive_restored_and_verified' and result['sqlite_integrity']==result['sqlite_foreign_keys']=='ok'
    report_file=Path(result['report_file']);assert report_file.name=='restoration-report.json' and report_file.parent.parent==ROOT/'backups/cloudbase-restore-check'
    acceptance=ident(report_file.parent.name);operation=ident(result['operation_id'])
    report=json_file('retained/restoration-report.json',report_file)
    assert report['operation_id']==operation and report['acceptance_id']==acceptance and report['all_entries_verified'] is True and report['entries']==88
    assert report['sqlite_integrity']==report['sqlite_foreign_keys']=='ok' and report['credential_exclusions_verified'] is True and report['additional_remote_gets']==0
    runtime=json_file('retained/runtime-result.json',report_file.parent/'runtime-result.json');assert runtime['operation_id']==operation
    receipt_path=Path(runtime['remote']['receipt_file']);adapter=ident(runtime['remote']['operation_id']);assert receipt_path==ROOT/'backups/cloudbase-receipts'/(adapter+'.verified.json')
    receipt=json_file('retained/verified-receipt.json',receipt_path);assert receipt['remote_verified'] is True and receipt['verification_method']=='archive_and_manifest_full_remote_get'
    assert {k:v for k,v in runtime['remote'].items() if k!='receipt_file'}==receipt
    json_file('retained/upload-intent.json',receipt_path.with_name(adapter+'.intent.json'))
    canary=runtime['fresh_private_canary'];canary_path=Path(canary['file']);assert canary_path==ROOT/'data/cloudbase-backup-canary-checks'/(operation+'.json')
    live=json_file('retained/fresh-private-canary.json',canary_path);assert live['private_storage_verified'] is True and digest(FILES['retained/fresh-private-canary.json']['data'])==canary['sha256']
    prune=json_file('retained/prune-intent.json',ROOT/'backups/cloudbase-prune'/(acceptance+'.intent.json'))
    assert prune['operation_id']==operation and prune['acceptance_id']==acceptance and prune['state']=='completed' and prune['deleted_files']==92
    assert all(item['state']=='deleted' for item in prune['candidates'])
    assert prune['historical_raw_backups_touched'] is False and prune['business_sources_touched'] is False
    latest=json_file('root-ops/latest.json',STATE/'latest.json');assert latest['state']=='completed' and latest['acceptance']['operation_id']==operation and latest['acceptance']['acceptance_id']==acceptance
    json_file('root-ops/history.json',STATE/'history'/(ident(latest['daily_id'])+'.json'))
    json_file('root-ops/last-error.json',STATE/'last-error.json',optional=True)
    for folder in sorted((STATE/'install-history').iterdir()):
        if folder.is_dir() and re.fullmatch('[a-f0-9-]{36}',folder.name):
            ident(folder.name);json_file('root-ops/installs/'+folder.name+'.json',folder/'installation-report.json',optional=True)
    for name in ('test-attestation.json','actual-systemd-restore-prune-completed.json'):
        json_file('evidence/'+name,current/name)
    for name in ('tests.log','image-id.txt','test-exit.txt'):
        text_file('evidence/'+name,current/name,optional=True)
    opsev=EV/'cloudbase-ops-fixtures-v1e'
    json_file('ops-tests/test-attestation.json',opsev/'test-attestation.json')
    text_file('ops-tests/tests.log',opsev/'tests.log',optional=True)
    oldhead=EV/'cloudbase-backup-v1d/failed-upload-exact-head-reconciliation.json'
    head=json_file('historical/failed-upload-exact-head-reconciliation.json',oldhead,optional=True)
    if isinstance(head,dict) and isinstance(head.get('report_file'),str):
        hp=Path(head['report_file']);assert hp.parent==ROOT/'backups/cloudbase-reconciliation-head' and hp.suffix=='.json';ident(hp.stem)
        json_file('historical/head-reconciliation-report.json',hp)
    unit_paths={name:Path('/etc/systemd/system')/name for name in ('lighthouse-cloudbase-backup.service','lighthouse-cloudbase-backup-failure.service','lighthouse-cloudbase-backup.timer')}
    for name,path in unit_paths.items():unit_file(name,path)
    statuses={}
    for name in unit_paths:
        argv=['/usr/bin/systemctl','show',name,'--property=Id,ActiveState,SubState,UnitFileState,Result,ExecMainStatus,ExecMainCode,NextElapseUSecRealtime,LastTriggerUSec']
        completed=subprocess.run(argv,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=25);assert completed.returncode==0
        values=dict(line.split('=',1) for line in completed.stdout.splitlines() if '=' in line);scan(values);statuses[name]=values
    add('derived/systemd-status.json',encoded(statuses))
    installed={}
    for name in ('run_cloudbase_backup_sidecar_v1e.py','accept_cloudbase_backup_capture_v1.py','prune_cloudbase_verified_clones_v1.py','cloudbase_backup_daily_v1.py'):
        path=OPS/name;body=read(path,100000);s=path.stat();assert s.st_uid==0 and s.st_mode&0o022==0
        installed[name]={'bytes':len(body),'sha256':digest(body),'uid':s.st_uid,'mode':oct(s.st_mode&0o777)}
    observed=subprocess.check_output(['/usr/bin/docker','image','inspect','-f','{{.Id}}',IMAGE],text=True,timeout=25).strip();assert observed==IMAGE
    add('derived/installed-operator-hashes.json',encoded({'image':observed,'files':installed}))
    manifest=runtime['bundle']['manifest'];member=manifest['files']['database/credential-excluded-operational.sqlite3']
    summary={'version':'cloudbase-final-v1e-metadata-snapshot-statistics-v1','operation_id':operation,'acceptance_id':acceptance,
        'snapshot_kind':manifest['snapshot']['kind'],'database_member':member,'credential_exclusions':manifest['snapshot']['excluded_tables'],
        'restored_counts':report['restored_counts'],'tweet_jobs':manifest['tweet_jobs'],'archive_member_count':report['entries'],
        'database_operational_log_records':manifest['database_operational_log_records'],'raw_docker_logs_captured':manifest['raw_docker_logs_captured'],
        'directory_json_reports_captured':manifest['directory_json_reports_captured'],'pruned_files':prune['deleted_files'],
        'pruned_logical_bytes':sum(i['bytes'] for i in prune['candidates']),'historical_raw_backups_migrated':False,
        'db_archive_media_profile_or_secret_bytes_copied':False,'source_archive_was_deleted_after_verified_restore':not Path(runtime['bundle']['archive']).exists()}
    scan(summary);add('derived/snapshot-statistics.json',encoded(summary))
    index={'version':'cloudbase-final-v1e-safe-proof-collection-v1','created_at':datetime.now(timezone.utc).isoformat(),'image':IMAGE,
        'files':{name:{'bytes':len(item['data']),'sha256':digest(item['data']),'source':item['source']} for name,item in FILES.items()},
        'db_archive_media_profile_or_secret_bytes_copied':False,'external_api_requests':0,'services_modified':False,'files_deleted':0}
    manifest_bytes=encoded(index)
    delivery=Path('/home/lighthouse-ops')/('lighthouse-cloudbase-v1e-safe-proof-'+str(uuid.uuid4()));delivery.mkdir(mode=0o700)
    archive=delivery/'evidence.tar.gz'
    with tarfile.open(archive,'w:gz') as tar:
        for name,item in {**FILES,'MANIFEST.json':{'data':manifest_bytes}}.items():
            data=item['data'];info=tarfile.TarInfo(name);info.mode=0o600;info.size=len(data);tar.addfile(info,io.BytesIO(data))
    archive.chmod(0o600);(delivery/'MANIFEST.json').write_bytes(manifest_bytes);(delivery/'MANIFEST.json').chmod(0o600)
    output={'version':'cloudbase-final-v1e-safe-proof-artifact-v1','archive':str(archive),'archive_bytes':archive.stat().st_size,'archive_sha256':digest(archive.read_bytes()),
        'manifest':str(delivery/'MANIFEST.json'),'manifest_sha256':digest(manifest_bytes),'files':len(FILES),'external_api_requests':0,'files_deleted':0}
    data=encoded(output);(delivery/'artifact.json').write_bytes(data);(delivery/'artifact.json').chmod(0o600)
    user=pwd.getpwnam('lighthouse-ops')
    for path in (archive,delivery/'MANIFEST.json',delivery/'artifact.json',delivery):os.chown(path,user.pw_uid,user.pw_gid)
    print(json.dumps(output));return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print(json.dumps({'state':'stopped_safely','error_code':type(exc).__name__,'external_api_requests':0,'files_deleted':0}));raise SystemExit(2) from None
