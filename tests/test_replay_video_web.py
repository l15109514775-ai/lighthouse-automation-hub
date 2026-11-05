"""Owned SQLite/files only. No browser, credentials, real video or live gate."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

import pytest

from test_short_video_web import short_video, PRINCIPAL, result as short_result
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_video_web import ReplayVideoWeb, POLICY, STATE, KIND, JOB_TYPE
from services.hub.youtube_download import STATE as DOWNLOAD_STATE, digest

CHANNELS = ['UCaaaaaaaaaaaaaaaaaaaaaa', 'UCbbbbbbbbbbbbbbbbbbbbbb']


def write_policy(file, value):
    file.write_text('\n'.join(key+' = '+json.dumps(item, ensure_ascii=False) for key,item in value.items())+'\n', 'utf-8')


def update_source(fixture, *, duration=25715, channel=CHANNELS[0], end='2026-10-31T23:09:22Z'):
    s,db,_,source,_,_=fixture; job=db.get(source); payload=deepcopy(job['payload']); checkpoint=deepcopy(job['result'])
    payload.update(channel_id=channel, source_api='youtube-data-v3-official', duration='PT'+str(duration)+'S',
        published_at='2026-10-31T14:00:12Z')
    payload['raw']={'id':job['source_id'], 'snippet':{'channelId':channel,'title':payload['title'],'publishedAt':payload['published_at']}, 'status':{'privacyStatus':'public'},
        'liveStreamingDetails':{'actualStartTime':'2026-10-31T16:00:36Z','actualEndTime':end},
        'contentDetails':{'duration':payload['duration']}}
    state=checkpoint[DOWNLOAD_STATE]; state['binding'].update(channel_id=channel,duration=duration,source_payload_sha256=digest(payload))
    state['source_asset']['duration']=duration
    verified={'version':state['version'],'binding_sha256':digest(state['binding']), 'source_asset':state['source_asset'],
        'receipt':state['receipt'],'human_review':False,'publication_authorized':False,'creator_original_bytes_verified':False}
    receipt=Path(state['source_asset']['file']).parent/'verified.json';receipt.write_text(encode(verified),'utf-8')
    with db.transaction() as connection:connection.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?',(encode(payload),encode(checkpoint),source))
    return source


@pytest.fixture
def replay(short_video):
    s,db,_,source,metadata,_=short_video
    update_source(short_video)
    metadata=deepcopy(metadata);metadata['title']='Owned seven-hour replay fixture'
    metadata.pop('category_id');metadata['category_label']='娱乐'
    policy={'version':POLICY,'enabled':True,'target_site_uid':'123456','target_account_name':'Owned synthetic account',
        'public_submission_mode':ReplayVideoWeb.PUBLIC_MODE,'owner_authorization_reference':'Owned only; not actual owner approval',
        'owner_permission_reference':'Owned internal rights fixture','workflow_authorization_reference':'Owned workflow request reference',
        'allowed_channel_ids':CHANNELS,'source_scope':'pilot_single','acceptance_video_ids':['abcdefghijk'],
        'activated_at':'2026-10-31T23:00:00Z','automation_enabled':False}
    file=s.root/'config/replay-video-web-policy.toml';write_policy(file,policy)
    return s,db,ReplayVideoWeb(s,db),source,metadata,file,policy


def candidate(f):
    return f[2].create_candidate(PRINCIPAL,f[3],f[4])


def uploaded(f):
    job=candidate(f);service=f[2];binding=f[1].get(job)['payload'];attempt=str(uuid.uuid4())
    service.start_upload(PRINCIPAL,job,attempt_id=attempt,site_uid='123456')
    service.record_upload(PRINCIPAL,job,{'version':'short-video-browser-upload-v1','attempt_id':attempt,'site_uid':'123456',
        'artifact_sha256':binding['artifact_sha256'],'file_bytes':binding['file_bytes'],
        'upload_reference_sha256':'b'*64,'same_attempt_observed':True,'server_file_hash_verified':False})
    return job,attempt,binding


def authorize(f,job):
    return f[2].record_automation_authorization(PRINCIPAL,job,
        actual_owner_workflow_response='Owned workflow grant, not individual human review or a real user statement',
        owner_response_reference='Owned workflow-only fixture',workflow_authorization_reference=f[6]['workflow_authorization_reference'])


def reserve(f,job,attempt,**changes):
    args={'attempt_id':attempt,'site_uid':'123456','upload_reference_sha256':'b'*64};args.update(changes)
    return f[2].reserve_submit(PRINCIPAL,job,**args)


def enable_before_prepare(f):
    f[6]['automation_enabled']=True;write_policy(f[5],f[6])


class OwnedSemanticVerifier:
    """Test double only. This is never installed by ReplayVideoWeb's default."""
    def __init__(self,root,file):self.calls=0;self.root=root;self.file=file
    def verify(self,job,binding,state,policy,**kwargs):
        self.calls+=1
        receipt=json.loads(self.file.read_bytes());expected={'job_id':job['job_id'],'binding_sha256':digest(binding),
            'site_uid':binding['site_uid'],'attempt_id':state['attempt_id']}
        if (any(receipt[key]!=value for key,value in expected.items()) or receipt['human_review'] is not False or
                receipt['per_artifact_owner_acceptance'] is not False):raise HubError('REPLAY_MACHINE_ACCEPTANCE_REQUIRED',review=True)
        # Exercise the verifier's actual file inputs; no network or real pass.
        parent=self.file.parent
        for reference in receipt['evidence'].values():
            raw=Path(reference['file']).read_bytes()
            if len(raw)!=reference['bytes'] or hashlib.sha256(raw).hexdigest()!=reference['sha256']:
                raise HubError('REPLAY_MACHINE_ACCEPTANCE_REQUIRED',review=True)
        assert json.loads((parent/'native-observation.json').read_bytes())=={'owned_only':True,'binding_sha256':expected['binding_sha256']}
        assert (parent/'results.xml').read_bytes()==b'<testsuite name="owned-only"><testcase name="owned_only"/></testsuite>'
        record={'version':'verified-replay-machine-checks-v1','job_id':job['job_id'],'binding_sha256':digest(binding),
            'policy_sha256':policy['_sha256'],'human_review':False,'per_artifact_owner_acceptance':False,'publication_authorized':False,
            'checks':{'cover':{'output':{'sha256':'c'*64},'provenance':{'sha256':'d'*64}}},'owned_only':True}
        folder=self.root/'media'/job['job_id']/'replay-machine-checks-v1'/str(uuid.uuid4());folder.mkdir(parents=True)
        file=folder/'receipt.json';raw=encode(record).encode();file.write_bytes(raw)
        return {**record,'evidence_file':str(file),'evidence_sha256':hashlib.sha256(raw).hexdigest(),'evidence_bytes':len(raw)}


