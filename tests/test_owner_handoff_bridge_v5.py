"""Offline bridge contracts. No Docker, SSH, real profile or credential access."""
from copy import deepcopy
from datetime import datetime,timezone
import importlib.util,json,math,sqlite3,stat,sys
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT=Path(__file__).resolve().parents[1]
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    value=importlib.util.module_from_spec(spec);sys.modules[name]=value;spec.loader.exec_module(value);return value

bridge=load('owner_handoff_bridge_v5','scripts/ops/owner_handoff_bridge_v5.py')
tunnel=load('owner_handoff_tunnel_v5','scripts/ops/owner_handoff_tunnel_v5.py')
app={'__name__':'owned_test'};exec(bridge.APP_CODE,app)
NOW=1_800_000_000
SCOPE=bridge.parse_scope('a'*64,'sha256:'+'b'*64,'00000000-0000-4000-8000-000000000001','00000000-0000-4000-8000-000000000002','00000000-0000-4000-8000-000000000003')
def stamp(seconds):return datetime.fromtimestamp(seconds,timezone.utc).isoformat()
def owned_documents():
    digest=app['digest'];scope=deepcopy(SCOPE)
    payload={'site_uid':'1000000000000000','source_id':'OwnedID1234','source_url':'https://www.youtube.com/watch?v=OwnedID1234',
             'metadata':{'title':'Owned offline fixture'},'human_review':False,'part_index':0}
    payload['metadata_sha256']=digest(payload['metadata'])
    ref={'file':str(app['folder'](scope)/'detected.json'),'bytes':1,'sha256':'d'*64}
    protection={'blocked':True,'error_code':'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED','evidence':{
        'version':'bilibili-video-protection-guard-v3','inspection_failed':False,'visible_dialogs':[],
        'challenge_frames':[],'visible_challenge_controls':[{'tag':'DIV','role':'','classes':['geetest_panel']}]}}
    detected={'version':app['OBS'],'state':'detected',**{k:scope[k] for k in ('job_id','attempt_id','nonce')},
        'site_uid':payload['site_uid'],'source_id':payload['source_id'],'binding_sha256':digest(payload),
        'metadata_sha256':payload['metadata_sha256'],'human_review':False,'publication_authorized':False,
        'authentication_assistance_only':True,'automatic_resubmit':False,'same_browser_context':True,'same_page':True,
        'ttl_seconds':900,'reason':'visible_human_verification','interactive_verification_observed':True,
        'visible_body_empty':False,'readiness_observation':None,'expires_at':stamp(NOW+890),'observed_at':stamp(NOW-10),
        'page':{'origin':'https://member.bilibili.com','path':'/platform/upload/video/frame','query_keys':[]},
        'protection':protection,'handoff_actions':{'automatic_public_submit_clicks':0,'file_selections':0,'private_save_clicks':0}}
    ready={**deepcopy(detected),'state':'ready','observed_at':stamp(NOW-5),'web_host':'127.0.0.1','web_port':6084,
        'vnc_host':'127.0.0.1','vnc_port':5904,'password_file':str(app['folder'](scope)/'vnc-password.txt')}
    ack={'version':'replay-part-owner-handoff-parent-ack-v5','event_reference':ref,
         **{k:scope[k] for k in ('job_id','attempt_id','nonce')},'recorded_unknown_or_hold':True,
         'automatic_retry':False,'human_review':False,'publication_authorized':False}
    hold={'version':app['OBS'],'nonce':scope['nonce'],'event_reference':ref,'reason':detected['reason'],
          'authentication_assistance_only':True,'automatic_retry':False,'human_review':False}
    state={'state':'submit_unknown','attempt_id':scope['attempt_id'],'human_review':False,
           'public_submission_approved':False,'owner_handoff':hold,'key':'e'*64}
    parent={'job_id':scope['job_id'],'job_type':'replay_part_publish_v5_p01','queue':'video','source_platform':'youtube',
        'policy_version':app['POLICY'],'source_id':payload['source_id'],'payload':payload,'result':{app['STATE']:state},
        'state':'processing','stage':'replay_part_owner_handoff_wait','lease_owner':'owned-main-worker','lease_until':NOW+60}
    audit={'target':parent['job_id'],'action':'replay_part_owner_handoff_before_gui','principal':'worker:replay-video-v1','outcome':hold,'at':NOW-5}
    return scope,ready,detected,ack,{'parent':parent,'lookups':[],'audits':[audit],'detected_reference':ref}

