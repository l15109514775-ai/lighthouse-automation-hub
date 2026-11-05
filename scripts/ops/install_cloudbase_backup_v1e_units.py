"""Default verify-only. --install writes fixed root-protected ops/units; never enable/start."""
from datetime import datetime,timezone
import argparse,hashlib,json,os,subprocess,tomllib,uuid
from pathlib import Path
ROOT=Path('/opt/lighthouse-hub');OPS=Path('/usr/local/lib/lighthouse-backup-ops');STATE=Path('/var/lib/lighthouse-backup-ops')
IMAGE='sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55'
OVERLAY='298ac4b0c0dbdfc60c27a3890ed914b71ef6d10c'
PINS={'run_cloudbase_backup_sidecar_v1e.py':'0ce1bb9a362a5429ad71631d551b16ab043462295acd54fceb8998107ba4483e',
    'accept_cloudbase_backup_capture_v1.py':'f3a9a0543a236c5d2e94a209d6e2074c7b3828fce6641ce4fa7d780ac5f17efb',
    'prune_cloudbase_verified_clones_v1.py':'6af8e57a2b50d9a47c69c1a7debc957e45f89f140860368e6c761e1c5cf8a9ce',
    'cloudbase_backup_daily_v1.py':'c31d61724d1e6ea2d6c9ab39d55cec9f07e4ae656edbd4965ca647aa3d10f989'}
SERVICE='''[Unit]
Description=Lighthouse verified CloudBase backup, actual restore and exact clone cleanup
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target
OnFailure=lighthouse-cloudbase-backup-failure.service

[Service]
Type=oneshot
User=root
Group=root
UMask=0077
ExecStart=/usr/bin/python3 /usr/local/lib/lighthouse-backup-ops/cloudbase_backup_daily_v1.py --run-once
TimeoutStartSec=30min
TimeoutStopSec=45s
Restart=no
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/run/docker.sock /opt/lighthouse-hub/data/cloudbase-backup-state /opt/lighthouse-hub/data/cloudbase-backup-canary-checks /opt/lighthouse-hub/backups /opt/lighthouse-hub/data /var/lib/lighthouse-backup-ops
StandardOutput=journal
StandardError=journal
'''
FAILURE='''[Unit]
Description=Record independent Lighthouse backup failure status (no MCP notification)

[Service]
Type=oneshot
User=root
Group=root
UMask=0077
ExecStart=/usr/bin/python3 /usr/local/lib/lighthouse-backup-ops/run_cloudbase_backup_sidecar_v1e.py --record-failure
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/opt/lighthouse-hub/data/cloudbase-backup-state
StandardOutput=journal
StandardError=journal
'''
TIMER='''[Unit]
Description=Daily 03:30 Asia/Shanghai Lighthouse verified operational backup and clone cleanup

[Timer]
OnCalendar=*-*-* 03:30:00 Asia/Shanghai
AccuracySec=1min
RandomizedDelaySec=0
Persistent=true
Unit=lighthouse-cloudbase-backup.service

[Install]
WantedBy=timers.target
'''
def sha(data):return hashlib.sha256(data).hexdigest()
def safe(path):
    assert path.is_absolute() and path.resolve()==path and not path.is_symlink() and not any(p.is_symlink() for p in path.parents);return path

def protected(path):
    safe(path)
    for p in (path,*path.parents):assert p.stat().st_uid==0 and p.stat().st_mode&0o022==0

def directory(path,mode):
    safe(path);protected(path.parent)
    if not path.exists():path.mkdir(mode=mode);path.chmod(mode)
    protected(path);assert path.is_dir() and path.stat().st_mode&0o777==mode

def write(path,data,mode=0o644):
    safe(path);protected(path.parent)
    if path.exists():protected(path);assert path.is_file()
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.partial');fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as stream:stream.write(data);stream.flush();os.fsync(stream.fileno())
    os.chmod(temp,mode);os.replace(temp,path);fd=os.open(path.parent,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)

