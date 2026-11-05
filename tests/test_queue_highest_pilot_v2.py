from copy import deepcopy
import importlib.util
from pathlib import Path
import pytest
from test_short_video_web import short_video
from test_replay_highest_video_v2 import highest

SPEC=importlib.util.spec_from_file_location('owned_highest_queue',Path(__file__).resolve().parents[1]/'scripts/ops/queue_highest_pilot_v2.py')
q=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(q)


def test_actual_source_date_and_four_full_description_lines(highest):
    f=highest;value=q.plan(f['service'],f['source']);source=f['db'].get(f['source'])
    assert value['metadata']['title']=='【示例主播A】2026-11-01「Owned highest replay」直播回放'
    assert value['metadata']['description']=='示例主播A 2026年11月1日直播回放，日语原声。\n原题：Owned highest replay\n原视频：https://www.youtube.com/watch?v=9T5tn1WGlJg\n原频道：https://www.youtube.com/@example-channel-a'
    assert not value['queue_mutated'] and not value['human_review'] and not value['existing_publish_jobs']
    assert f['db'].get(f['source'])==source


def test_truncated_display_preserves_full_original_and_astral_characters(highest):
    f=highest;source=f['db'].get(f['source']);title='😊日本語'*35;source['payload']['title']=source['payload']['raw']['snippet']['title']=title
    meta=q.metadata(f['s'],source)
    assert len(meta['title'].encode('utf-16-le'))//2<=80 and meta['title'].endswith('…」直播回放')
    assert '\n原题：'+title+'\n' in meta['description']


def test_apply_calls_true_v2_queue_without_manual_review_or_duplicate_reset(highest):
    f=highest;auth={'actual_owner_workflow_response':'Owned explicit workflow only','owner_response_reference':'Owned fixture, no real user authorization'}
    result=q.apply(f['service'],f['source'],auth);job=f['db'].get(result['publish_job_id'])
    state=job['result'][f['service'].STATE]
    assert job['state']=='pending' and job['attempts']==0
    assert state['human_review'] is state['public_submission_approved'] is False and 'owner_acceptance' not in state
    assert q.apply(f['service'],f['source'],auth)['publish_job_id']==job['job_id']
    assert len(f['db'].query('SELECT * FROM jobs WHERE job_type=?',(f['service'].JOB_TYPE,)))==1


def test_before_november_or_missing_actual_workflow_response_is_rejected(highest):
    f=highest;source=f['db'].get(f['source']);source['payload']['raw']['liveStreamingDetails']['actualStartTime']='2026-10-31T15:59:59Z'
    with pytest.raises(ValueError,match='ACTUAL_START'):q.metadata(f['s'],source)
    with pytest.raises(ValueError,match='AUTHORIZATION'):q.apply(f['service'],f['source'],{'actual_owner_workflow_response':'','owner_response_reference':'Owned'})