def check(documents):return app['validate_documents'](*documents,NOW)

def runtime_fixture(service='video-worker',mode='--publish'):
    info={'Id':SCOPE['cid'],'Image':SCOPE['image'],'State':{'Running':True},'Config':{'User':'10001:10001','Entrypoint':None,
        'WorkingDir':str(bridge.ROOT),'Cmd':['python','-m',bridge.ENTRY,'--publish'],
        'Labels':{'com.docker.compose.service':'video-worker','com.docker.compose.project.working_dir':str(bridge.ROOT)}},
        'HostConfig':{'Privileged':False,'ReadonlyRootfs':True,'PublishAllPorts':False,'PortBindings':{},'NetworkMode':'owned-net'},
        'NetworkSettings':{'Ports':{}},'Mounts':[{'Destination':str(bridge.ROOT/n),'Source':str(bridge.ROOT/n),'Type':'bind','RW':True} for n in ('data','media')]}
    info['Config']['Labels']['com.docker.compose.service']=service
    info['Config']['Cmd'][-1]=mode
    return info

@pytest.mark.parametrize('service,mode',[('video-worker','--publish'),('replay-publication-worker','--publication-only')])
def test_running_runtime_rejects_foreign_scope_publish_ports_and_wrong_role(service,mode):
    info=runtime_fixture(service,mode)
    assert bridge.validate_runtime(info,SCOPE) is True
    for mutate in (lambda v:v.update(Image='sha256:'+'c'*64),lambda v:v['Config'].update(User='0'),
                   lambda v:v['Config'].update(Cmd=['python','-m',bridge.ENTRY,'--register-only']),
                   lambda v:v['HostConfig'].update(PortBindings={'6084/tcp':[{'HostPort':'6084'}]}),
                   lambda v:v['NetworkSettings'].update(Ports={'6084/tcp':[{'HostIp':'127.0.0.1'}]}),
                   lambda v:v['Mounts'].append({'Destination':'/var/run/docker.sock'})):
        bad=deepcopy(info);mutate(bad)
        with pytest.raises(RuntimeError):bridge.validate_runtime(bad,SCOPE)

@pytest.mark.parametrize('service,mode',[
    ('video-worker','--publication-only'),('replay-publication-worker','--publish'),
    ('replay-acquisition-worker','--acquisition-only'),('unknown','--publication-only')])
def test_bridge_requires_exact_service_command_pair(service,mode):
    with pytest.raises(RuntimeError,match='RUNTIME_INVALID'):
        bridge.validate_runtime(runtime_fixture(service,mode),SCOPE)

@pytest.mark.parametrize('service,mode',[('video-worker','--publish'),('replay-publication-worker','--publication-only')])
@pytest.mark.parametrize('extra',[False,True])
def test_bridge_requires_one_publisher_across_both_generations(monkeypatch,service,mode,extra):
    calls=[]
    def run(argv):
        calls.append(argv)
        if argv[1]=='inspect':return json.dumps([runtime_fixture(service,mode)]).encode()
        name=next(x.split('=',2)[-1] for x in argv if x.startswith('label='))
        return ((SCOPE['cid']+'\n') if name==service else ('c'*64+'\n' if extra else '')).encode()
    monkeypatch.setattr(bridge,'_run',run)
    if extra:
        with pytest.raises(RuntimeError,match='CURRENT_WORKER_CHANGED'):bridge.runtime(SCOPE)
    else:bridge.runtime(SCOPE)
    assert len(calls)==3

def test_fixed_remote_command_cannot_supply_a_shell_path_endpoint_or_extra_operation():
    args=bridge.docker_argv(SCOPE,'relay')
    assert args[:7]==['/usr/bin/docker','exec','--user','10001:10001','--interactive',SCOPE['cid'],'/usr/bin/env']
    assert args[-4:]==['relay',SCOPE['job_id'],SCOPE['attempt_id'],SCOPE['nonce']]
    assert "('127.0.0.1',6084)" in bridge.APP_CODE and 'socket.create_connection' in bridge.APP_CODE
    command=tunnel.remote_command(SCOPE,'relay','c'*64)
    assert command.startswith('sudo -n /usr/local/lib/lighthouse-owner-handoff-v5/owner_handoff_bridge_v5.py --mode relay ')
    assert '--port' not in command and tunnel.LOCAL==('127.0.0.1',18789)
    for bad in ('../status','relay; id','shell'):
        with pytest.raises(RuntimeError):tunnel.remote_command(SCOPE,bad,'c'*64)
    with pytest.raises(RuntimeError):bridge.parse_scope('a'*64+';id',SCOPE['image'],SCOPE['job_id'],SCOPE['attempt_id'],SCOPE['nonce'])

