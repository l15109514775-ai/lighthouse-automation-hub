"""Owned orchestration protocol only: no child process, network or deletion."""
from copy import deepcopy
import pytest

from test_cloudbase_backup_daily_v1 import flow,d
from scripts.ops import cloudbase_archive_registration_hook_v2 as hook


def execute(flow,monkeypatch,*,registrar=None,previous=None,runner=None):
    root,report,status,dry,applied,calls,saved,ordinary=flow
    monkeypatch.setattr(hook,'daily',d)
    return hook.execute(previous,registrar=registrar,runner=runner or ordinary,
        saver=lambda record:saved.append(deepcopy(record)),validator=lambda _: '1'*64)


def registered(reference):
    return {'version':'archive-source-retention-v2','state':'archive_locations_registered','archive_sha256':'2'*64,
        'operation_id':reference['operation_id'],'acceptance_id':reference['acceptance_id'],
        'report_sha256':reference['report_sha256'],'mapped_files':7,'external_requests':0,'source_files_deleted':0}


def test_registration_is_before_first_clone_prune_and_resume_is_verification_only(flow,monkeypatch):
    calls=flow[5];seen=[]
    def registrar(reference,*,verify_only):
        seen.append((verify_only,[args[-1] for _,args in calls]));return registered(reference)
    result=execute(flow,monkeypatch,registrar=registrar)
    assert seen==[(False,['--accept']),(True,['--accept','--dry-run'])]
    assert result['state']=='completed' and result['archive_source_registration_v2']['mapped_files']==7
    assert [args[-1] for _,args in calls]==['--accept','--dry-run','--apply']


def test_registration_failure_preserves_report_and_never_calls_prune(flow,monkeypatch):
    def failed(reference,*,verify_only):raise RuntimeError('owned unverified archive')
    with pytest.raises(RuntimeError):execute(flow,monkeypatch,registrar=failed)
    assert [args[-1] for _,args in flow[5]]==['--accept'] and flow[6][-1]['state']=='prune_failed'


def test_interrupted_prune_resumes_from_registered_index_without_second_get(flow,monkeypatch):
    ordinary=flow[7];seen=[]
    def registrar(reference,*,verify_only):seen.append(verify_only);return registered(reference)
    def failed(script,args,timeout):
        if args[-1]=='--dry-run':raise RuntimeError('owned interruption after registration')
        return ordinary(script,args,timeout)
    with pytest.raises(RuntimeError):execute(flow,monkeypatch,registrar=registrar,runner=failed)
    previous=flow[6][-1];flow[5].clear()
    assert previous['archive_source_registration_v2']['mapped_files']==7
    assert execute(flow,monkeypatch,registrar=registrar,previous=previous)['state']=='completed'
    assert seen==[False,True,True] and [args[-1] for _,args in flow[5]]==['--dry-run','--apply']


def test_changed_registration_or_extra_get_claim_never_reaches_apply(flow,monkeypatch):
    seen=[]
    def registrar(reference,*,verify_only):
        seen.append(verify_only);value=registered(reference)
        if verify_only:value['archive_sha256']='3'*64
        return value
    with pytest.raises(RuntimeError):execute(flow,monkeypatch,registrar=registrar)
    assert [args[-1] for _,args in flow[5]]==['--accept','--dry-run']


def test_unknown_previous_acceptance_does_not_register_or_start_new_upload(flow,monkeypatch):
    seen=[]
    with pytest.raises(RuntimeError):execute(flow,monkeypatch,registrar=lambda *a,**k:seen.append(a),previous={'state':'accept_failed'})
    assert seen==flow[5]==[]
