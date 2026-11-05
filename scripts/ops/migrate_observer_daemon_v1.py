"""Migrate only the observer collector from a timer to its read-only daemon.

Default is a read-only plan. Existing oneshot/timer files stay unchanged.
No database writes, Docker mutations, reader/tunnel restarts or secret access.
"""
from pathlib import Path
import argparse
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
import uuid

VERSION = 'lighthouse-observer-daemon-migration-v1'
CODE = '/opt/lighthouse-observer-v1'
STATE = '/var/lib/lighthouse-observer-v1'
SNAPSHOT = STATE + '/snapshot/status.json'
DAEMON_FILE = 'observer_collector_daemon_v1.py'
SERVICE = 'lighthouse-observer-daemon-v1.service'
OLD_SERVICE = 'lighthouse-observer-collector-v1.service'
OLD_TIMER = 'lighthouse-observer-collector-v1.timer'
COLLECTOR_SHA = 'be357d4f3c5a41dd92ac9ce7495e46e4fb22149a4ca9c56216dea4305dbf96c5'
READER_SHAS = {
    '8072b7a0ceaa733938d09ea4dfac7eeb8263a67ae88381394e8a846343018128',
    '4c301d2afdecf6a557ac8cca9f5879d4d57a83104bcc362eb0885d611386f580',
}
OLD_UNIT_SHA = {
    OLD_SERVICE:'4d860c3226f6de5464f6134d34860e0baab722cfbe78e9505cd97185317abca7',
    OLD_TIMER:'87ddc2b6a9bbe0f2d1e6c279e1dcf79975c1d7d771928c95a35a9a4ae1e92a6d',
}
CORE = {'api','scheduler','tweet-worker','video-worker','system-worker'}
OBSERVERS = {'lighthouse-observer-mcp-v1','lighthouse-observer-tunnel-v1'}


def need(value, code):
    if not value: raise ValueError(code)


def sha(raw): return hashlib.sha256(raw).hexdigest()
def wire(value): return json.dumps(value,sort_keys=True,separators=(',',':')).encode()
def unit_path(name): return Path('/etc/systemd/system')/name


def safe_path(value):
    path=Path(value)
    need(path.is_absolute() and path.resolve()==path and not any(p.is_symlink() for p in (path,*path.parents)), 'UNSAFE_MIGRATION_PATH')
    return path