def test_ready_requires_actual_main_lease_same_nonce_hold_and_committed_audit():
    docs=owned_documents();assert check(docs)['lease_role']=='main'
    for mutate in (lambda f:f['parent'].update(lease_until=NOW-1),lambda f:f['parent'].update(lease_until=math.inf),
                   lambda f:f['parent'].update(state='completed'),lambda f:f.update(audits=[]),
                   lambda f:f['parent']['result'][app['STATE']]['owner_handoff'].update(nonce='00000000-0000-4000-8000-000000000004'),
                   lambda f:f['parent']['payload'].update(source_id='Foreign1234')):
        bad=deepcopy(docs);mutate(bad[-1])
        with pytest.raises(RuntimeError):check(bad)
    bad=deepcopy(docs);bad[3]['recorded_unknown_or_hold']=False
    with pytest.raises(RuntimeError):check(bad)

def test_lookup_uses_real_child_lease_without_manufacturing_a_parent_lease():
    docs=owned_documents();scope,ready,detected,ack,facts=docs;parent=facts['parent'];state=parent['result'][app['STATE']]
    hold=state.pop('owner_handoff');parent.update(state='publish_unknown',stage='native_submit_unknown',lease_owner=None,lease_until=None)
    child_id='00000000-0000-4000-8000-000000000005'
    child={'job_id':child_id,'job_type':'replay_part_lookup_v5','policy_version':app['POLICY'],'source_platform':'bilibili','queue':'video',
        'state':'processing','lease_owner':'owned-lookup-worker','lease_until':NOW+40,'stage':'replay_part_lookup_owner_handoff_wait',
        'result':{'owner_handoff':hold},'payload':{'version':'replay-part-read-only-lookup-v5','parent_publish_job_id':parent['job_id'],
        'attempt_id':scope['attempt_id'],'parent_binding_sha256':detected['binding_sha256'],'site_uid':detected['site_uid'],
        'source_id':parent['source_id'],'metadata_sha256':detected['metadata_sha256'],'publication_key':state['key'],'poll_number':7}}
    facts['lookups']=[{'job':child,'schedule':{'active_child_id':child_id,'status':'waiting','deadline':NOW+600,'polls_total':7}}]
    facts['audits']=[{'target':child_id,'action':'replay_part_lookup_owner_handoff_before_gui','principal':'worker:replay-part-lookup-v5','outcome':hold,'at':NOW-5}]
    assert check(docs)['lease_job_id']==child_id
    for mutate in (lambda f:f['lookups'][0]['job'].update(lease_until=NOW-1),lambda f:f['parent'].update(lease_owner='fake-parent'),
                   lambda f:f['lookups'][0]['schedule'].update(active_child_id='foreign-child'),
                   lambda f:f['lookups'][0]['job']['payload'].update(publication_key='wrong')):
        bad=deepcopy(docs);mutate(bad[-1])
        with pytest.raises(RuntimeError):check(bad)

def test_expiry_terminal_and_empty_body_are_not_accepted_as_a_human_challenge(tmp_path):
    docs=owned_documents();docs[1]['expires_at']=docs[2]['expires_at']=stamp(NOW-1)
    with pytest.raises(RuntimeError):check(docs)
    app['terminal_check'](tmp_path)
    (tmp_path/'page_readable.json').write_text('{}',encoding='utf-8')
    with pytest.raises(RuntimeError,match='TERMINAL'):app['terminal_check'](tmp_path)
    docs=owned_documents();e=docs[2]['protection']['evidence'];e.update(visible_challenge_controls=[{'tag':'BODY','role':'','classes':['risk-captcha-adapt-pc','risk-captcha-adapt']}])
    with pytest.raises(RuntimeError):check(docs)

