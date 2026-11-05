"""Read a genuinely completed pilot and prepare protected v3 candidate bytes.

Default plan writes no permanent candidate, changes no live DB/config and
starts no service. --issue creates a new root-only-editable candidate directory.
"""
from datetime import datetime,timezone
from pathlib import Path
import argparse,hashlib,json,os,re,stat,tempfile,tomllib,types,uuid

INSTALLER_SHA='7e1548ce72b662816ca052ab4bc057bd4de5fe8930842d71d1f9c38dc0c451c2'
PILOT='594aed06-3aa1-45f4-90f0-1db49a602395'
DESTINATION=Path('/etc/lighthouse-hub/natural-candidates-v3')

def load(file):
    file=Path(file);assert file.is_absolute() and file.resolve()==file and not file.is_symlink()
    raw=file.read_bytes();assert hashlib.sha256(raw).hexdigest()==INSTALLER_SHA
    module=types.ModuleType('pinned_natural_installer');module.__file__=str(file);exec(compile(raw,str(file),'exec'),module.__dict__);return module

def authorization(raw):
    value=json.loads(raw)
    assert set(value)=={'actual_owner_workflow_response','owner_response_reference'}
    assert all(isinstance(x,str) and 0<len(x.strip())<=2048 for x in value.values())
    return value

def activate_at(raw,instant):
    """Only the top-level instant is replaced; all other TOML facts remain."""
    parsed=tomllib.loads(raw.decode('utf-8-sig'));lines=raw.decode('utf-8-sig').splitlines(keepends=True)
    bound=next((i for i,line in enumerate(lines) if line.lstrip().startswith('[')),len(lines))
    matches=[i for i,line in enumerate(lines[:bound]) if re.match(r'^\s*activated_at\s*=',line)];assert len(matches)==1
    i=matches[0];newline='\r\n' if lines[i].endswith('\r\n') else '\n' if lines[i].endswith('\n') else ''
    body=lines[i].rstrip('\r\n');pattern=r'''^(\s*activated_at\s*=\s*)(?:"[^"\r\n]*"|'[^'\r\n]*')(\s*(?:#.*)?)$'''
    match=re.fullmatch(pattern,body);assert match
    lines[i]=match[1]+'"'+instant+'"'+match[2]+newline
    result=(b'\xef\xbb\xbf' if raw.startswith(b'\xef\xbb\xbf') else b'')+''.join(lines).encode()
    expected={**parsed,'activated_at':instant};assert tomllib.loads(result.decode('utf-8-sig'))==expected
    return result