def main():
    assert os.name=='posix' and os.geteuid()==0;os.umask(0o077)
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group();mode.add_argument('--verify-only',action='store_true');mode.add_argument('--install',action='store_true');args=parser.parse_args()
    for unit in ('lighthouse-cloudbase-backup.timer','lighthouse-cloudbase-backup.service','lighthouse-cloudbase-backup-failure.service'):
        active=subprocess.run(['/usr/bin/systemctl','is-active',unit],capture_output=True,text=True).stdout.strip()
        enabled=subprocess.run(['/usr/bin/systemctl','is-enabled',unit],capture_output=True,text=True).stdout.strip()
        assert active not in ('active','activating','reloading','deactivating') and enabled not in ('enabled','enabled-runtime','linked','linked-runtime','masked','masked-runtime')
    assert subprocess.check_output(['/usr/bin/docker','image','inspect','-f','{{.Id}}',IMAGE],text=True).strip()==IMAGE
    att=safe(Path('/home/lighthouse-ops/lighthouse-evidence/cloudbase-backup-v1e/test-attestation.json'));assert att.is_file() and att.stat().st_size<=65536
    rawatt=att.read_bytes();a=json.loads(rawatt)
    assert a['version']=='cloudbase-backup-sidecar-ubuntu-attestation-v1e' and a['image']==IMAGE and a['overlay_commit']==OVERLAY and a['base_commit']=='86f03d3b1ad74c265af71d43194a1eb7a28d4599'
    assert a['exit_code']==0 and a['stats']=={'tests':113,'errors':0,'failures':0,'skipped':0} and a['oom_killed'] is False
    assert a['network']=='none' and a['uid']=='10001:10001' and a['readonly'] is True and a['credentials_mounted'] is False
    assert a['real_backups_uploaded']==a['local_deletions']==0 and a['core_services_changed'] is False and a['complete_overlay_git_tree_deployed'] is False
    source=Path(__file__).resolve().parent;data={}
    for name,digest in PINS.items():
        path=safe(source/name);assert path.is_file() and path.stat().st_size<=100000;body=path.read_bytes();assert sha(body)==digest;data[name]=body
    policy=safe(ROOT/'config/cloudbase-backup.toml');assert policy.is_file() and policy.stat().st_size<=65536 and policy.stat().st_mode&0o022==0
    body=policy.read_bytes();config=tomllib.loads(body.decode('utf-8-sig'));limits=config.get('limits',{})
    summary={'state':'verified_not_installed','verify_only':True,'image':IMAGE,'overlay_commit':OVERLAY,'tests':113,'helper_pins':PINS,
        'policy_sha256':sha(body),'limits':{k:v for k,v in limits.items() if type(v) is int},'timer_enabled':False,'timer_started':False,
        'schedule':'03:30 Asia/Shanghai (19:30 UTC previous day)','budget_ledger_modified':False,'automatic_mcp_failure_notification_integrated':False}
    if not args.install:print(json.dumps(summary));return 0
    directory(OPS,0o755);directory(STATE,0o700);directory(STATE/'history',0o700);directory(STATE/'install-history',0o700)
    operation=str(uuid.uuid4());history=STATE/'install-history'/operation;directory(history,0o700)
    targets={**{OPS/name:body for name,body in data.items()},Path('/etc/systemd/system/lighthouse-cloudbase-backup.service'):SERVICE.encode(),
        Path('/etc/systemd/system/lighthouse-cloudbase-backup-failure.service'):FAILURE.encode(),Path('/etc/systemd/system/lighthouse-cloudbase-backup.timer'):TIMER.encode()}
    changed=[]
    for target,body in targets.items():
        safe(target);old=None
        if target.exists():
            protected(target);assert target.is_file() and target.stat().st_size<=100000;old=target.read_bytes()
            if old!=body:write(history/(target.name+'.previous'),old,0o600)
        if old!=body:write(target,body)
        changed.append({'file':str(target),'sha256':sha(body),'previous_sha256':sha(old) if old is not None else None,'changed':old!=body})
    subprocess.check_call(['/usr/bin/systemd-analyze','verify',*[str(p) for p in targets if p.suffix in ('.service','.timer')]])
    subprocess.check_call(['/usr/bin/systemctl','daemon-reload'])
    report={**summary,'state':'installed_not_enabled','verify_only':False,'files':changed,'at':datetime.now(timezone.utc).isoformat(),
        'ubuntu_attestation_sha256':sha(rawatt),'installer_sha256':sha(Path(__file__).read_bytes()),'core_service_images_changed':False,'historical_raw_backups_deleted':0}
    write(history/'installation-report.json',json.dumps(report,indent=2).encode()+b'\n',0o600)
    print(json.dumps({'state':'installed_not_enabled','report_file':str(history/'installation-report.json'),'timer_enabled':False,'timer_started':False,'image':IMAGE}));return 0
if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:print(json.dumps({'state':'stopped_safely','error_code':type(exc).__name__,'timer_enabled':False,'timer_started':False}));raise SystemExit(2) from None