def test_db_facts_reads_real_sqlite_snapshot_without_mutating_jobs_or_audit(tmp_path,monkeypatch):
    docs=owned_documents();facts=docs[-1];(tmp_path/'data').mkdir();file=tmp_path/'data/hub.sqlite3'
    with sqlite3.connect(file) as db:
        db.execute('CREATE TABLE jobs(job_id TEXT,job_type TEXT,policy_version TEXT,state TEXT,payload TEXT,result TEXT,queue TEXT,source_platform TEXT,source_id TEXT,stage TEXT,lease_owner TEXT,lease_until REAL)')
        db.execute('CREATE TABLE audit(at REAL,principal TEXT,action TEXT,target TEXT,outcome TEXT)')
        p=facts['parent'];keys=list(p);values=[json.dumps(p[k]) if k in ('payload','result') else p[k] for k in keys]
        db.execute('INSERT INTO jobs('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',values)
        a=facts['audits'][0];db.execute('INSERT INTO audit VALUES(?,?,?,?,?)',(a['at'],a['principal'],a['action'],a['target'],json.dumps(a['outcome'])))
    before=file.read_bytes();monkeypatch.setitem(app,'ROOT',tmp_path)
    actual=app['db_facts'](SCOPE);actual['detected_reference']=facts['detected_reference']
    assert actual==facts and file.read_bytes()==before

def test_synthetic_password_and_png_have_strict_bytes_owner_mode_and_links(monkeypatch):
    item=SimpleNamespace(st_mode=stat.S_IFREG|0o600,st_uid=10001,st_nlink=1)
    assert app['check_password'](b'Ab1+/xyz\n',item)==b'Ab1+/xyz'
    for raw in (b'short',b'Ab1+/xyz\nextra',b'Ab1+/xy='):
        with pytest.raises(RuntimeError):app['check_password'](raw,item)
    for key,value in (('st_uid',0),('st_mode',stat.S_IFREG|0o644),('st_nlink',2)):
        bad=deepcopy(item);setattr(bad,key,value)
        with pytest.raises(RuntimeError):app['check_password'](b'Ab1+/xyz',bad)
    import hashlib
    image=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'+(1).to_bytes(4,'big')*2+b'\0'*9
    reference={'file':str(Path('/owned/image.png')),'bytes':len(image),'sha256':hashlib.sha256(image).hexdigest()}
    monkeypatch.setitem(app,'read',lambda *a,**k:(image,item))
    app['check_ref'](reference,Path('/owned/image.png'),image=True)
    invalid=b'J'*len(image);monkeypatch.setitem(app,'read',lambda *a,**k:(invalid,item))
    reference['sha256']=hashlib.sha256(invalid).hexdigest()
    with pytest.raises(RuntimeError):app['check_ref'](reference,Path('/owned/image.png'),image=True)

def test_local_status_handshake_and_cleanup_never_expand_scope_or_delete_replacements(tmp_path,monkeypatch):
    status={**check(owned_documents()),'cid':SCOPE['cid'],'image':SCOPE['image'],'bridge_sha256':'c'*64}
    assert tunnel.validate_status(status,SCOPE,'c'*64,now=NOW)==status
    bad={**status,'web_port':5904}
    with pytest.raises(RuntimeError):tunnel.validate_status(bad,SCOPE,'c'*64,now=NOW)
    class Channel:
        def recv_ready(self):return True
        def recv(self,n):return json.dumps({'state':'relay_ready',**{k:SCOPE[k] for k in ('job_id','attempt_id','nonce')},'expires_at':status['expires_at']}).encode()+b'\nHTTP/1.1'
    assert tunnel.relay_header(Channel(),SCOPE,status['expires_at'])==b'HTTP/1.1'
    with pytest.raises(RuntimeError):tunnel.relay_header(Channel(),{**SCOPE,'nonce':'foreign'},status['expires_at'])
    monkeypatch.setattr(tunnel,'KEYS',tmp_path)
    path=tunnel.credential_path(SCOPE['nonce']);path.write_bytes(b'owned-only')
    item=path.stat()
    with pytest.raises(RuntimeError):tunnel.remove_credential(path,(item.st_dev,item.st_ino+1))
    assert path.read_bytes()==b'owned-only'
    tunnel.remove_credential(path,(item.st_dev,item.st_ino));assert not path.exists()
    with pytest.raises((ValueError,RuntimeError)):tunnel.credential_path('../../lighthouse_ed25519')
