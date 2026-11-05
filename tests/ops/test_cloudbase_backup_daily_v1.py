"""Owned pure orchestration fixtures; all child commands are mocks, no deletion/network."""
import copy,importlib.util,uuid
from pathlib import Path
import pytest
path=Path(__file__).resolve().parents[2]/'scripts/ops/cloudbase_backup_daily_v1.py';spec=importlib.util.spec_from_file_location('daily',path);d=importlib.util.module_from_spec(spec);spec.loader.exec_module(d)
@pytest.fixture
def flow(tmp_path,monkeypatch):
    root=tmp_path/'hub';root.mkdir();accept=str(uuid.uuid4());op=str(uuid.uuid4());report=root/'backups/cloudbase-restore-check'/accept/'restoration-report.json'
    report.parent.mkdir(parents=True)
    original=d.accept_reference;monkeypatch.setattr(d,'ROOT',root);monkeypatch.setattr(d,'accept_reference',lambda value:original(value,root))
    status={'version':'cloudbase-backup-host-service-v1e','mode':'accept','exit_code':0,'image':d.IMAGE,'uid':10001,'offline':False,'credentials_mounted':True,'core_service_images_changed':False,
       'result':{'state':'remote_archive_restored_and_verified','operation_id':op,'sqlite_integrity':'ok','sqlite_foreign_keys':'ok','additional_remote_gets':0,'local_deletions':0,'stage_prune_candidates':3,'report_file':str(report)}}
    dry={'state':'dry_run_verified','apply':False,'deleted_files':0,'remote_requests':0,'historical_raw_backups_touched':False,'business_sources_touched':False,
        'journal_file':str(root/'backups/cloudbase-prune'/(accept+'.intent.json')),'candidate_files':8,'candidate_bytes':40960}
    applied={**dry,'state':'completed','apply':True,'deleted_files':8};calls=[];saved=[]
    def runner(script,args,timeout):
        calls.append((script,args))
        if args==['--accept']:return copy.deepcopy(status)
        if args[-1]=='--dry-run':return copy.deepcopy(dry)
        assert args==['--report',str(report),'--apply'];return copy.deepcopy(applied)
    return root,report,status,dry,applied,calls,saved,runner

def test_accept_then_validated_dry_run_then_exact_apply_with_no_extra_get(flow):
    root,report,status,dry,applied,calls,saved,runner=flow
    result=d.execute(runner=runner,saver=lambda x:saved.append(copy.deepcopy(x)),validator=lambda _: '1'*64)
    assert result['state']=='completed' and result['pruned_files']==8
    assert [a[-1] for _,a in calls]==['--accept','--dry-run','--apply']
    assert [x['state'] for x in saved]==['accept_started','accepted_pending_prune','prune_dry_run_started','prune_started','completed']
    assert report.parent.is_dir() # No mock ever deletes anything.

@pytest.mark.parametrize('field,value',[('uid',0),('image','sha256:'+'0'*64),('offline',True),('credentials_mounted',False)])
def test_invalid_accept_context_never_prunes(flow,field,value):
    root,report,status,dry,applied,calls,saved,runner=flow;status[field]=value
    with pytest.raises(AssertionError):d.execute(runner=runner,saver=lambda x:saved.append(copy.deepcopy(x)),validator=lambda _: '1'*64)
    assert [a for _,a in calls]==[['--accept']] and saved[-1]['state']=='accept_failed'

def test_untrusted_report_path_outside_exact_uuid_area_is_rejected(flow):
    root,report,status,*_=flow;status['result']['report_file']=str(root/'data/hub.sqlite3')
    with pytest.raises(AssertionError):d.accept_reference(status)

def test_dry_run_failure_preserves_pending_report_and_resume_does_not_accept_again(flow):
    root,report,status,dry,applied,calls,saved,runner=flow
    def failure(script,args,timeout):
        if args[-1]=='--dry-run':raise RuntimeError('owned dry run failure')
        return runner(script,args,timeout)
    with pytest.raises(RuntimeError):d.execute(runner=failure,saver=lambda x:saved.append(copy.deepcopy(x)),validator=lambda _: '1'*64)
    pending=saved[-1];assert pending['state']=='prune_failed' and pending['acceptance']['report_file']==str(report)
    calls.clear();result=d.execute(pending,runner=runner,saver=lambda x:saved.append(copy.deepcopy(x)),validator=lambda _: '1'*64)
    assert result['state']=='completed' and [a[-1] for _,a in calls]==['--dry-run','--apply']

def test_report_changed_between_accept_and_apply_stops_without_apply(flow):
    root,report,status,dry,applied,calls,saved,runner=flow;count=[0]
    def validator(_):count[0]+=1;return ('1' if count[0]<3 else '2')*64
    with pytest.raises(AssertionError):d.execute(runner=runner,saver=lambda x:saved.append(copy.deepcopy(x)),validator=validator)
    assert [a[-1] for _,a in calls]==['--accept','--dry-run'] and saved[-1]['state']=='prune_failed'

@pytest.mark.parametrize('state',['accept_started','accept_failed','reconciliation_required'])
def test_unknown_or_interrupted_accept_never_starts_another_upload(flow,state):
    *_,calls,saved,runner=flow
    with pytest.raises(RuntimeError):d.execute({'state':state},runner=runner,saver=lambda x:saved.append(copy.deepcopy(x)),validator=lambda _: '1'*64)
    assert calls==[]

def test_pruner_incomplete_result_is_not_claimed_complete(flow):
    root,report,status,dry,applied,calls,saved,runner=flow;applied['deleted_files']=7
    with pytest.raises(AssertionError):d.execute(runner=runner,saver=lambda x:saved.append(copy.deepcopy(x)),validator=lambda _: '1'*64)
    assert saved[-1]['state']=='prune_failed'


def test_unknown_valid_json_previous_state_never_authorizes_new_accept(flow):
    *_,calls,saved,runner=flow
    with pytest.raises(AssertionError):d.execute({'state':'unrecognized-state'},runner=runner,saver=lambda x:saved.append(copy.deepcopy(x)),validator=lambda _: '1'*64)
    assert calls==[]


def test_one_time_adoption_stages_real_report_then_service_prunes_without_new_accept(flow):
    root,report,status,dry,applied,calls,saved,runner=flow
    reference=d.accept_reference(status);reference['report_sha256']='1'*64
    record=d.stage_adoption(reference,runner=runner,saver=lambda x:saved.append(copy.deepcopy(x)),validator=lambda _: '1'*64)
    assert record['state']=='accepted_pending_prune' and record['adopted_existing_acceptance'] is True
    assert [a[-1] for _,a in calls]==['--dry-run']
    calls.clear();result=d.execute(record,runner=runner,saver=lambda x:saved.append(copy.deepcopy(x)),validator=lambda _: '1'*64)
    assert result['state']=='completed' and [a[-1] for _,a in calls]==['--dry-run','--apply']


def test_adoption_cannot_overwrite_unresolved_unknown_upload(flow):
    root,report,status,dry,applied,calls,saved,runner=flow
    reference=d.accept_reference(status);reference['report_sha256']='1'*64
    with pytest.raises(AssertionError):d.stage_adoption(reference,{'state':'accept_started'},runner=runner,saver=lambda x:saved.append(copy.deepcopy(x)),validator=lambda _: '1'*64)
    assert calls==[]
