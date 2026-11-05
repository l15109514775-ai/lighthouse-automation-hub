"""Root operator/systemd wrapper. No listener, API, arbitrary shell, or deletion."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys
import uuid

ROOT=Path('/opt/lighthouse-hub')
IMAGE='sha256:bc71cffd5e6795d0a779719e0c4e13aae3383c3d1493ac9a3926ac4f2486ec8a'
STATE=ROOT/'data/cloudbase-backup-state'
LABEL='org.lighthouse.backup.sidecar'


def command(*args,**kwargs):return subprocess.check_output(args,timeout=30,**kwargs)


def private_dir(path):
    assert path.is_absolute() and path.resolve().is_relative_to(ROOT) and not path.is_symlink() and not any(p.is_symlink() for p in path.parents)
    if not path.exists():path.mkdir(mode=0o700);os.chown(path,10001,10001)
    assert path.is_dir() and path.stat().st_uid==10001 and path.stat().st_mode&0o077==0


def save_status(value):
    private_dir(STATE)
    temp=STATE/('host-status.'+uuid.uuid4().hex+'.partial');target=STATE/'host-service-status.json'
    assert not target.is_symlink()
    fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o640)
    with os.fdopen(fd,'w') as output:json.dump(value,output);output.write('\n');output.flush();os.fsync(output.fileno())
    os.chown(temp,0,10001);os.replace(temp,target)
    fd=os.open(STATE,os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def main():
    assert os.name=='posix' and os.geteuid()==0
    os.umask(0o077)
    parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True)
    for name in ('prepare-only','run-once','accept','status','record-failure'):mode.add_argument('--'+name,action='store_true')
    args=parser.parse_args()
    for path in (STATE,ROOT/'data/cloudbase-backup-canary-checks'):
        private_dir(path)
    if args.record_failure:
        reason=os.getenv('MONITOR_SERVICE_RESULT','not-provided')
        if reason not in ('exit-code','signal','timeout','watchdog','core-dump','resources','not-provided'):reason='unknown'
        value={'version':'cloudbase-backup-host-service-v1d','at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
               'state':'failed','service_result':reason,'automatic_mcp_notification_integrated':False}
        save_status(value);print(json.dumps(value));return 0
    assert command('docker','image','inspect','-f','{{.Id}}',IMAGE,text=True).strip()==IMAGE
    for path in (ROOT/'data',ROOT/'media',ROOT/'logs',ROOT/'config',ROOT/'backups'):
        assert path.is_dir() and not path.is_symlink() and not any(p.is_symlink() for p in path.parents)
    identifier=str(uuid.uuid4());cidfile=STATE/('sidecar-'+identifier+'.cid');name='lighthouse-cloudbase-backup-'+identifier
    offline=args.prepare_only or args.status
    docker=['docker','run','--pull=never','--rm','--init','--name',name,'--label',LABEL+'=v1d','--cidfile',str(cidfile),
        '--user','10001:10001','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
        '--security-opt','seccomp='+str(ROOT/'config/browser-seccomp.json'),
        '--memory','512m','--memory-swap','512m','--cpus','1','--pids-limit','96',
        '--tmpfs','/tmp:rw,size=64m,mode=1777',
        '--tmpfs',str(ROOT/'data/browser-profiles')+':rw,size=1024,mode=0700']
    if offline:docker+=['--network','none']
    for folder,readonly in (('data',True),('media',True),('logs',True),('config',True),('backups',False),
                            ('data/cloudbase-backup-state',False),('data/cloudbase-backup-canary-checks',False)):
        path=ROOT/folder;docker+=['--mount','type=bind,src='+str(path)+',dst='+str(path)+(',readonly' if readonly else '')]
    if not offline:
        secret=ROOT/'secrets/cloudbase-backup.json'
        assert secret.is_file() and not secret.is_symlink() and secret.stat().st_mode&0o007==0
        docker+=['--mount','type=bind,src='+str(secret)+',dst=/run/secrets/cloudbase-backup.json,readonly']
    if args.accept:
        helper=Path('/usr/local/lib/lighthouse-backup-ops/accept_cloudbase_backup_capture_v1.py')
        assert helper.is_file() and helper.stat().st_size<=20000
        for path in (helper,*helper.parents):
            assert not path.is_symlink() and path.stat().st_uid==0 and path.stat().st_mode&0o022==0
        assert hashlib.sha256(helper.read_bytes()).hexdigest()=='f3a9a0543a236c5d2e94a209d6e2074c7b3828fce6641ce4fa7d780ac5f17efb'
        docker+=['--mount','type=bind,src='+str(helper)+',dst=/run/accept-backup.py,readonly',
                 IMAGE,'python','/run/accept-backup.py']
    else:
        selected='--prepare-only' if args.prepare_only else '--status' if args.status else '--run-once'
        docker+=[IMAGE,'python','scripts/cloudbase-backup.py','--root',str(ROOT),selected]
    pin=None
    try:
        if not args.status:
            def as_app():os.setgroups([]);os.setgid(10001);os.setuid(10001)
            code="""from pathlib import Path
import sqlite3,sys,os
assert os.getuid()==10001
db=sqlite3.connect(Path(sys.argv[1]).as_uri()+'?mode=ro',uri=True,timeout=15)
db.execute('PRAGMA query_only=ON');db.execute('SELECT COUNT(*) FROM jobs').fetchone()
print('sqlite-readonly-pin-ready',flush=True)
sys.stdin.buffer.read();db.close()
"""
            pin=subprocess.Popen([sys.executable,'-u','-c',code,str(ROOT/'data/hub.sqlite3')],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,preexec_fn=as_app,
                env={'PATH':'/usr/bin:/bin','HOME':'/tmp','PYTHONNOUSERSITE':'1'})
            selector=selectors.DefaultSelector();selector.register(pin.stdout,selectors.EVENT_READ)
            assert selector.select(20) and pin.stdout.readline()==b'sqlite-readonly-pin-ready\n';selector.close()
        result=subprocess.run(docker,stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=900)
        try:observed=json.loads(result.stdout)
        except (ValueError,TypeError):observed={'state':'failed','error_code':'SIDECAR_OUTPUT_NOT_JSON','stdout_length':len(result.stdout)}
        status={'version':'cloudbase-backup-host-service-v1d','at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'mode':'prepare-only' if args.prepare_only else 'accept' if args.accept else 'status' if args.status else 'run-once',
            'exit_code':result.returncode,'image':IMAGE,'uid':10001,'offline':offline,
            'credentials_mounted':not offline,'core_service_images_changed':False,
            'automatic_mcp_notification_integrated':False,'result':observed}
        save_status(status);print(json.dumps(status));return result.returncode
    finally:
        if pin:
            if pin.stdin and not pin.stdin.closed:pin.stdin.close()
            try:pin.wait(timeout=15)
            except subprocess.TimeoutExpired:pin.terminate();pin.wait(timeout=5)
        if cidfile.exists():
            cid=cidfile.read_text().strip()
            try:items=json.loads(command('docker','inspect',cid))
            except subprocess.CalledProcessError:items=[]
            if items:
                item=items[0];assert item['Id']==cid and item['Config']['Labels'][LABEL]=='v1d'
                if item['State']['Running']:command('docker','stop','-t','20',cid)


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        value={'version':'cloudbase-backup-host-service-v1d','at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'state':'failed','error_code':type(exc).__name__,'automatic_mcp_notification_integrated':False}
        try:save_status(value)
        except Exception:pass
        print(json.dumps(value));raise SystemExit(2) from None
