"""Owned SQLite repair/CAS boundaries, not source QA or real native evidence.

Only snapshot/source-policy and the original native-reference SHA pins are
explicit owned seams. The inherited V5 full-row CAS and _update remain real.
No browser, SSH, platform request or production operator main is executed.
"""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
import importlib.util
import json,time

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_parts_v5 import ReplayPartGroupsV5,ReplayPartVideoWeb

path=Path(__file__).resolve().parents[2]/'scripts/ops/repair_unconsumed_part_upload_v5.py'
spec=importlib.util.spec_from_file_location('owned_unconsumed_repair',path)
op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)


@pytest.fixture
def repair_case(runtime,monkeypatch):
    settings,store=runtime
    source=settings.root/'media'/op.DOWNLOAD/'youtube-whole-v4/original.mp4'
    source.parent.mkdir(parents=True);source.write_bytes(b'owned-size-boundary-only-not-a-real-MP4')
    binding={'parent_job_id':op.PARENT,'download_job_id':op.DOWNLOAD,'part_index':0,'part_count':1,
        'site_uid':op.UID,'source_id':op.SOURCE,'file':str(source),'file_bytes':source.stat().st_size}
    policy={'workflow_authorization_reference':'owned-upstream-workflow-only'}
    state={'version':'replay-part-website-acceptance-v5','state':'upload_unknown','attempt_id':op.ATTEMPT,
        'private_upload_selections':1,'public_submission_approved':False,'human_review':False,
        'owned_checkpoint_preserved':{'nested':['keep',1]},
        'automation_authorization':{'version':ReplayPartVideoWeb.WORKFLOW_AUTHORIZATION_VERSION,
            'binding_sha256':op.digest(binding),'workflow_authorization_reference':policy['workflow_authorization_reference'],
            'human_review':False,'per_artifact_owner_acceptance':False}}
    result={op.STATE:state,'publish_performed':False,'owned_other_result':{'keep':True}}
    ReplayPartGroupsV5(settings,store).initialize()
    now=time.time()
    with store.transaction() as db:
        for ident,kind,jobstate,payload,version in (
            (op.DOWNLOAD,'youtube_replay_download_v4','completed',{},'replay-fifo-v4'),
            (op.PARENT,'replay_parts_publish_parent_v5','completed',{},'replay-part-video-policy-v5'),
            (op.CHILD,'replay_part_publish_v5_p01','awaiting_review',binding,'replay-part-video-policy-v5')):
            db.execute('INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (ident,'youtube',op.SOURCE,kind,'video',jobstate,now,now,version,encode(payload)))
        db.execute('UPDATE jobs SET result=?,stage=?,error_code=?,attempts=1 WHERE job_id=?',
            (encode(result),'short_video_reconcile_required',op.ERROR,op.CHILD))
        db.execute("UPDATE pipelines SET paused=1 WHERE name='video'")
        db.execute('INSERT INTO replay_part_groups_v5 VALUES(?,?,?,?,?,?,?,?,?,?)',
            (op.PARENT,op.SOURCE,op.UID,op.DOWNLOAD,'a'*64,'b'*64,1,'prepared',now,now))
        db.execute('INSERT INTO replay_part_children_v5 VALUES(?,?,?,?,?,?,?)',
            (op.PARENT,0,op.CHILD,'owned-part-key','c'*64,'prepared',None))
    class OwnedSourceBoundary(ReplayPartVideoWeb):
        def snapshot(self,job_id,*,write=False):
            assert job_id==op.CHILD and write is False
            row=self.store.get(job_id)
            return row,row['payload'],row['result'][op.STATE]
        def _validate_snapshot_binding(self,job,binding,*,verify_artifact_hash):
            assert verify_artifact_hash is False and job['job_id']==op.CHILD
            return policy
        def _require_workflow_launch(self,actual):
            assert actual==policy
    service=OwnedSourceBoundary(settings,store)
    folder=settings.root/'media'/op.CHILD/service.NATIVE_DIRECTORY/op.ATTEMPT
    folder.mkdir(parents=True)
    error={'action':'upload','error_code':op.ERROR,'publication_result_unknown':False,'video_selection_attempted_in_session':False}
    (folder/'last-error.json').write_bytes(op.wire(error))
    (folder/'error-upload-inventory.json').write_bytes(op.wire({'owned_inventory_only':True}))
    monkeypatch.setattr(op,'ERROR_SHA',op.reference(folder/'last-error.json')['sha256'])
    monkeypatch.setattr(op,'INVENTORY_SHA',op.reference(folder/'error-upload-inventory.json')['sha256'])
    clean=settings.root/'media'/op.CHILD/'owned-clean-entry.json'
    clean_body={'version':'replay-native-clean-upload-entry-v1','observed_at':datetime.now(timezone.utc).isoformat(),
        'site_uid':op.UID,'url':'https://member.bilibili.com/platform/upload/video/frame',
        'file_selections':0,'metadata_edits':0,'save_clicks':0,'submit_clicks':0,'all_clicks':0,'url_query_keys':[],
        'counts':{'upload_area':1,'continue':0,'discard':0,'file_rows':0,'title_forms':0,'outer_tasks':0,'dialogs':0},
        'protection':{'blocked':False,'evidence':{'version':'bilibili-video-protection-guard-v3','truncated':False,
            'top_level_authentication':False,'visible_dialogs':[],'visible_challenge_controls':[],'challenge_frames':[]}},
        'native':{'url':'https://member.bilibili.com/platform/upload/video/frame','total_file_item_count':0,
            'visible_file_item_count':0,'body_text':'Owned synthetic clean upload boundary',
            'uploaders':[{'visible':True,'accept':'.mp4','files':[]}]},'owned_native_boundary_only':True}
    clean.write_bytes(op.wire(clean_body))
    request={'version':'unconsumed-part-upload-repair-request-v1','job_id':op.CHILD,
        'before_row_sha256':op.digest(store.get(op.CHILD)),'clean_entry':op.reference(clean),'operator_reference':'owned-test-no-real-operator'}
    return SimpleNamespace(s=settings,db=store,service=service,request=request,clean=clean,clean_body=clean_body,folder=folder)


def tables(f):
    return {name:f.db.query('SELECT * FROM '+name) for name in ('jobs','publications','audit','replay_part_children_v5')}


def test_default_plan_writes_no_database_or_repair_archive(repair_case):
    f=repair_case;before=tables(f)
    result=op.repair(f.service,f.request)
    assert result['state']=='verified_plan_only' and result['plan']['public_submissions']==0
    assert tables(f)==before and not (f.s.root/'media'/op.CHILD/'preselection-repair-v1').exists()


def test_apply_real_update_preserves_attempt_and_checkpoint_then_ordinary_claim_is_two(repair_case):
    f=repair_case;before=f.db.get(op.CHILD);old=deepcopy(before['result'][op.STATE])
    plan=op.repair(f.service,f.request)
    result=op.repair(f.service,f.request,apply=True,expected_plan_sha256=plan['plan_sha256'])
    after=f.db.get(op.CHILD);state=after['result'][op.STATE];marker=state['preselection_repair_v1']
    assert result['attempts']==after['attempts']==1 and after['state']=='pending'
    assert state['state']=='prepared' and state['attempt_id']==old['attempt_id']==op.ATTEMPT
    assert state['private_upload_selections']==0 and marker['old_checkpoint']==old
    assert after['payload']==before['payload'] and after['result']['owned_other_result']==before['result']['owned_other_result']
    for ref in marker['immutable_originals'].values():assert op.reference(Path(ref['file']))==ref
    assert json.loads(Path(marker['immutable_originals']['original-job.json']['file']).read_bytes())==before
    assert marker['original_error_evidence']['last_error']['file']!=str(f.folder/'last-error.json')
    assert not f.db.query('SELECT * FROM publications')
    with f.db.transaction() as db:db.execute("UPDATE pipelines SET paused=0 WHERE name='video'")
    claimed=f.db.claim('video',120,4)
    assert claimed['job_id']==op.CHILD and claimed['attempts']==2 and claimed['lease_owner']
    assert claimed['result'][op.STATE]['attempt_id']==op.ATTEMPT


def test_any_selection_intent_or_existing_publication_refuses_repair(repair_case):
    f=repair_case;intent=f.s.root/'media'/op.CHILD/'replay-native-intents/video-selection.json'
    intent.parent.mkdir();intent.write_bytes(b'owned-consumed-intent')
    with pytest.raises(ValueError,match='REPAIR_SELECTION_INTENT_EXISTS'):op.repair(f.service,f.request)
    intent.unlink()  # Only this owned fixture's synthetic marker.
    with f.db.transaction() as db:
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('owned-key',op.CHILD,'bilibili','youtube',op.SOURCE,
            'site_uid:'+op.UID,'video_website_replay_part_v5_p01','unknown',None,None,time.time()))
    with pytest.raises(ValueError,match='REPAIR_QUIET_PAUSED_SOURCE_REQUIRED'):op.repair(f.service,f.request)
    assert f.db.get(op.CHILD)['state']=='awaiting_review'


