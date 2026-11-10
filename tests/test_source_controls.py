import json
from types import SimpleNamespace
import pytest
from services.hub.source_controls import paused_sources,source_paused,guard_job
from services.hub.errors import HubError

CHANNEL_A='UC'+'a'*22
CHANNEL_B='UC'+'b'*22

def policy(root):
    p=root/'config/source-controls.json';p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps({'version':'source-controls-v1','paused_x_handles':['paused_owner'],
        'paused_youtube_channel_ids':[CHANNEL_A],'reason':'Owner requested temporary pause'}))
    p.chmod(0o640)
    return p

def test_source_selection_is_exact_and_keeps_other_channel(tmp_path):
    assert paused_sources(tmp_path)=={'x':(),'youtube':()}
    policy(tmp_path)
    assert source_paused(tmp_path,'x','PAUSED_OWNER')
    assert not source_paused(tmp_path,'x','other_owner')
    assert source_paused(tmp_path,'youtube',CHANNEL_A)
    assert not source_paused(tmp_path,'youtube',CHANNEL_B)

def test_malformed_pause_config_fails_closed(tmp_path):
    p=policy(tmp_path);p.write_text('{')
    with pytest.raises(HubError) as exc:paused_sources(tmp_path)
    assert exc.value.code=='SOURCE_CONTROLS_INVALID'

def test_primary_author_and_publish_parent_are_paused_but_quotes_are_not(tmp_path):
    policy(tmp_path);settings=SimpleNamespace(root=tmp_path)
    post={'job_type':'tweet_process','source_platform':'x','payload':{'author':{'username':'paused_owner'}}}
    store=SimpleNamespace(get=lambda ident:post)
    child={'job_type':'tweet_machine_publish','source_platform':'machine_web','payload':{'parent_job_id':'owned-parent'}}
    for job in (post,child):
        with pytest.raises(HubError) as exc:guard_job(settings,store,job)
        assert exc.value.code=='SOURCE_FORWARDING_PAUSED'
    post['payload']['author']['username']='other_owner'
    post['payload']['reference_context']={'author':{'username':'paused_owner'}}
    guard_job(settings,store,post);guard_job(settings,store,child)

def test_replay_pause_retains_readonly_lookup(tmp_path):
    policy(tmp_path);settings=SimpleNamespace(root=tmp_path)
    store=SimpleNamespace(query=lambda *args:[{'channel_id':CHANNEL_A}])
    job={'job_type':'youtube_replay_download_v4','source_platform':'youtube','source_id':'owned-video'}
    with pytest.raises(HubError) as exc:guard_job(settings,store,job)
    assert exc.value.code=='SOURCE_FORWARDING_PAUSED'
    job['job_type']='replay_part_lookup_v5';guard_job(settings,store,job)
    store.query=lambda *args:[{'channel_id':CHANNEL_B}]
    job['job_type']='youtube_replay_download_v4';guard_job(settings,store,job)

def test_worker_holds_paused_post_before_external_handler(runtime):
    from services.hub.worker import Worker
    settings,store=runtime;policy(settings.root);calls=[]
    ident=store.enqueue('x','owned-paused','tweet_process','tweet',
        {'author':{'username':'paused_owner'}},'owned-test')
    worker=Worker(settings,store,{'tweet_process':lambda job:calls.append(job)})
    worker.once('tweet')
    row=store.get(ident)
    assert not calls and row['state']=='awaiting_review' and row['error_code']=='SOURCE_FORWARDING_PAUSED'
    assert row['attempts']==1 and row['external_publish_id'] is None