def root_file(value, *, limit=262144):
    path=safe_path(value);info=path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022 and 0<info.st_size<=limit, 'ROOT_FILE_REQUIRED')
    with os.fdopen(os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK),'rb') as stream:
        before=os.fstat(stream.fileno());need(stat.S_ISREG(before.st_mode),'ROOT_FILE_REQUIRED')
        raw=stream.read(limit+1);after=os.fstat(stream.fileno())
    need(len(raw)==info.st_size and all(getattr(info,k)==getattr(before,k)==getattr(after,k)
         for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')), 'MIGRATION_FILE_CHANGED')
    return raw


def run(args):
    result=subprocess.run(args,capture_output=True,text=True,timeout=40)
    need(result.returncode==0,'MIGRATION_COMMAND_FAILED')
    return result.stdout


def unit_state(name):
    keys=('FragmentPath','DropInPaths','ActiveState','UnitFileState','MainPID')
    raw=run(['systemctl','show',name,*['--property='+key for key in keys]])
    return dict(line.split('=',1) for line in raw.splitlines() if '=' in line)


def container_fingerprint():
    ids=run(['docker','ps','-aq','--filter','label=com.docker.compose.project=lighthouse-hub']).split()
    need(ids,'CORE_CONTAINERS_REQUIRED')
    items=json.loads(run(['docker','inspect',*ids,*sorted(OBSERVERS)]));result={};found=set()
    for item in items:
        name=item['Name'].lstrip('/');service=item['Config']['Labels'].get('com.docker.compose.service')
        if service not in CORE and name not in OBSERVERS:continue
        key='core:'+service if service in CORE else name
        need(key not in result and item['State']['Running'],'EXACT_RUNNING_CONTAINERS_REQUIRED')
        result[key]={'id':item['Id'],'image':item['Image'],'pid':item['State']['Pid'],'started_at':item['State']['StartedAt']}
        if service in CORE:found.add(service)
    need(found==CORE and set(result)=={'core:'+s for s in CORE}|OBSERVERS,'EXACT_SEVEN_CONTAINERS_REQUIRED')
    return result


def service_bytes():
    return f'''[Unit]
Description=Lighthouse read-only snapshot collector with idle SQLite anchor
After=docker.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=root
Group=10001
UMask=0027
ExecStart=/usr/bin/python3 -I {CODE}/{DAEMON_FILE} --root /opt/lighthouse-hub --database /opt/lighthouse-hub/data/hub.sqlite3 --backup-status /opt/lighthouse-hub/data/cloudbase-backup-state/status.json --output {SNAPSHOT}
Restart=on-failure
RestartSec=5
TimeoutStopSec=10
Nice=10
CPUQuota=15%
MemoryMax=128M
TasksMax=16
NoNewPrivileges=true
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=/opt/lighthouse-hub {CODE}
ReadWritePaths={STATE}/snapshot
InaccessiblePaths=-/opt/lighthouse-hub/data/browser-profiles -/etc/lighthouse-hub
PrivateNetwork=true
RestrictAddressFamilies=AF_UNIX
IPAddressDeny=any
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
ProtectClock=true
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
Environment=PYTHONDONTWRITEBYTECODE=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
'''.encode()


def check_host():
    need(os.name=='posix' and os.geteuid()==0 and __debug__,'ROOT_LINUX_OPERATOR_REQUIRED')
    import grp
    need(grp.getgrgid(10001).gr_name=='lighthouse-observer','OBSERVER_GROUP_REQUIRED')


def preflight(source, daemon_sha):
    check_host()
    for folder,mode,gid in ((CODE,0o755,0),(STATE,0o750,10001),(STATE+'/snapshot',0o2750,10001)):
        info=safe_path(folder).stat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid==0 and info.st_gid==gid and stat.S_IMODE(info.st_mode)==mode,'EXACT_OBSERVER_DIRECTORY_REQUIRED')
    raw=root_file(source);need(sha(raw)==daemon_sha,'DAEMON_SOURCE_HASH_MISMATCH')
    reader_sha=sha(root_file(Path(CODE)/'observer_mcp_v1.py'))
    need(sha(root_file(Path(CODE)/'observer_snapshot_v1.py'))==COLLECTOR_SHA and
         reader_sha in READER_SHAS,'EXISTING_OBSERVER_SOURCE_CHANGED')
    for name,expected in OLD_UNIT_SHA.items():
        need(sha(root_file(unit_path(name)))==expected,'OLD_UNIT_HASH_MISMATCH')
        state=unit_state(name)
        need(state.get('FragmentPath')==str(unit_path(name)) and not state.get('DropInPaths'),'OLD_UNIT_OVERRIDE_REFUSED')
    files={Path(CODE)/DAEMON_FILE:raw,unit_path(SERVICE):service_bytes()}
    for path,content in files.items():
        safe_path(path)
        if path.exists():need(root_file(path)==content,'EXISTING_DAEMON_FILE_CHANGED')
    state=unit_state(SERVICE)
    need(not state.get('DropInPaths') and state.get('FragmentPath','') in ('',str(unit_path(SERVICE))), 'DAEMON_UNIT_OVERRIDE_REFUSED')
    timer=unit_state(OLD_TIMER)
    need(timer.get('UnitFileState') in ('enabled','disabled') and timer.get('ActiveState') in ('active','inactive','failed'),'OLD_TIMER_STATE_UNKNOWN')
    plan={'version':VERSION,'source':str(source),'daemon_sha256':daemon_sha,'collector_sha256':COLLECTOR_SHA,'reader_sha256':reader_sha,
          'old_unit_sha256':OLD_UNIT_SHA,'new_unit_sha256':sha(service_bytes()),'containers_before':container_fingerprint(),
          'old_timer':{k:timer.get(k) for k in ('ActiveState','UnitFileState')},
          'daemon':{k:state.get(k) for k in ('ActiveState','UnitFileState','MainPID')},
          'files':{str(p):{'exists':p.exists(),'sha256':sha(content)} for p,content in files.items()},
          'business_mutations':[],'reader_tunnel_restart':False}
    return plan,files


def create_file(path,raw,mode):
    if path.exists():need(root_file(path)==raw,'EXISTING_FILE_CHANGED');return
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode),'wb') as stream:
        os.fchmod(stream.fileno(),mode);stream.write(raw);stream.flush();os.fsync(stream.fileno())


def fresh_generation(after):
    try:
        raw=root_file(SNAPSHOT,limit=2_097_152);info=Path(SNAPSHOT).stat();snapshot=json.loads(raw)
        need(isinstance(snapshot,dict),'SNAPSHOT_CONTRACT_CHANGED');at=snapshot.get('generated_at')
        need(info.st_gid==10001 and stat.S_IMODE(info.st_mode)==0o640,'SNAPSHOT_PERMISSIONS_CHANGED')
        need(snapshot.get('version')=='lighthouse-observer-snapshot-v1' and snapshot.get('read_only') is True and
             snapshot.get('provider_requests')==0,'SNAPSHOT_CONTRACT_CHANGED')
        need(isinstance(at,(int,float)) and not isinstance(at,bool) and math.isfinite(at) and
             at>after and -5<=time.time()-at<=90,'SNAPSHOT_NOT_NEW')
        return at
    except (OSError,ValueError,TypeError):return None