def test_expired_clean_entry_is_not_a_retry_authority(repair_case):
    f=repair_case;f.clean_body['observed_at']=(datetime.now(timezone.utc)-timedelta(seconds=600)).isoformat()
    f.clean.write_bytes(op.wire(f.clean_body));f.request['clean_entry']=op.reference(f.clean);before=tables(f)
    with pytest.raises(ValueError,match='REPAIR_CLEAN_ENTRY_EXPIRED'):op.repair(f.service,f.request)
    assert tables(f)==before


def test_real_full_row_cas_rejects_change_between_validation_and_update(repair_case,monkeypatch):
    f=repair_case;plan=op.repair(f.service,f.request);actual=f.service._update
    def raced(*args,**kwargs):
        changed={**f.db.get(op.CHILD)['payload'],'owned_concurrent_change':True}
        with f.db.transaction() as db:db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(changed),op.CHILD))
        return actual(*args,**kwargs)
    monkeypatch.setattr(f.service,'_update',raced)
    with pytest.raises(HubError,match='SHORT_VIDEO_JOB_CHANGED'):
        op.repair(f.service,f.request,apply=True,expected_plan_sha256=plan['plan_sha256'])
    current=f.db.get(op.CHILD)
    assert current['state']=='awaiting_review' and current['attempts']==1
    assert current['payload']['owned_concurrent_change'] is True
    assert 'preselection_repair_v1' not in current['result'][op.STATE]


def test_second_apply_cannot_consume_repair_again(repair_case):
    f=repair_case;plan=op.repair(f.service,f.request)
    op.repair(f.service,f.request,apply=True,expected_plan_sha256=plan['plan_sha256'])
    before=tables(f)
    with pytest.raises(ValueError,match='REPAIR_ROW_CHANGED'):
        op.repair(f.service,f.request,apply=True,expected_plan_sha256=plan['plan_sha256'])
    assert tables(f)==before
