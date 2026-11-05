"""Owned handoff protocol/SQLite checks, no GUI, challenge solving or platform.

Only root filesystem ownership is an explicit seam. The original Worker lease
and unknown/hold transaction are real; event pixels/guard JSON are synthetic.
"""
from datetime import datetime,timezone,timedelta
from copy import deepcopy
import base64,json,queue,time,uuid
from types import SimpleNamespace
import pytest

from services.hub.errors import HubError
from services.hub.db import encode
from services.hub.youtube_download import digest
from services.hub.replay_video_web import ReplayVideoWeb,WORKER_PRINCIPAL
from services.hub.replay_parts_v5 import STATE
from services.hub.replay_part_publish_worker_v5 import PartReplaySession,part_replay_manifest
from services.hub import replay_part_owner_handoff_v5 as module
from test_replay_parts_v5 import publisher,reserved,write,ref
from test_replay_delivery_v5 import delivery
from test_replay_fifo_v4 import fifo
from test_ytdlp_highest_v2 import tracks
from test_replay_part_moderation_v5 import lookup


def protocol(s,manifest):
    nonce=str(uuid.uuid4());request=str(uuid.uuid4());now=datetime.now(timezone.utc);folder=s.root/'media'/manifest['job_id']/'replay-browser-part-v5'/manifest['attempt_id']/'owner-handoff'/nonce
    folder.mkdir(parents=True)
    image=folder/'detected.png';image.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jR3sAAAAASUVORK5CYII='))
    common={'job_id':manifest['job_id'],'attempt_id':manifest['attempt_id'],'nonce':nonce,'site_uid':manifest['target_site_uid'],
        'source_id':manifest['source_id'],'binding_sha256':manifest['binding_sha256'],'metadata_sha256':manifest['metadata_sha256'],
        'human_review':False,'publication_authorized':False,'authentication_assistance_only':True,'automatic_resubmit':False,
        'same_browser_context':True,'same_page':True,'ttl_seconds':60,'expires_at':(now+timedelta(seconds=60)).isoformat(),
        'reason':'visible_human_verification','interactive_verification_observed':True,'visible_body_empty':False,'readiness_observation':None}
    body={'version':module.OBSERVATION,**common,'state':'detected','observed_at':now.isoformat(),
        'page':{'origin':'https://member.bilibili.com','path':'/platform/upload/video/frame','query_keys':[]},
        'protection':{'blocked':True,'error_code':'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED','evidence':{
            'inspection_failed':False,'challenge_frames':[],'visible_dialogs':[],'visible_challenge_controls':[{'tag':'DIV','classes':['owned-challenge']}]}},
        'screenshot':ref(image),'handoff_actions':{'automatic_public_submit_clicks':0,'file_selections':0,'private_save_clicks':0}}
    event={'version':module.EVENT,**common,'event_reference':write(folder/'detected.json',body),'request_id':request,'operation':'submit'}
    return SimpleNamespace(s=s,manifest=manifest,event=event,body=body,folder=folder,request=request)


@pytest.fixture
def handoff(runtime,monkeypatch):
    s,db=runtime;config={'version':module.CONFIG,'enabled':True,'ttl_seconds':60}
    monkeypatch.setattr(ReplayVideoWeb,'_root_readonly_file',lambda self,file,**kw:encode(config).encode())
    manifest={'version':'replay-part-native-browser-manifest-v5','job_id':str(uuid.uuid4()),'attempt_id':str(uuid.uuid4()),
        'target_site_uid':'1000000000000000','source_id':'ownedPart01','binding_sha256':'a'*64,'metadata_sha256':'b'*64}
    f=protocol(s,manifest);f.config=config;return f


def acknowledge(f,callback,seen=None,check=lambda:None):
    return module.acknowledge(f.s,f.manifest,f.event,request_id=f.request,operation='submit',callback=callback,seen=set() if seen is None else seen,assert_lease=check)