def owned_proof(f,job,monkeypatch):
    s,db,service,*_=f;current,binding,state=service.snapshot(job);policy=service.policy()
    folder=s.root/'owned-only-machine-input'/job;folder.mkdir(parents=True)
    files={'ubuntu_test_attestation':('ubuntu-test-attestation.json',b'{"owned_only":true}'),
        'junit':('results.xml',b'<testsuite name="owned-only"><testcase name="owned_only"/></testsuite>'),
        'native_observation':('native-observation.json',encode({'owned_only':True,'binding_sha256':digest(binding)}).encode())}
    refs={}
    for name,(filename,body) in files.items():
        file=folder/filename;file.write_bytes(body);refs[name]={'file':str(file),'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()}
    expected={'job_id':job,'binding_sha256':digest(binding),'policy_sha256':policy['_sha256'],
        'source_id':binding['source_id'],'channel_id':binding['channel_id'],'download_job_id':binding['download_job_id'],
        'artifact_sha256':binding['artifact_sha256'],'file_bytes':binding['file_bytes'],'metadata_sha256':binding['metadata_sha256'],
        'site_uid':binding['site_uid'],'attempt_id':state['attempt_id'],
        'upload_reference_sha256':state['upload_observation']['upload_reference_sha256'],
        'workflow_authorization_reference':policy['workflow_authorization_reference'],'human_review':False,'per_artifact_owner_acceptance':False}
    proof={**expected,'version':'replay-machine-acceptance-v1','evidence':refs};file=folder/'receipt.json';file.write_text(encode(proof),'utf-8')
    verifier=OwnedSemanticVerifier(s.root,file);service.machine_acceptance_verifier=verifier
    return file,proof,verifier


def native_prepare(f,job,attempt,*,proof=None,record=True):
    s,db,service,*_=f;current,binding,state=service.snapshot(job)
    proof=proof or service.verify_for_publish(job)
    common={'job_id':job,'attempt_id':attempt,'site_uid':binding['site_uid'],'source_id':binding['source_id'],
        'source_url':binding['source_url'],'artifact_sha256':binding['artifact_sha256'],'file_bytes':binding['file_bytes'],
        'metadata_sha256':binding['metadata_sha256'],'binding_sha256':digest(binding),
        'cover_sha256':proof['checks']['cover']['output']['sha256'],'cover_provenance_sha256':proof['checks']['cover']['provenance']['sha256'],
        'upload_reference_sha256':state['upload_observation']['upload_reference_sha256'],
        'title':binding['metadata']['title'],'description':binding['metadata']['description'],'creation_statement':'内容为转载',
        'source':binding['source_url'],'category':binding['metadata']['category_label'],'tags':binding['metadata']['tags'],
        'metadata_verified':True,'cover_applied':True,'server_file_hash_verified':False,'human_review':False,
        'per_artifact_owner_acceptance':False,'observed_at':datetime.now(timezone.utc).isoformat()}
    folder=s.root/'media'/job/'replay-browser'/attempt;folder.mkdir(parents=True)
    # An explicitly owned screenshot-shaped byte fixture; actual Chromium
    # screenshots and real media/cover checks have their own integration suites.
    screenshot=folder/'pre-submit.png';screenshot.write_bytes(b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'+(100).to_bytes(4,'big')+(100).to_bytes(4,'big')+bytes(9))
    inner=folder/'native-preparation-observation.json';inner.write_text(encode({'version':'replay-native-form-observation-v1',**common}),'utf-8')
    def ref(file):raw=file.read_bytes();return {'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    receipt={'version':'replay-native-preparation-v1',**common,'pre_submit_screenshot':ref(screenshot),
        'native_observation_file':ref(inner),'private_draft_saved':True}
    if record:service.record_native_prepare(PRINCIPAL,job,receipt)
    return receipt


def test_seven_hour_source_is_accepted_but_short_limits_and_legacy_constants_are_unchanged(replay,short_video):
    s,db,service,source,_,_,_=replay
    assert service._source(source)[1]['duration']==25715
    with pytest.raises(HubError,match='SHORT_VIDEO_VERIFIED_DOWNLOAD_REQUIRED'):short_video[2]._source(source)
    job=candidate(replay);assert candidate(replay)==job
    current,binding,state=service.snapshot(job)
    assert current['job_type']==JOB_TYPE and current['state']=='awaiting_review' and current['attempts']==0
    assert binding['human_review'] is state['human_review'] is state['public_submission_approved'] is False
    assert state['version']==service.VERSION and 'short_video_web_v1' not in current['result']
    assert db.claim('video',180,4) is None and not db.query('SELECT * FROM publications')
    assert short_video[2].MAX_BYTES==100_000_000 and short_video[2].MAX_SECONDS==600
    assert service.MAX_BYTES==3_800_000_000 and service.MAX_SECONDS==86400


@pytest.mark.parametrize('key,value',[('target_site_uid','other'),('target_account_name','other'),('source_scope','all'),
    ('allowed_channel_ids',[CHANNELS[0]]),('acceptance_video_ids',[]),('workflow_authorization_reference',''),
    ('automation_enabled','true'),('public_submission_mode','per_artifact_owner_acceptance')])
def test_policy_identity_scope_and_permissions_fail_closed(replay,key,value):
    replay[6][key]=value;write_policy(replay[5],replay[6])
    with pytest.raises(HubError,match='REPLAY_VIDEO_POLICY_REQUIRED'):candidate(replay)
    assert not replay[1].query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))