def await_updates(after, *, timeout=75):
    deadline=time.monotonic()+timeout;generations=[];pid=None
    while time.monotonic()<deadline:
        state=unit_state(SERVICE)
        need(state.get('ActiveState')=='active' and state.get('MainPID') not in ('0','',None),'DAEMON_NOT_RUNNING')
        if pid is None:pid=state['MainPID']
        need(pid==state['MainPID'],'DAEMON_RESTARTED_DURING_ACCEPTANCE')
        at=fresh_generation(after)
        if at is not None and (not generations or at>generations[-1]):generations.append(at)
        if len(generations)>=2:return generations
        time.sleep(1)
    raise ValueError('TWO_NEW_SNAPSHOTS_REQUIRED')


def restore_timer():
    run(['systemctl','disable','--now',SERVICE])
    run(['systemctl','enable','--now',OLD_TIMER])
    state=unit_state(OLD_TIMER)
    need(state.get('ActiveState')=='active' and state.get('UnitFileState')=='enabled','OLD_TIMER_RESTORE_FAILED')


def apply(plan,files):
    for path,content in files.items():create_file(path,content,0o444 if path.parent==Path(CODE) else 0o644)
    run(['systemd-analyze','verify',str(unit_path(SERVICE))])
    run(['systemctl','daemon-reload'])
    mutated=False
    try:
        # Only the collector changes. The two reader/tunnel containers stay up.
        mutated=True;run(['systemctl','disable','--now',OLD_TIMER])
        run(['systemctl','stop',OLD_SERVICE]);after=time.time()
        run(['systemctl','enable','--now',SERVICE])
        updates=await_updates(after)
        need(container_fingerprint()==plan['containers_before'],'UNRELATED_CONTAINER_CHANGED')
        state=unit_state(OLD_TIMER)
        need(state.get('ActiveState')=='inactive' and state.get('UnitFileState')=='disabled','OLD_TIMER_NOT_DISABLED')
        return {'state':'daemon_active_two_snapshots_verified','generations':updates,'containers_unchanged':True,'old_units_preserved':True}
    except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as error:
        restored=False
        if mutated:
            try:restore_timer();restored=True
            except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError):pass
        code=str(error) if isinstance(error,ValueError) and re.fullmatch('[A-Z_]{1,100}',str(error)) else 'DAEMON_MIGRATION_FAILED'
        return {'state':'migration_failed','error_code':code,'old_timer_restored':restored,'rollback_health_asserted':False}


def rollback(plan):
    restore_timer()
    need(container_fingerprint()==plan['containers_before'],'UNRELATED_CONTAINER_CHANGED')
    return {'state':'old_timer_restored_health_not_asserted','containers_unchanged':True,'files_deleted':0,
            'known_old_wal_limitation_remains':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-daemon',type=Path,required=True);parser.add_argument('--daemon-sha256',required=True)
    parser.add_argument('--expected-operator-sha256',required=True);parser.add_argument('--plan-sha256')
    action=parser.add_mutually_exclusive_group();action.add_argument('--apply',action='store_true');action.add_argument('--rollback',action='store_true')
    args=parser.parse_args()
    try:
        need(os.name=='posix' and os.geteuid()==0 and __debug__,'ROOT_LINUX_OPERATOR_REQUIRED')
        need(all(re.fullmatch('[a-f0-9]{64}',value) for value in (args.daemon_sha256,args.expected_operator_sha256)),'EXACT_SHA256_REQUIRED')
        need(sha(root_file(Path(__file__).absolute()))==args.expected_operator_sha256,'OPERATOR_HASH_CHANGED')
        need(bool(args.apply or args.rollback)==bool(args.plan_sha256),'REVIEWED_PLAN_REQUIRED')
        plan,files=preflight(args.source_daemon,args.daemon_sha256);checksum=sha(wire(plan))
        if not args.apply and not args.rollback:
            print(json.dumps({'state':'readonly_migration_plan','plan_sha256':checksum,'plan':plan}));return
        need(checksum==args.plan_sha256,'MIGRATION_PLAN_CHANGED')
        result=rollback(plan) if args.rollback else apply(plan,files)
        result.update(version=VERSION,at=time.time(),plan_sha256=checksum)
        receipt=Path(STATE)/('daemon-migration-'+uuid.uuid4().hex+'.json');create_file(receipt,wire(result)+b'\n',0o600)
        print(json.dumps({**result,'receipt_file':str(receipt)}))
        if result['state']=='migration_failed':raise SystemExit(1)
    except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as error:
        code=str(error) if isinstance(error,ValueError) and re.fullmatch('[A-Z_]{1,100}',str(error)) else 'MIGRATION_OPERATOR_FAILED'
        print(json.dumps({'state':'stopped','error_code':code,'business_restart_requested':False}),file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__':main()
