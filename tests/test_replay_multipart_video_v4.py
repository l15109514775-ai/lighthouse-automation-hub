"""Qualified owned V4 files/packet checkpoints; no real account or platform."""
from copy import deepcopy
import json
import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_multipart_video_v4 import MultipartReplayVideoWeb, POLICY, STATE, wire_seconds
from services.hub.replay_natural_video_v3 import natural_metadata
from services.hub.short_video_web import PRINCIPAL
from test_replay_fifo_v4 import fifo
from test_youtube_download_v4 import whole, toml
from test_ytdlp_highest_v2 import tracks


@pytest.fixture
def publisher(whole):
    f=whole
    result=f.service.process(f.job)
    f.db.finish(f.job,'completed',result=result)
    f.s.config['target_bilibili_account_name']='Owned synthetic multipart publisher'
    f.publish=MultipartReplayVideoWeb(f.s,f.db)
    p={'version':POLICY,'enabled':True,'target_site_uid':f.s.config['target_bilibili_site_uid'],
        'target_account_name':f.s.config['target_bilibili_account_name'],'public_submission_mode':f.publish.PUBLIC_MODE,
        'source_scope':'pilot_single','acceptance_video_ids':[f.raw['id']],'allowed_channel_ids':f.p['allowed_channel_ids'],
        'source_not_before':f.p['source_not_before'],'activated_at':f.p['activated_at'],'automation_enabled':False,
        'owner_authorization_reference':'Owned synthetic owner scope; no actual authorization',
        'owner_permission_reference':'Owned synthetic files; no live rights',
        'workflow_authorization_reference':'Owned workflow reference',
        'actual_owner_workflow_response':'Owned fixture only, not a user response','owner_response_reference':'Owned test conversation'}
    (f.s.root/'config'/f.publish.POLICY_FILE).write_text(toml(p),encoding='utf-8')
    (f.s.root/'config'/f.publish.POLICY_FILE).chmod(0o644)
    f.publish_policy=p
    return f


def test_actual_whole_files_and_persisted_packet_chain_prepare_one_source_bound_candidate(publisher):
    f=publisher
    source=f.db.get(f.job['job_id']);meta=natural_metadata(f.s,source)
    ident=f.publish.create_candidate(PRINCIPAL,source['job_id'],meta)
    current=f.db.get(ident)
    assert current['state']=='awaiting_review' and current['attempts']==0
    assert current['payload']['whole_plan_sha256']==source['payload']['whole_replay_plan_sha256']
    assert current['payload']['parts'] and current['payload']['parts'][0]['part_index']==0
    assert current['result'][STATE]['public_submission_approved'] is False
    assert f.publish.create_candidate(PRINCIPAL,source['job_id'],meta)==ident
    assert not f.db.query('SELECT * FROM publications')


def test_pilot_queue_remains_closed_without_current_native_creation_capability(publisher):
    f=publisher
    with pytest.raises(HubError,match='NATIVE_CREATION_CAPABILITY_REQUIRED'):
        f.publish.queue_verified_download(f.job['job_id'])
    assert not f.db.query('SELECT * FROM jobs WHERE job_type=?',(f.publish.JOB_TYPE,))
    assert not f.db.query('SELECT * FROM publications')


def test_current_part_mutation_and_forged_packet_chain_are_rejected(publisher):
    f=publisher
    source=f.db.get(f.job['job_id']);part=source['result']['youtube_download_v4']['parts']['parts'][0]
    from pathlib import Path
    file=Path(part['file']);original=file.read_bytes();file.write_bytes(original+b'changed')
    with pytest.raises(HubError):f.publish._source(f.job['job_id'])
    file.write_bytes(original)
    manifest=Path(source['result']['youtube_download_v4']['parts']['manifest'])
    body=json.loads(manifest.read_bytes());body['parts'][0]['packet_after']['video']['chain']='a'*64
    manifest.write_text(encode(body),encoding='utf-8')
    with pytest.raises(HubError):f.publish._source(f.job['job_id'])


def test_wire_duration_numbers_keep_value_and_match_javascript_canonical_integers():
    assert wire_seconds(3.0)==3 and type(wire_seconds(3.0)) is int
    assert wire_seconds(10.519336)==10.519336 and type(wire_seconds(10.519336)) is float
    from services.hub.youtube_download import digest
    assert digest({'duration_seconds':wire_seconds(3.0)})==digest({'duration_seconds':3})