@pytest.mark.parametrize('change',['wrong_channel','missing_official','missing_end','wrong_raw_id','nonpublic','before_activation'])
def test_scope_uses_official_actual_end_and_two_fixed_channels(replay,short_video,change):
    if change=='wrong_channel':update_source(short_video,channel='OwnedOtherChannel')
    else:
        if change=='before_activation':
            replay[6].update(source_scope='automatic_new_only',acceptance_video_ids=[]);write_policy(replay[5],replay[6])
            update_source(short_video,end='2026-10-31T22:59:59Z')
        else:
            db=replay[1];job=db.get(replay[3]);payload=job['payload']
            if change=='missing_official':payload['source_api']='owned-nonofficial'
            elif change=='missing_end':payload['raw']['liveStreamingDetails'].pop('actualEndTime')
            elif change=='wrong_raw_id':payload['raw']['id']='zyxwvutsrqp'
            else:payload['raw']['status']['privacyStatus']='private'
            checkpoint=job['result'];checkpoint[DOWNLOAD_STATE]['binding']['source_payload_sha256']=digest(payload)
            state=checkpoint[DOWNLOAD_STATE];file=Path(state['source_asset']['file']).parent/'verified.json'
            receipt=json.loads(file.read_bytes());receipt['binding_sha256']=digest(state['binding']);file.write_text(encode(receipt),'utf-8')
            with db.transaction() as connection:connection.execute('UPDATE jobs SET payload=?,result=? WHERE job_id=?',(encode(payload),encode(checkpoint),job['job_id']))
    with pytest.raises(HubError):candidate(replay)
    assert not replay[1].query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,))


