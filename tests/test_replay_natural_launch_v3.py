"""Owned immutable-launch evidence; no real authorization is issued."""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from pathlib import Path
import hashlib,json,uuid
import pytest
from test_short_video_web import short_video
from test_replay_highest_video_v2 import highest
from test_replay_natural_video_v3 import natural
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_highest_video_v2 import HighestReplayVideoWeb
from services.hub.replay_natural_launch_v3 import issue_launch
from services.hub.youtube_download import digest


def test_later_player_refresh_does_not_revoke_immutable_root_acceptance(natural):
    f=natural;pilot=f['db'].get(f['grant']['acceptance_job_id']);state=pilot['result'][HighestReplayVideoWeb.STATE]
    original=deepcopy(f['launch_snapshot']);state['result_observation']['public_lookup_observed_at']='2099-01-01T00:00:00Z'
    state['cleanup_coverage']['public_player']['observed_at']='2099-01-01T00:00:00Z'
    with f['db'].transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(pilot['result']),pilot['job_id']))
    assert f['service']._require_workflow_launch(f['service'].policy())==f['grant']
    assert f['launch_snapshot']==original
    with f['db'].transaction() as db:db.execute("UPDATE publications SET external_id='BV9999999999' WHERE job_id=?",(pilot['job_id'],))
    with pytest.raises(HubError,match='LAUNCH_ACCEPTANCE'):f['service']._require_workflow_launch(f['service'].policy())


def test_altered_snapshot_or_stale_at_acceptance_is_rejected(natural):
    f=natural;snapshot=f['launch_snapshot'];snapshot['cleanup_coverage']['public_player']['observed_at']='2020-01-01T00:00:00Z'
    with pytest.raises(HubError,match='LAUNCH_ACCEPTANCE'):f['service']._require_workflow_launch(f['service'].policy())
    f['grant']['pilot_acceptance_snapshot_sha256']=digest(snapshot)
    f['grant']['pilot_cleanup_coverage_sha256']=digest(snapshot['cleanup_coverage'])
    with pytest.raises(HubError,match='LAUNCH_ACCEPTANCE'):f['service']._require_workflow_launch(f['service'].policy())


def test_issuer_reopens_real_proof_bytes_and_returns_bytes_without_installing(natural,monkeypatch):
    f=natural;pilot=f['db'].get(f['grant']['acceptance_job_id']);state=pilot['result'][HighestReplayVideoWeb.STATE]
    proof=state['machine_acceptance'];proof.pop('evidence_sha256')
    folder=f['s'].root/'media'/pilot['job_id']/HighestReplayVideoWeb.MACHINE_PROOF_DIRECTORY/str(uuid.uuid4())
    folder.mkdir(parents=True);file=folder/'receipt.json';file.write_text(encode(proof),'utf-8')
    proof.update(evidence_file=str(file),evidence_bytes=file.stat().st_size,evidence_sha256=hashlib.sha256(file.read_bytes()).hexdigest())
    with f['db'].transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(pilot['result']),pilot['job_id']))
    calls=[]
    def checked_summary(self,ident):
        calls.append(ident);return {'coverage':deepcopy(state['cleanup_coverage'])}
    monkeypatch.setattr(HighestReplayVideoWeb,'cleanup_coverage_summary',checked_summary)
    result=issue_launch(f['service'],pilot['job_id'],actual_owner_launch_response='Owned explicit workflow only',owner_response_reference='Owned fixture')
    assert calls==[pilot['job_id']] and result['files_written'] is result['publication_authorized'] is False
    grant=json.loads(result['grant_bytes']);snapshot=json.loads(result['snapshot_bytes'])
    assert hashlib.sha256(result['snapshot_bytes']).hexdigest()==grant['pilot_acceptance_snapshot_sha256']
    assert snapshot['machine_acceptance']['human_review'] is False
    file.write_bytes(file.read_bytes()+b' ')
    with pytest.raises(HubError,match='LAUNCH_ACCEPTANCE'):issue_launch(f['service'],pilot['job_id'],actual_owner_launch_response='Owned only',owner_response_reference='Owned fixture')