def diagnostic(f):
    marker={'blocked':True,'error_code':'BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED','evidence':{
        'version':'bilibili-video-protection-guard-v3','inspection_failed':False,'truncated':False,'top_level_authentication':False,
        'native_mp4_uploaders':0,'challenge_frames':[],'visible_dialogs':[],
        'visible_challenge_controls':[{'tag':'BODY','role':'','classes':['risk-captcha-adapt','risk-captcha-adapt-pc']}]}}
    folder=f.folder.parent.parent/'protection-readiness'/str(uuid.uuid4());folder.mkdir(parents=True)
    shots={}
    for key in ('before','after'):
        file=folder/(key+'.png');file.write_bytes((f.folder/'detected.png').read_bytes())
        shots[key]={'protection':deepcopy(marker),'visible_body_text':'','screenshot':ref(file)}
    value={'version':'replay-part-empty-body-readiness-v5',**{k:f.manifest[k] for k in ('job_id','attempt_id','source_id','binding_sha256','metadata_sha256')},
        'site_uid':f.manifest['target_site_uid'],'max_wait_ms':5000,**shots,
        'polls':[{'elapsed_ms':5001.5,'protection':deepcopy(marker),'visible_body_empty':True}],
        'outcome':'still_unreadable_body_marker','observed_at':datetime.now(timezone.utc).isoformat(),'page':f.body['page'],
        'read_only':True,'actions':{'file_selections':0,'submit_clicks':0,'save_clicks':0}}
    reference=write(folder/'observation.json',value)
    changes={'reason':'unreadable_body_marker','interactive_verification_observed':False,'visible_body_empty':True,
        'authentication_assistance_only':False,'readiness_observation':reference}
    f.body.update(changes,protection=marker);f.event.update(changes)
    f.event['event_reference']=write(f.folder/'detected.json',f.body)
    return value,folder


def test_exact_empty_body_still_blocked_is_ui_diagnostic_not_claimed_human_verification(handoff):
    f=handoff;diagnostic(f);seen=[]
    acknowledge(f,lambda body,reference:seen.append(body))
    assert len(seen)==1 and seen[0]['reason']=='unreadable_body_marker'
    assert seen[0]['authentication_assistance_only'] is seen[0]['interactive_verification_observed'] is False
    assert (f.folder/'parent-ack.json').is_file()


@pytest.mark.parametrize('changed',['missing_evidence','nonempty_after','real_challenge_poll'])
def test_empty_body_diagnostic_requires_complete_zero_action_readiness_evidence(handoff,changed):
    f=handoff;value,folder=diagnostic(f);calls=[]
    if changed=='missing_evidence':f.event['readiness_observation']=f.body['readiness_observation']=None
    else:
        if changed=='nonempty_after':value['after']['visible_body_text']='actual loaded native form'
        else:value['polls'][0]['protection']['evidence']['visible_challenge_controls'].append({'tag':'DIV','role':'dialog','classes':['real-challenge']})
        reference=write(folder/'observation.json',value);f.event['readiness_observation']=f.body['readiness_observation']=reference
    f.event['event_reference']=write(f.folder/'detected.json',f.body)
    with pytest.raises(HubError):acknowledge(f,lambda *args:calls.append(args))
    assert not calls and not (f.folder/'parent-ack.json').exists()


def test_original_owner_expiry_is_rechecked_after_callback_before_ack(handoff,monkeypatch):
    f=handoff;clock=[time.time()]
    monkeypatch.setattr(module.time,'time',lambda:clock[0])
    def persist(*args):clock[0]+=61
    with pytest.raises(HubError,match='REPLAY_PART_OWNER_HANDOFF_EXPIRED'):acknowledge(f,persist)
    assert not (f.folder/'parent-ack.json').exists()