def test_new_only_uses_end_time_even_when_published_on_previous_day_and_is_idempotent(replay):
    replay[6].update(source_scope='automatic_new_only',acceptance_video_ids=[]);write_policy(replay[5],replay[6])
    payload=replay[1].get(replay[3])['payload']
    assert payload['published_at']<replay[6]['activated_at']<payload['raw']['liveStreamingDetails']['actualEndTime']
    job=candidate(replay)
    assert candidate(replay)==job and len(replay[1].query('SELECT * FROM jobs WHERE job_type=?',(JOB_TYPE,)))==1
    assert replay[2].policy()['automation_enabled'] is False


def test_public_description_cannot_reintroduce_license_number(replay):
    replay[4]['description']+='\n转载许可编号：114514'
    with pytest.raises(HubError,match='REPLAY_VIDEO_PUBLIC_PERMISSION_LINE_FORBIDDEN'):candidate(replay)


def test_workflow_grant_does_not_forge_old_artifact_approval_or_open_default_gate(replay):
    job,attempt,binding=uploaded(replay);authorize(replay,job)
    with pytest.raises(HubError,match='REPLAY_MACHINE_'):reserve(replay,job,attempt)
    with pytest.raises(HubError,match='REPLAY_VIDEO_USE_SEPARATE_WORKFLOW_AUTHORIZATION'):
        replay[2].approve(PRINCIPAL,job,binding_sha256=digest(binding),actual_owner_response='Owned only',approval_reference='Owned only')
    state=replay[1].get(job)['result'][STATE]
    assert state['public_submission_approved'] is False and 'owner_acceptance' not in state
    assert state['automation_authorization']['human_review'] is False
    assert state['automation_authorization']['per_artifact_owner_acceptance'] is False
    assert not replay[1].query('SELECT * FROM publications')


def test_default_real_verifier_rejects_synthetic_source_instead_of_being_unconnected(replay):
    enable_before_prepare(replay);job,attempt,_=uploaded(replay);authorize(replay,job)
    from services.hub.replay_machine_checks import ReplayMachineChecks
    assert isinstance(replay[2].machine_acceptance_verifier,ReplayMachineChecks)
    with pytest.raises(HubError,match='REPLAY_MACHINE_'):reserve(replay,job,attempt,machine_acceptance_receipt_sha256='f'*64)
    assert not replay[1].query('SELECT * FROM publications')


def test_owned_verified_reservation_is_one_use_and_unknown_never_reuploads_or_reapproves(replay,monkeypatch):
    enable_before_prepare(replay);job,attempt,binding=uploaded(replay);authorize(replay,job)
    file,_,verifier=owned_proof(replay,job,monkeypatch);native_prepare(replay,job,attempt);before_calls=verifier.calls
    answer=reserve(replay,job,attempt)
    assert answer['can_click_submit'] is True and verifier.calls==before_calls+1
    state=replay[1].get(job)['result'][STATE]
    assert state['state']=='submitting' and state['public_submission_approved'] is False and 'owner_acceptance' not in state
    proofsha=state['machine_acceptance']['evidence_sha256']
    assert hashlib.sha256(Path(state['machine_acceptance']['evidence_file']).read_bytes()).hexdigest()==proofsha
    replay[2].mark_unknown(PRINCIPAL,job);before=replay[1].get(job)
    with pytest.raises(HubError):reserve(replay,job,attempt,machine_acceptance_receipt_sha256=proofsha)
    with pytest.raises(HubError):replay[2].start_upload(PRINCIPAL,job,attempt_id=str(uuid.uuid4()),site_uid='123456')
    assert verifier.calls==before_calls+1 and replay[1].get(job)==before
    assert before['state']=='publish_unknown' and before['result'][STATE]['state']=='submit_unknown'
    publications=replay[1].query('SELECT * FROM publications')
    assert len(publications)==1 and publications[0]['state']=='unknown' and publications[0]['kind']==KIND
    assert replay[1].query("SELECT COUNT(*) n FROM audit WHERE target=? AND outcome='uploading'",(job,))[0]['n']==1


