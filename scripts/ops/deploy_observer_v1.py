"""Plan/install/stop a separate snapshot collector and private stdio observer.

Run only as the server operator after the exact source pair passed isolated
Ubuntu tests. Default is read-only planning. No SSH, core restart, public port,
credentials, remote tunnel provisioning, or existing business-volume deletion.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid

VERSION = 'lighthouse-observer-deploy-v1'
IMAGE = 'sha256:5c5391b8bf313525f6fc7c43db40ad6832b6c6b431e58a5aeb0bcf04bdc4e981'
APP = Path('/opt/lighthouse-hub')
CODE = Path('/opt/lighthouse-observer-v1')
STATE = Path('/var/lib/lighthouse-observer-v1')
SNAPSHOT = STATE / 'snapshot'
SERVICE = 'lighthouse-observer-collector-v1.service'
TIMER = 'lighthouse-observer-collector-v1.timer'
NAME = 'lighthouse-observer-mcp-v1'
LABEL = 'io.lighthouse.observer.version'
ROLE = 'snapshot-only-v1'
SOURCES = ('observer_snapshot_v1.py', 'observer_mcp_v1.py')
CORE = {'api', 'scheduler', 'tweet-worker', 'video-worker', 'system-worker'}


def need(value, code):
    if not value:
        raise ValueError(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def checked_path(path):
    path = Path(path)
    need(path.is_absolute() and path.resolve() == path and not any(p.is_symlink() for p in (path, *path.parents)), 'UNSAFE_OPERATOR_PATH')
    return path


def root_file(path, *, limit=262144):
    path = checked_path(path)
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_mode & 0o022 == 0 and
         0 < info.st_size <= limit, 'ROOT_CONTROLLED_REGULAR_FILE_REQUIRED')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode), 'ROOT_CONTROLLED_REGULAR_FILE_REQUIRED')
        raw = stream.read(limit+1); after = os.fstat(stream.fileno())
    need(before.st_ino == info.st_ino and len(raw) == before.st_size and
         all(getattr(before, k) == getattr(after, k) for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')),
         'OPERATOR_SOURCE_CHANGED')
    return raw


def run(args, *, allow_missing=False):
    child = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if child.returncode and allow_missing:
        return None
    need(child.returncode == 0, 'LOCAL_OPERATOR_COMMAND_FAILED')
    return child.stdout


def inspect_container(name):
    raw = run(['docker', 'container', 'inspect', name], allow_missing=True)
    return json.loads(raw)[0] if raw is not None else None


def core_fingerprint():
    ids = run(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=lighthouse-hub']).split()
    need(ids, 'CORE_CONTAINERS_REQUIRED')
    result = {}
    for ident in ids:
        item = inspect_container(ident); service = item['Config']['Labels'].get('com.docker.compose.service')
        if service not in CORE:
            continue
        need(service not in result and item['State']['Running'], 'EXACT_RUNNING_CORE_REQUIRED')
        result[service] = {k: item[k] for k in ('Id', 'Image')}
        result[service].update(pid=item['State']['Pid'], started_at=item['State']['StartedAt'])
    need(set(result) == CORE, 'EXACT_FIVE_CORE_SERVICES_REQUIRED')
    return result


def units():
    service = f'''[Unit]
Description=Lighthouse sanitized status snapshot (read-only source)
After=docker.service

[Service]
Type=oneshot
User=root
Group=10001
UMask=0027
ExecStart=/usr/bin/python3 -I {CODE}/observer_snapshot_v1.py --root {APP} --database {APP}/data/hub.sqlite3 --backup-status {APP}/data/cloudbase-backup-state/status.json --output {SNAPSHOT}/status.json
TimeoutStartSec=20
Nice=10
CPUQuota=15%
MemoryMax=128M
TasksMax=16
NoNewPrivileges=true
CapabilityBoundingSet=CAP_DAC_READ_SEARCH
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths={APP} {CODE}
ReadWritePaths={SNAPSHOT}
InaccessiblePaths=-{APP}/data/browser-profiles -/etc/lighthouse-hub
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
'''
    timer = f'''[Unit]
Description=Refresh Lighthouse sanitized status every 30 seconds

[Timer]
OnBootSec=20s
OnUnitActiveSec=30s
AccuracySec=1s
Unit={SERVICE}

[Install]
WantedBy=timers.target
'''
    return {SERVICE: service.encode(), TIMER: timer.encode()}


def container_command():
    return ['docker', 'create', '--name', NAME, '--label', LABEL+'='+ROLE,
            '--interactive', '--restart', 'unless-stopped', '--network', 'none', '--read-only', '--no-healthcheck',
            '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--memory', '268435456', '--cpus', '0.25', '--pids-limit', '64',
            '--log-driver', 'local', '--log-opt', 'max-size=1m', '--log-opt', 'max-file=2',
            '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=16777216,mode=1700,uid=10001,gid=10001',
            '--mount', 'type=bind,src='+str(SNAPSHOT)+',dst=/snapshot,readonly',
            '--mount', 'type=bind,src='+str(CODE/SOURCES[1])+',dst=/observer/'+SOURCES[1]+',readonly',
            '--env', 'HUB_OBSERVER_SNAPSHOT=/snapshot/status.json', '--env', 'PYTHONDONTWRITEBYTECODE=1',
            '--entrypoint', 'python', IMAGE, '-I', '/observer/'+SOURCES[1]]


def verify_container(value, image_environment=None):
    if image_environment is None:
        image_environment = json.loads(run(['docker', 'image', 'inspect', IMAGE]))[0]['Config']['Env']
    expected = sorted([(str(SNAPSHOT), '/snapshot', False), (str(CODE/SOURCES[1]), '/observer/'+SOURCES[1], False)])
    host = value['HostConfig']
    mounts = value['Mounts']
    bind_mounts = [m for m in mounts if m.get('Type') == 'bind']
    need(value['Image'] == IMAGE and value['Config']['User'] == '10001:10001' and
         value['Config']['Labels'].get(LABEL) == ROLE and host['ReadonlyRootfs'] is True and
         host['NetworkMode'] == 'none' and not host.get('PortBindings') and not host.get('PublishAllPorts') and
         not host.get('Privileged') and not host.get('CapAdd') and not host.get('Devices') and
         not host.get('DeviceRequests') and not host.get('Binds') and not host.get('VolumesFrom') and
         host.get('PidMode', '') == '' and host.get('IpcMode') in ('private', '') and
         host.get('UTSMode', '') == '' and host['CapDrop'] == ['ALL'] and
         host['SecurityOpt'] == ['no-new-privileges'] and
         all(m.get('Type') == 'bind' or m.get('Type') == 'tmpfs' and m['Destination'] == '/tmp' for m in mounts) and
         sorted((m['Source'], m['Destination'], m['RW']) for m in bind_mounts) == expected and
         host.get('Memory') == 268435456 and host.get('NanoCpus') == 250000000 and host.get('PidsLimit') == 64 and
         host.get('Tmpfs') == {'/tmp':'rw,nosuid,nodev,noexec,size=16777216,mode=1700,uid=10001,gid=10001'} and
         host.get('LogConfig') == {'Type':'local','Config':{'max-size':'1m','max-file':'2'}} and
         host.get('RestartPolicy', {}).get('Name') == 'unless-stopped' and
         set(value.get('NetworkSettings', {}).get('Networks', {})) <= {'none'} and
         value['Config'].get('Healthcheck', {}).get('Test') == ['NONE'] and
         value['Config']['Entrypoint'] == ['python'] and value['Config']['Cmd'] == ['-I', '/observer/'+SOURCES[1]],
         'OBSERVER_CONTAINER_SCOPE_CHANGED')
    environment = dict(entry.split('=', 1) for entry in value['Config']['Env'])
    expected_environment = dict(entry.split('=', 1) for entry in image_environment)
    expected_environment.update(HUB_OBSERVER_SNAPSHOT='/snapshot/status.json', PYTHONDONTWRITEBYTECODE='1')
    need(environment == expected_environment and len(environment) == len(value['Config']['Env']) and
         not any(re.search(r'TOKEN|SECRET|PASSWORD|COOKIE|API_KEY|ACCESS_KEY', key, re.I) for key in environment),
         'OBSERVER_SECRET_ENVIRONMENT_REFUSED')


def preflight(source_dir, pins):
    need(os.name == 'posix' and os.geteuid() == 0 and __debug__, 'ROOT_LINUX_OPERATOR_REQUIRED')
    checked_path(source_dir); sources = {}
    for name in SOURCES:
        raw = root_file(source_dir/name)
        need(sha(raw) == pins[name], 'REVIEWED_SOURCE_HASH_REQUIRED'); sources[name] = raw
    image = json.loads(run(['docker', 'image', 'inspect', IMAGE]))[0]
    need(image['Id'] == IMAGE, 'QUALIFIED_LOCAL_IMAGE_REQUIRED')
    checked_path(APP/'data/hub.sqlite3')
    need((APP/'data/hub.sqlite3').is_file(), 'LIVE_DATABASE_REQUIRED')
    expected = {**{CODE/name: raw for name, raw in sources.items()},
                **{Path('/etc/systemd/system')/name: raw for name, raw in units().items()}}
    for path, raw in expected.items():
        checked_path(path)
        if path.exists(): need(root_file(path) == raw, 'CHANGED_EXISTING_OBSERVER_FILE_REFUSED')
    for path, mode, gid in ((CODE, 0o755, 0), (STATE, 0o750, 10001), (SNAPSHOT, 0o2750, 10001)):
        checked_path(path)
        if path.exists():
            info=path.stat(); need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_gid == gid and
                                  stat.S_IMODE(info.st_mode) == mode, 'EXACT_OBSERVER_DIRECTORY_REQUIRED')
    old = inspect_container(NAME)
    if old is not None: verify_container(old)
    for name in units():
        extra = run(['systemctl', 'show', name, '--property=DropInPaths', '--value'])
        need(not extra.strip(), 'OBSERVER_UNIT_DROP_INS_REFUSED')
    return {'version': VERSION, 'source_pins': pins, 'source_dir': str(source_dir), 'image': IMAGE,
            'core_before': core_fingerprint(), 'observer_container_id': old['Id'] if old else None,
            'files': {str(path): {'sha256': sha(raw), 'exists': path.exists()} for path,raw in expected.items()},
            'public_ports': [], 'remote_tunnel_configured': False}, expected


def create_file(path, raw, mode):
    if path.exists():
        need(root_file(path) == raw, 'EXISTING_FILE_CHANGED'); return
    with os.fdopen(os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, mode), 'wb') as stream:
        os.fchmod(stream.fileno(), mode); stream.write(raw); stream.flush(); os.fsync(stream.fileno())


def install(plan, files):
    for path, mode, gid in ((CODE, 0o755, 0), (STATE, 0o750, 10001), (SNAPSHOT, 0o2750, 10001)):
        if not path.exists():
            path.mkdir(); os.chown(path, 0, gid); os.chmod(path, mode)
    for path, raw in files.items(): create_file(path, raw, 0o444 if path.parent == CODE else 0o644)
    run(['systemd-analyze', 'verify', '/etc/systemd/system/'+SERVICE, '/etc/systemd/system/'+TIMER])
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'start', SERVICE])
    snapshot = SNAPSHOT/'status.json'; raw = root_file(snapshot, limit=2_097_152); status=json.loads(raw)
    info=snapshot.stat()
    need(info.st_gid == 10001 and stat.S_IMODE(info.st_mode) == 0o640 and
         status['version'] == 'lighthouse-observer-snapshot-v1' and 0 <= time.time()-status['generated_at'] < 120,
         'FRESH_OWNED_SNAPSHOT_REQUIRED')
    item=inspect_container(NAME)
    if item is None:
        ident=run(container_command()).strip(); item=inspect_container(ident)
    verify_container(item)
    if not item['State']['Running']: run(['docker','start',item['Id']])
    probe="import sys;sys.path.insert(0,'/observer');from observer_mcp_v1 import SnapshotReader;assert SnapshotReader('/snapshot/status.json').load()[0]['availability']=='current';print('observer_snapshot_current')"
    need(run(['docker','exec','--user','10001:10001',item['Id'],'python','-I','-c',probe]).strip()=='observer_snapshot_current',
         'MCP_READER_VALIDATION_FAILED')
    run(['systemctl','enable','--now',TIMER])
    need(run(['systemctl','is-active',TIMER]).strip()=='active','OBSERVER_TIMER_NOT_ACTIVE')
    need(core_fingerprint() == plan['core_before'], 'CORE_SERVICE_CHANGED_DURING_OBSERVER_INSTALL')
    receipt={'version':VERSION,'state':'local_observer_ready_remote_tunnel_not_connected','at':time.time(),
             'plan':plan,'observer_container_id':item['Id'],'snapshot_generated_at':status['generated_at'],
             'core_unchanged':True,'public_ports':[],'remote_client_verified':False}
    path=STATE/('deployment-'+uuid.uuid4().hex+'.json');create_file(path,wire(receipt)+b'\n',0o600)
    return {**receipt,'receipt_file':str(path)}


def rollback(plan):
    # Keep code, snapshots and receipts for review; only deactivate our objects.
    item=inspect_container(NAME)
    if item is not None: verify_container(item)
    run(['systemctl','disable','--now',TIMER])
    run(['systemctl','stop',SERVICE])
    if item is not None and item['State']['Running']: run(['docker','stop','--time','10',item['Id']])
    need(core_fingerprint() == plan['core_before'], 'CORE_SERVICE_CHANGED_DURING_OBSERVER_ROLLBACK')
    return {'version':VERSION,'state':'observer_stopped_files_preserved','at':time.time(),'core_unchanged':True,
            'business_files_deleted':0,'remote_tunnel_changed':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-dir',type=Path,required=True)
    parser.add_argument('--collector-sha256',required=True);parser.add_argument('--mcp-sha256',required=True)
    parser.add_argument('--expected-operator-sha256',required=True)
    action=parser.add_mutually_exclusive_group();action.add_argument('--apply',action='store_true');action.add_argument('--rollback',action='store_true')
    parser.add_argument('--plan-sha256');args=parser.parse_args()
    try:
        need(os.name=='posix' and os.geteuid()==0 and __debug__,'ROOT_LINUX_OPERATOR_REQUIRED')
        need(all(re.fullmatch('[a-f0-9]{64}',v) for v in (args.collector_sha256,args.mcp_sha256,args.expected_operator_sha256)), 'EXACT_SHA256_REQUIRED')
        need(sha(root_file(Path(__file__).absolute()))==args.expected_operator_sha256,'OPERATOR_HASH_CHANGED')
        need(bool(args.apply or args.rollback)==bool(args.plan_sha256),'EXPLICIT_REVIEWED_PLAN_REQUIRED')
        pins=dict(zip(SOURCES,(args.collector_sha256,args.mcp_sha256)))
        plan,files=preflight(args.source_dir,pins);checksum=sha(wire(plan))
        if not args.apply and not args.rollback:
            print(json.dumps({'state':'readonly_observer_deployment_plan','plan_sha256':checksum,'plan':plan}));return
        need(checksum==args.plan_sha256,'DEPLOYMENT_PLAN_CHANGED')
        print(json.dumps(rollback(plan) if args.rollback else install(plan,files)))
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as error:
        code=str(error) if isinstance(error,ValueError) and re.fullmatch('[A-Z_]{1,100}',str(error)) else 'OBSERVER_DEPLOYMENT_FAILED'
        print(json.dumps({'state':'stopped','error_code':code,'core_restart_requested':False}),file=sys.stderr)
        raise SystemExit(1) from None


if __name__=='__main__': main()