@pytest.mark.parametrize('owned_live',[False,True])
def test_forced_node_exit_only_cleans_new_nonce_secrets_after_owned_descendants_exit(handoff,monkeypatch,owned_live):
    """The exit seam replaces only process termination; exact file IO is real."""
    from services.hub.replay_multipart_private_v4 import PrivateSession
    f=handoff;before=module.existing_nonces(f.s,f.manifest)
    old=f.folder/'vnc-password.txt';old.write_bytes(b'owned old credential retained');old.chmod(0o600)
    directory=f.folder.parent/str(uuid.uuid4());directory.mkdir(mode=0o700)
    for name in ('Xauthority','vnc-password.txt'):
        file=directory/name;file.write_bytes(b'owned disposable secret');file.chmod(0o600)
    evidence=directory/'detected.json';evidence.write_bytes(b'{"owned_evidence":"retained"}')
    session=PartReplaySession.__new__(PartReplaySession);session.closed=False;session.settings=f.s;session.manifest=f.manifest
    session._handoff_existing=before;session._owned={123:456};session._live_owned=lambda:[123] if owned_live else []
    def forced_exit(self):self.closed=True
    monkeypatch.setattr(PrivateSession,'close',forced_exit)
    session.close()
    assert old.read_bytes()==b'owned old credential retained' and evidence.is_file()
    assert all((directory/name).exists() is owned_live for name in ('Xauthority','vnc-password.txt'))
    receipts=list(directory.parent.parent.glob('owned-process-cleanup-*.json'))
    assert len(receipts)==1
    receipt=json.loads(receipts[0].read_bytes())
    assert receipt['temporary_credentials_deleted']==(0 if owned_live else 2)
    assert 'owned disposable secret' not in receipts[0].read_text()


def test_exact_event_calls_durable_callback_before_exclusive_ack_and_bounds_actual_expiry(handoff):
    f=handoff;calls=[]
    def persist(body,reference):
        assert not (f.folder/'parent-ack.json').exists();calls.append((body,reference))
    seconds=acknowledge(f,persist)
    assert 45<seconds<=105 and len(calls)==1
    ack=json.loads((f.folder/'parent-ack.json').read_bytes())
    assert ack['recorded_unknown_or_hold'] is True and ack['automatic_retry'] is ack['publication_authorized'] is ack['human_review'] is False
    assert ack['event_reference']==f.event['event_reference'] and len(ack)==9


@pytest.mark.parametrize('changed',['request','uid','sha','body_only','actions','expired','config_off'])
def test_wrong_or_unproven_event_never_gets_gui_ack(handoff,changed):
    f=handoff;calls=[]
    if changed=='request':f.event['request_id']=str(uuid.uuid4())
    elif changed=='uid':f.event['site_uid']='1'
    elif changed=='sha':f.event['event_reference']['sha256']='f'*64
    elif changed=='config_off':f.config['enabled']=False
    else:
        if changed=='body_only':f.body['protection']['evidence']['visible_challenge_controls']=[{'tag':'BODY','classes':['risk-captcha-adapt','risk-captcha-adapt-pc']}]
        elif changed=='actions':f.body['handoff_actions']['automatic_public_submit_clicks']=True
        elif changed=='expired':f.body['expires_at']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();f.event['expires_at']=f.body['expires_at']
        f.event['event_reference']=write(f.folder/'detected.json',f.body)
    with pytest.raises(HubError):acknowledge(f,lambda *a:calls.append(a))
    assert not calls and not (f.folder/'parent-ack.json').exists()


def test_repeat_nonce_does_not_extend_wait_or_repeat_callback(handoff):
    f=handoff;seen=set();calls=[];acknowledge(f,lambda *a:calls.append(a),seen)
    with pytest.raises(HubError):acknowledge(f,lambda *a:calls.append(a),seen)
    assert len(calls)==1


def test_failed_real_checkpoint_never_acknowledges_owner_access(handoff):
    def lost(*args):raise HubError('OWNED_ACTUAL_LEASE_LOST',review=True)
    with pytest.raises(HubError):acknowledge(handoff,lost)
    assert not (handoff.folder/'parent-ack.json').exists()