@pytest.mark.parametrize('tamper',['wrong_binding','wrong_uid','wrong_attempt','evidence_changed','boolean_verifier',
    'numeric_review_flag','numeric_acceptance_flag'])
def test_hash_bound_machine_evidence_cannot_be_replaced_by_flags_or_other_jobs(replay,monkeypatch,tamper):
    enable_before_prepare(replay);job,attempt,_=uploaded(replay);authorize(replay,job)
    file,proof,_=owned_proof(replay,job,monkeypatch)
    if tamper=='wrong_binding':proof['binding_sha256']='0'*64
    elif tamper=='wrong_uid':proof['site_uid']='other'
    elif tamper=='wrong_attempt':proof['attempt_id']=str(uuid.uuid4())
    elif tamper=='evidence_changed':(file.parent/'native-observation.json').write_bytes(b'{"passed":true}')
    elif tamper=='numeric_review_flag':proof['human_review']=0
    elif tamper=='numeric_acceptance_flag':proof['per_artifact_owner_acceptance']=0
    else:
        class BooleanVerifier:
            def verify(self,*args,**kwargs):return True
        replay[2].machine_acceptance_verifier=BooleanVerifier()
    file.write_text(encode(proof),'utf-8');proofsha=hashlib.sha256(file.read_bytes()).hexdigest()
    with pytest.raises(HubError,match='REPLAY_MACHINE_ACCEPTANCE_REQUIRED'):reserve(replay,job,attempt)
    assert not replay[1].query('SELECT * FROM publications')


def test_any_source_publication_across_kind_blocks_new_replay_candidate(replay):
    db=replay[1];source=db.get(replay[3])
    with db.transaction() as connection:
        connection.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('e'*64,source['job_id'],'bilibili','youtube',source['source_id'],'site_uid:123456','different_official_or_website_kind','unknown',None,None,1))
    with pytest.raises(HubError,match='SHORT_VIDEO_EXISTING_PUBLICATION_REQUIRES_LOOKUP'):candidate(replay)


def test_reused_result_and_cleanup_rules_require_causal_bv_and_public_lookup(replay,monkeypatch):
    replay[6].update(source_scope='automatic_new_only',acceptance_video_ids=[]);write_policy(replay[5],replay[6])
    enable_before_prepare(replay);job,attempt,binding=uploaded(replay);authorize(replay,job)
    file,_,_=owned_proof(replay,job,monkeypatch)
    native_prepare(replay,job,attempt)
    # This case isolates the shared causal result/cleanup rules. The separate
    # launch-grant tests exercise the global root acceptance gate.
    monkeypatch.setattr(replay[2],'_require_workflow_launch',lambda policy:None)
    reserve(replay,job,attempt)
    replay[2].record_result(PRINCIPAL,job,short_result(attempt,binding))
    assert replay[2].cleanup_eligibility(job)['eligible'] is False
    observation={**short_result(attempt,binding,'completed'),'public_accessible':True,
        'public_lookup_observed_at':datetime.now(timezone.utc).isoformat()}
    replay[2].record_result(PRINCIPAL,job,observation,lookup=True)
    eligibility=replay[2].cleanup_eligibility(job)
    assert eligibility['eligible'] is True and eligibility['delete_performed'] is False
    assert Path(binding['file']).is_file()


def test_completed_pilot_remains_retained_without_remote_or_local_deletion(replay,monkeypatch):
    enable_before_prepare(replay);job,attempt,binding=uploaded(replay);authorize(replay,job)
    file,_,_=owned_proof(replay,job,monkeypatch)
    native_prepare(replay,job,attempt)
    reserve(replay,job,attempt)
    replay[2].record_result(PRINCIPAL,job,short_result(attempt,binding))
    replay[2].record_result(PRINCIPAL,job,{**short_result(attempt,binding,'completed'),'public_accessible':True,
        'public_lookup_observed_at':datetime.now(timezone.utc).isoformat()},lookup=True)
    answer=replay[2].cleanup_eligibility(job)
    assert answer=={'eligible':False,'reason':'PILOT_TESTS_RETAINED_UNTIL_FORMAL_WORKFLOW_LAUNCH','delete_performed':False}
    assert Path(binding['file']).is_file()