ISSUE=r'''
from pathlib import Path
import hashlib,json,os,socket,threading
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.worker import Worker
from services.hub.replay_moderation_common import QueueView
def refuse(*args,**kwargs):raise RuntimeError('CANDIDATE_ISSUER_CONSUMPTION_OR_NETWORK_FORBIDDEN')
socket.socket.connect=refuse;Store.claim=Store.recover=Store.enqueue=Store.finish=refuse
Worker.once=Worker.run=QueueView.claim=QueueView.recover=threading.Thread.start=refuse
from services.hub.replay_natural_video_v3 import NaturalReplayVideoWeb
from services.hub.replay_natural_launch_v3 import issue_launch
from services.hub.youtube_download_v3 import YouTubeNaturalPrepare
assert os.getuid()==os.getgid()==10001
s=Settings.load();db=Store('/run/issuer-db/hub.sqlite3');db.init()
auth=json.loads(Path('/run/night-authorization.json').read_bytes())
service=NaturalReplayVideoWeb(s,db);policy=service.policy();download=YouTubeNaturalPrepare(s,db).policy()
assert download['activated_at']==policy['activated_at'] and download['daily_http_requests']==6000 and download['monthly_http_requests']==150000
pilot='594aed06-3aa1-45f4-90f0-1db49a602395'
before=db.query('SELECT COUNT(*) AS n FROM jobs')[0]['n'];prior=db.get(pilot)
value=issue_launch(service,pilot,actual_owner_launch_response=auth['actual_owner_workflow_response'],owner_response_reference=auth['owner_response_reference'])
assert value['files_written'] is value['publication_authorized'] is False
assert db.get(pilot)==prior and db.query('SELECT COUNT(*) AS n FROM jobs')[0]['n']==before
refs={}
for name,key in (('workflow-launch.json','grant_bytes'),('pilot.json','snapshot_bytes')):
 file=Path('/owned')/name;raw=value[key]
 fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
 with os.fdopen(fd,'wb') as out:out.write(raw);out.flush();os.fsync(out.fileno())
 refs[name]={'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
fd=os.open('/owned',os.O_DIRECTORY)
try:os.fsync(fd)
finally:os.close(fd)
print(json.dumps({'version':'natural-candidate-issuer-result-v3','pilot_job_id':pilot,'files':refs,'policy_sha256':policy['_sha256'],
 'human_review':False,'per_artifact_owner_acceptance':False,'platform_publication_performed':False,'live_db_modified':False,'external_calls':0}))
'''

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for kind in ('natural','download','integration'):
        parser.add_argument('--'+kind+'-attestation',required=True);parser.add_argument('--'+kind+'-sha256',required=True)
    parser.add_argument('--installer-file',required=True);parser.add_argument('--deployer-file',required=True)
    parser.add_argument('--input-directory',required=True);parser.add_argument('--issue',action='store_true')
    args=parser.parse_args();assert os.name=='posix' and os.geteuid()==0
    m=load(args.installer_file);d=m.helper(args.deployer_file);values={};references={}
    for kind in m.SPECS:values[kind],references[kind]=m.gate(Path(getattr(args,kind+'_attestation')),getattr(args,kind+'_sha256'),kind,d)
    files=m.merged_sources(values['natural'],values['download'],values['integration']);overlay=Path(args.integration_attestation).parent/'overlay'
    code={name:entry for name,entry in files.items() if d.runtime_source(name)}
    source=d.safe(Path(args.input_directory));m.protected(source);assert source.is_dir()
    inputs={name:source/name for name in m.POLICIES};inputs['authorization']=source/'night-authorization.json';original={}
    for name,file in inputs.items():
        m.protected(file);info=file.stat();assert info.st_gid==10001 and stat.S_IMODE(info.st_mode)==0o640
        original[name]=d.read(file,16384)
    auth=authorization(original['authorization']);instant=datetime.now(timezone.utc).isoformat()
    policies={name:activate_at(original[name],instant) for name in m.POLICIES}
    hub=m.candidate_hub(d.read(d.ROOT/'config/hub.toml'))
    command=['/usr/bin/docker','run','--rm','--pull=never','--network=none','--read-only','--user','10001:10001','--cap-drop=ALL','--security-opt','no-new-privileges:true',
        '--init','--pids-limit=256','--memory=1400m','--shm-size=256m','--tmpfs','/tmp:rw,size=192m,mode=1777','-e','HUB_ROOT='+str(d.ROOT),
        '-e','HUB_CONFIG='+str(d.ROOT/'config/hub.toml'),'-e','HOME=/tmp/candidate-issuer','-e','PYTHONDONTWRITEBYTECODE=1']
    with tempfile.TemporaryDirectory(prefix='lighthouse-natural-issuer-',dir='/run') as directory:
        directory=Path(directory);dbdir=directory/'database';dbdir.mkdir(mode=0o700)
        file,backup=d.online_snapshot(dbdir);os.chown(file,10001,10001);file.chmod(0o600);os.chown(dbdir,10001,10001);dbdir.chmod(0o700)
        owned=directory/'owned';owned.mkdir(mode=0o700);os.chown(owned,10001,10001)
        candidate=directory/'hub.toml';candidate.write_bytes(hub);os.chown(candidate,0,10001);candidate.chmod(0o640)
        mounts=[(d.ROOT/'config',d.ROOT/'config'),(candidate,d.ROOT/'config/hub.toml'),
            (inputs['authorization'],'/run/night-authorization.json'),(d.ROOT/'media'/PILOT,d.ROOT/'media'/PILOT)]
        for name,raw in policies.items():
            file=directory/name;file.write_bytes(raw);os.chown(file,0,10001);file.chmod(0o640);mounts.append((file,d.ROOT/'config'/name))
        for name in code:mounts.append((overlay/name,d.ROOT/name))
        for src,target in mounts:command+=['--mount','type=bind,source='+str(src)+',target='+str(target)+',readonly']
        command+=['--mount','type=bind,source='+str(dbdir)+',target=/run/issuer-db','--mount','type=bind,source='+str(owned)+',target=/owned']
        result=json.loads(d.run(command+[m.IMAGE,'python','-c',ISSUE],timeout=90))
        assert result['version']=='natural-candidate-issuer-result-v3' and result['pilot_job_id']==PILOT
        assert result['human_review'] is result['per_artifact_owner_acceptance'] is result['platform_publication_performed'] is result['live_db_modified'] is False
        outputs={}
        for name,ref in result['files'].items():
            assert name in ('workflow-launch.json','pilot.json');file=owned/name;info=file.lstat()
            assert stat.S_ISREG(info.st_mode) and info.st_uid==10001 and info.st_nlink==1 and 0<info.st_size<=1048576
            raw=d.read(file,1048576);assert len(raw)==ref['bytes'] and m.digest(raw)==ref['sha256'];outputs[name]=raw
        assert set(outputs)=={'workflow-launch.json','pilot.json'} and result['policy_sha256']==m.digest(policies[m.POLICIES[1]])
    assert all(d.read(inputs[name],16384)==raw for name,raw in original.items())
    report={'version':'natural-candidate-issuer-v3','state':'verified_plan_only','issued':False,'references':references,
        'pilot_job_id':PILOT,'activated_at':instant,'snapshot_backup':backup,'fresh_pilot_checked':True,'live_db_modified':False,
        'platform_publication_performed':False,'services_started':0,'policy_sha256':{name:m.digest(raw) for name,raw in policies.items()},
        'grant_sha256':m.digest(outputs['workflow-launch.json']),'snapshot_sha256':m.digest(outputs['pilot.json'])}
    if args.issue:
        destination=DESTINATION/str(uuid.uuid4());d.safe(destination)
        for parent in destination.parents:
            if parent.exists():m.protected(parent)
        destination.mkdir(parents=True,mode=0o750);os.chown(destination,0,10001);destination.chmod(0o750)
        for parent in (DESTINATION,*DESTINATION.parents):
            if parent==Path('/etc'):break
            os.chown(parent,0,10001);parent.chmod(0o750);m.protected(parent)
        for name,raw in {**policies,**outputs}.items():m.install_file(d,destination/name,raw)
        m.stage(destination,d) # Reopen all four actual installed byte strings.
        report.update(state='candidate_issued_only',issued=True,candidate_directory=str(destination))
    print(json.dumps(report))

if __name__=='__main__':main()