def test_current_rpc_can_wait_for_owner_without_extending_any_submit_ticket(handoff):
    f=handoff;session=PartReplaySession.__new__(PartReplaySession)
    session.settings=f.s;session.manifest=f.manifest;session.assert_lease=lambda:None;session.messages=queue.Queue()
    session._rpc_context=(f.request,'submit');session._handoff_seen=set();clock=[0.0]
    session._owner_handoff_callback=lambda *args:clock.__setitem__(0,2.0)
    session.messages.put(f.event);session.messages.put({'version':'replay-native-browser-response-v1','ok':False,'error_code':'REPLAY_PART_OWNER_VERIFIED_RECONCILE_REQUIRED'})
    from services.hub import replay_part_publish_worker_v5 as transport
    old=transport.time.monotonic;transport.time.monotonic=lambda:clock[0]
    try:value=session._receive(1)
    finally:transport.time.monotonic=old
    assert value['ok'] is False and value['error_code']=='REPLAY_PART_OWNER_VERIFIED_RECONCILE_REQUIRED'
    assert (f.folder/'parent-ack.json').is_file()


def test_real_worker_unknown_and_audit_exist_before_gui_ack(reserved,monkeypatch):
    f=reserved;job=f.db.get(f.child);job['result'][STATE]['state']='submitting'
    with f.db.transaction() as db:
        db.execute("UPDATE jobs SET state='pending',result=? WHERE job_id=?",(encode(job['result']),f.child))
        db.execute("UPDATE publications SET state='dispatching' WHERE job_id=?",(f.child,))
    claimed=f.db.claim('video',60,4)
    monkeypatch.setattr(ReplayVideoWeb,'_root_readonly_file',lambda self,file,**kw:encode({'version':module.CONFIG,'enabled':True,'ttl_seconds':60}).encode())
    with f.publish.worker_context(claimed):
        job,binding,state=f.publish.snapshot(f.child,write=False)
        spec=part_replay_manifest(f.s,f.db,job,binding,state,state['attempt_id'],require_cover=False);h=protocol(f.s,spec)
        def persist(body,reference):
            module.record_worker_handoff(f.publish,f.child,body,reference)
            assert f.db.query('SELECT state FROM publications WHERE job_id=?',(f.child,))[0]['state']=='unknown'
            assert f.db.get(f.child)['state']=='processing' and f.db.get(f.child)['lease_owner']==claimed['lease_owner']
            assert f.db.query("SELECT 1 FROM audit WHERE action='replay_part_owner_handoff_before_gui' AND target=?",(f.child,))
        acknowledge(h,persist,check=f.publish.assert_worker_lease)
    assert (h.folder/'parent-ack.json').exists() and f.db.get(f.child)['result'][STATE]['state']=='submit_unknown'


def test_lease_lost_after_real_checkpoint_still_refuses_gui_ack(reserved,monkeypatch):
    f=reserved
    with f.db.transaction() as db:db.execute("UPDATE jobs SET state='pending' WHERE job_id=?",(f.child,))
    claimed=f.db.claim('video',60,4)
    monkeypatch.setattr(ReplayVideoWeb,'_root_readonly_file',lambda self,file,**kw:encode({'version':module.CONFIG,'enabled':True,'ttl_seconds':60}).encode())
    with f.publish.worker_context(claimed):
        job,binding,state=f.publish.snapshot(f.child,write=False)
        h=protocol(f.s,part_replay_manifest(f.s,f.db,job,binding,state,state['attempt_id'],require_cover=False))
        def persist(body,reference):
            module.record_worker_handoff(f.publish,f.child,body,reference)
            with f.db.transaction() as db:db.execute("UPDATE jobs SET lease_owner='owned competing lease' WHERE job_id=?",(f.child,))
        with pytest.raises(HubError):acknowledge(h,persist,check=f.publish.assert_worker_lease)
    assert not (h.folder/'parent-ack.json').exists()
    assert f.db.query('SELECT state FROM publications WHERE job_id=?',(f.child,))[0]['state']=='unknown'


