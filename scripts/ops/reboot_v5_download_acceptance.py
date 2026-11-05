"""One current-V5 download restart acceptance. Default snapshot; explicit --reboot.

Host root only. No container start, queue reset, platform upload or publication.
The exact running download and completed publication identities are retained.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time
import urllib.request

ROOT = Path('/opt/lighthouse-hub')
JOB = '9801f01e-efb6-42ba-a233-c4124c3a7500'
SOURCE = 'YsSXSIYtCoE'
IMAGE = 'sha256:b6dd672929e46c1699c0b4281c7dbe047b099019bb9ae9ce832ce79a850eb94c'
ROLES = ('api', 'scheduler', 'tweet-worker', 'video-worker', 'system-worker')
BEATS = ('worker:video-main-v5', 'worker:video-producer-v5',
         'worker:video-part-moderation-v5', 'worker:video-retention-v5')
CAPSULE = r'''
import hashlib,json,sqlite3,time
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.replay_parts_v5 import ReplayPartVideoWeb,ReplayPartGroupsV5
s=Settings.load();store=Store(s.db_path)
policy=ReplayPartVideoWeb(s,store).policy()
assert policy['source_scope']=='all_eligible_ended_since_operating_cutoff' and policy['automation_enabled'] is True
grant=ReplayPartGroupsV5(s,store).require_launch(policy)
c=sqlite3.connect(s.db_path.as_uri()+'?mode=ro',uri=True)
c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
def rows(q,args=()):return [dict(x) for x in c.execute(q,args)]
def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':')).encode()).hexdigest()
j=rows('SELECT * FROM jobs WHERE job_id=?',('9801f01e-efb6-42ba-a233-c4124c3a7500',))[0]
payload=json.loads(j.pop('payload'));result=json.loads(j.pop('result'));state=result.get('youtube_download_v4',{})
j.update(payload_sha256=digest(payload),download_state=state.get('state'),binding_sha256=digest(state.get('binding')))
pubs=rows('SELECT * FROM publications ORDER BY idempotency_key')
print(json.dumps({'at':time.time(),'job':j,'max_attempts':s.config['max_attempts'],
 'formal':{'source_scope':policy['source_scope'],'automation_enabled':policy['automation_enabled'],
           'policy_sha256':policy['_sha256'],'launch_valid':True,'acceptance_group':grant['acceptance_parent_job_id']},
 'active':rows("SELECT job_id,job_type,queue,state,stage FROM jobs WHERE state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL"),
 'video_uncertain':rows("SELECT job_id,state FROM jobs WHERE queue='video' AND state IN ('publish_unknown','awaiting_moderation')"),
 'publications':pubs,'pipelines':rows('SELECT name,paused FROM pipelines'),
 'heartbeats':dict(c.execute('SELECT name,at FROM heartbeats'))}))
c.close()
'''


def run(args, *, data=None, timeout=40):
    p = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError('COMMAND_FAILED_' + args[0])
    return p.stdout


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def save(file, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode() + b'\n'
    with os.fdopen(os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as f:
        f.write(raw); f.flush(); os.fsync(f.fileno())
    return hashlib.sha256(raw).hexdigest()


def containers():
    answer = {}
    for role in ROLES:
        cid = run(['docker', 'compose', '-f', str(ROOT/'compose.yaml'), 'ps', '-q', role]).decode().strip()
        x = json.loads(run(['docker', 'inspect', cid]))[0]
        assert x['State']['Running'] and not x['State']['OOMKilled']
        answer[role] = {'cid': cid, 'image': x['Image'], 'hostname': x['Config']['Hostname'],
            'user': x['Config']['User'], 'started_at': x['State']['StartedAt'], 'pid': x['State']['Pid'],
            'restart': x['HostConfig']['RestartPolicy'],
            'protection_sha256': digest({k:x['HostConfig'].get(k) for k in (
                'ReadonlyRootfs','Init','SecurityOpt','Memory','PidsLimit','ShmSize','Binds','Tmpfs','PortBindings')}),
            'environment_sha256': digest(sorted(x['Config']['Env']))}
        assert answer[role]['restart']['Name'] in ('unless-stopped', 'always')
    assert answer['video-worker']['image'] == IMAGE
    return answer


def partials():
    area = ROOT/'media'/JOB/'youtube-whole-v4'
    answer = []
    for p in sorted(area.glob('stream-*.part')):
        assert p.resolve() == p and not any(q.is_symlink() for q in (p,*p.parents))
        with os.fdopen(os.open(p, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as f:
            s = os.fstat(f.fileno()); assert stat.S_ISREG(s.st_mode)
            length = min(s.st_size, 16 * 1024 * 1024)
            prefix = f.read(length); assert len(prefix) == length
        if length == 16 * 1024 * 1024:
            answer.append({'file':str(p),'bytes':s.st_size,'prefix_bytes':length,
                           'prefix_sha256':hashlib.sha256(prefix).hexdigest()})
    progress = json.loads((area/'progress.json').read_bytes())
    assert progress['status'] == 'downloading' and time.time()-progress['checked_at'] < 120
    return answer, {k:progress.get(k) for k in ('checked_at','status','format_id','downloaded_bytes','total_bytes','http')}


def capture():
    cs = containers()
    db = json.loads(run(['docker','exec','-i','--user','10001:10001',cs['video-worker']['cid'],'python','-'],data=CAPSULE.encode()))
    files, progress = partials()
    with urllib.request.urlopen('http://127.0.0.1:8080/ready', timeout=10) as r:
        ready=json.loads(r.read(20000)); assert r.status == 200 and ready['status'] == 'ready'
    return {'version':'current-v5-download-reboot-v1','at':time.time(),
        'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'containers':cs,'database':db,'partials':files,'progress':progress,'ready':ready,
        'compose_sha256':hashlib.sha256((ROOT/'compose.yaml').read_bytes()).hexdigest(),
        'grant_sha256':hashlib.sha256(Path('/etc/lighthouse-hub/replay-parts-workflow-launch-v5.json').read_bytes()).hexdigest()}


def before_guard(value):
    d=value['database']; j=d['job']
    assert j['source_id']==SOURCE and j['state']=='processing' and j['stage']=='youtube_whole_acquiring'
    assert j['download_state']=='acquiring' and j['attempts']<d['max_attempts']
    assert j['lease_owner'] and j['lease_until']>time.time() and not j['external_publish_id']
    assert len(d['active'])==1 and d['active'][0]['job_id']==JOB and not d['video_uncertain']
    assert not any(p['source_platform']=='youtube' and p['state'] in ('unknown','dispatching','awaiting_moderation') for p in d['publications'])
    assert not any(p['paused'] for p in d['pipelines'])
    assert any(p['prefix_bytes']==16*1024*1024 for p in value['partials'])
    assert value['progress']['total_bytes']-value['progress']['downloaded_bytes']>512*1024*1024
    assert all(time.time()-d['heartbeats'][name]<150 for name in BEATS)


def main():
    p=argparse.ArgumentParser();p.add_argument('--folder',type=Path,required=True)
    p.add_argument('--reboot',action='store_true');p.add_argument('--after',action='store_true');a=p.parse_args()
    assert os.geteuid()==0 and not(a.reboot and a.after)
    assert a.folder.parent==Path('/var/lib/lighthouse-backup-ops/current-v5-reboot')
    assert a.folder.resolve()==a.folder and not any(x.is_symlink() for x in (a.folder,*a.folder.parents))
    if not a.folder.exists():a.folder.mkdir(mode=0o700)
    assert a.folder.stat().st_uid==0 and a.folder.stat().st_mode&0o077==0
    value=capture()
    if a.after:
        old=json.loads((a.folder/'before.json').read_bytes())
        assert value['boot_id']!=old['boot_id']
        assert value['compose_sha256']==old['compose_sha256'] and value['grant_sha256']==old['grant_sha256']
        for role,c in value['containers'].items():
            prior=old['containers'][role]
            assert all(c[k]==prior[k] for k in ('cid','image','hostname','user','restart','protection_sha256','environment_sha256'))
            assert c['started_at']!=prior['started_at']
        b=old['database'];d=value['database'];j=d['job'];previous=b['job']
        assert all(j[k]==previous[k] for k in ('job_id','source_id','created_at','payload_sha256','binding_sha256'))
        assert j['attempts']==previous['attempts']+1 and j['state']=='processing'
        # Queue.claim retains the recovery code until the handler finishes.
        assert j['error_code'] in (None, 'PROCESS_INTERRUPTED')
        assert j['lease_owner']!=previous['lease_owner'] and j['lease_until']>time.time()
        for pub in b['publications']:
            assert pub in d['publications']
        assert all(d['heartbeats'][name]>old['at'] for name in BEATS)
        current={x['file']:x for x in value['partials']}
        for f in old['partials']:
            q=current[f['file']]
            assert q['prefix_bytes']==f['prefix_bytes'] and q['prefix_sha256']==f['prefix_sha256'] and q['bytes']>f['bytes']
        value['acceptance']='same_download_resumed_after_normal_host_reboot'
        value['scope']='download continuation and automatic services; no interrupted-upload claim'
        sha=save(a.folder/'after.json',value)
    else:
        before_guard(value)
        sha=save(a.folder/'before.json',value)
        if a.reboot:
            # SQLite online backup, not a live-WAL main-file copy. No secrets are exported.
            import sqlite3
            source=sqlite3.connect((ROOT/'data/hub.sqlite3').as_uri()+'?mode=ro',uri=True)
            target=sqlite3.connect(a.folder/'before.sqlite3')
            try:
                target.execute('PRAGMA journal_mode=DELETE')
                source.backup(target);assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            finally:
                target.close();source.close()
            os.chmod(a.folder/'before.sqlite3',0o600)
            with open(a.folder/'before.sqlite3','rb') as backup:os.fsync(backup.fileno())
            fd=os.open(a.folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(fd)
            finally:os.close(fd)
            fresh=capture();before_guard(fresh)
            assert fresh['containers']==value['containers'] and fresh['database']['job']['attempts']==value['database']['job']['attempts']
            run(['systemd-run','--unit=lighthouse-current-v5-download-reboot','--on-active=5s','/usr/bin/systemctl','reboot'])
    print(json.dumps({'folder':str(a.folder),'after':a.after,'reboot_armed':a.reboot,'evidence_sha256':sha}))


if __name__=='__main__':main()