@pytest.mark.parametrize('lose_lease',[False,True])
def test_lookup_handoff_uses_child_lease_and_never_changes_parent_lease_or_publication(lookup,monkeypatch,lose_lease):
    from services.hub import replay_part_moderation_v5 as mod
    f=lookup;mod.schedule_tick(f.s,f.db,f.lookup);child=mod.QueueView(f.db).claim('moderation',60,4)
    before=f.db.get(f.parent);pubs=f.db.query('SELECT * FROM publications');b=before['payload'];state=before['result'][STATE]
    manifest={'version':'replay-part-native-browser-manifest-v5','job_id':f.parent,'attempt_id':state['attempt_id'],
        'target_site_uid':b['site_uid'],'source_id':b['source_id'],'binding_sha256':digest(b),'metadata_sha256':b['metadata_sha256']}
    h=protocol(f.s,manifest);h.event['operation']='lookup'
    monkeypatch.setattr(ReplayVideoWeb,'_root_readonly_file',lambda self,file,**kw:encode({'version':module.CONFIG,'enabled':True,'ttl_seconds':60}).encode())
    def check():mod.lookup_context(f.s,f.db,f.lookup,child)
    def persist(body,reference):
        module.record_lookup_handoff(f.lookup,child,body,reference)
        if lose_lease:
            with f.db.transaction() as db:db.execute("UPDATE jobs SET lease_owner='owned competing lease' WHERE job_id=?",(child['job_id'],))
    def run():return module.acknowledge(f.s,h.manifest,h.event,request_id=h.request,operation='lookup',callback=persist,seen=set(),assert_lease=check)
    if lose_lease:
        with pytest.raises(HubError):run()
        assert not (h.folder/'parent-ack.json').exists()
    else:
        run();assert (h.folder/'parent-ack.json').is_file()
    assert f.db.get(f.parent)==before and f.db.query('SELECT * FROM publications')==pubs
    assert f.db.get(child['job_id'])['result']['owner_handoff']['event_reference']==h.event['event_reference']


@pytest.mark.parametrize('code',['REPLAY_PART_OWNER_VERIFIED_RECONCILE_REQUIRED','REPLAY_PART_OWNER_RECONCILE_REQUIRED'])
def test_owner_verified_lookup_only_schedules_another_read_without_review_or_resubmit(lookup,code):
    from contextlib import contextmanager
    from services.hub import replay_part_moderation_v5 as mod
    f=lookup;config=f.s.root/'config/bilibili-video-handoff-v5.json';config.parent.mkdir(exist_ok=True)
    config.write_text(encode({'version':module.CONFIG,'enabled':True,'ttl_seconds':60}),'utf-8')
    class OwnedVerifiedBrowser:
        @contextmanager
        def session(self,manifest,*,assert_lease,before_launch,owner_handoff):
            assert callable(owner_handoff);assert_lease();before_launch()
            class Session:
                def operation(self,*a,**kw):raise HubError(code,review=True)
            yield Session()
    before=f.db.get(f.parent);pubs=f.db.query('SELECT * FROM publications')
    mod.schedule_tick(f.s,f.db,f.lookup)
    worker=mod.build_lookup_worker(f.s,f.db,service=f.lookup,browser=OwnedVerifiedBrowser(),manifest_builder=lambda *a,**kw:{'owned_lookup_manifest':True})
    assert worker.once('moderation')
    child=f.db.query('SELECT * FROM jobs WHERE job_type=?',(mod.LOOKUP_JOB_TYPE,))[0]
    assert child['state']=='completed' and child['error_code'] is None
    schedule=f.db.query('SELECT * FROM '+mod.TABLE)[0]
    assert schedule['status']=='waiting' and schedule['next_check']>time.time()
    assert f.db.get(f.parent)==before and f.db.query('SELECT * FROM publications')==pubs
