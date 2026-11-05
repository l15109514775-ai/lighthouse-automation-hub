"""Owned real Chromium author navigation; raw proof crosses the Python gate."""
from copy import deepcopy
import hashlib,json,os,subprocess,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.tweet_repost_check_v1 import validate_observation


def run_owned(scenario,root):
    repo=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(repo/'compositor/runtime/browsers')
    spec={'version':'tweet-native-repost-check-v1','check_job_id':'00000000-0000-4000-8000-000000000001',
        'attempt_id':'00000000-0000-4000-8000-000000000002','submission_job_id':'00000000-0000-4000-8000-000000000003',
        'target_site_uid':'1000000000000000','target_name':'Owned native account','external_id':'1266077623570661433',
        'url':'https://t.bilibili.com/1266077623570661433','source_id':'2117189314744590552',
        'source_url':'https://x.com/example-channel-b/status/2117189314744590552',
        'caption':'Owned native translation https://x.com/example-channel-b/status/2117189314744590552',
        'submission_recorded_at':float(int(time.time())-600)}
    spec['not_before']=spec['submission_recorded_at']+300;spec['caption_sha256']=hashlib.sha256(spec['caption'].encode()).hexdigest()
    request=root/'request.json';request.write_text(encode(spec),encoding='utf-8')
    folder=root/'media'/spec['check_job_id']/'tweet-repost-check-v1'/spec['attempt_id']
    p=subprocess.run(['node',str(repo/'tests/tweet_repost_author_navigation_v2_fixture.mjs'),scenario,str(folder),str(request)],
        cwd=repo,env=env,capture_output=True,text=True,encoding='utf-8',timeout=45)
    assert p.returncode==0,p.stderr
    result=json.loads(p.stdout.strip().splitlines()[-1]);assert result['owned'] is True and result['counts']['final']==0
    ref=result['result_reference'];body=json.loads(Path(ref['file']).read_bytes())
    return SimpleNamespace(root=root),spec,ref,body,result


@pytest.mark.parametrize('scenario',['popup','same_tab','wrong_author','return_changed','profile_challenge',
    'duplicate_sidebar','cached_author','denied','duplicate_author'])
def test_actual_span_profile_navigation_and_separate_sidebar_probe(scenario,tmp_path):
    settings,spec,ref,body,result=run_owned(scenario,tmp_path)
    actual=validate_observation(settings,spec,spec['attempt_id'],ref)
    assert actual['send_requests']==0 and actual['public_visibility_verified'] is actual['platform_cause_verified'] is False
    assert actual['native_control_click_attempts']==result['counts']['repost']
    assert actual['author_navigation_click_attempts']==result['counts']['author']
    assert actual['total_native_click_attempts']==sum(result['counts'].values())


@pytest.fixture(scope='module')
def native_proof(tmp_path_factory):
    return run_owned('popup',tmp_path_factory.mktemp('author-native-v2'))


@pytest.mark.parametrize('mutation',['wrong_uid_url','missing_profile_image','missing_identity','no_return_binding',
    'different_return_dynamic','anchor_method_after_navigation','click_sum','future_profile_time','profile_port','classes_not_list'])
def test_authority_is_actual_profile_route_and_return_binding_not_logged_uid(native_proof,mutation):
    settings,spec,ref,original,_=native_proof;body=deepcopy(original)
    if mutation=='wrong_uid_url':body['author_identity']['profile_url']='https://space.bilibili.com/10001/dynamic'
    elif mutation=='missing_profile_image':body['author_identity'].pop('profile_screenshot')
    elif mutation=='missing_identity':body['author_identity']=None
    elif mutation=='no_return_binding':body['author_identity']['return_binding_verified']=False
    elif mutation=='different_return_dynamic':body['author_identity']['returned_dynamic_url']='https://t.bilibili.com/10001'
    elif mutation=='anchor_method_after_navigation':body['author_identity']['method']='visible_named_author_anchor'
    elif mutation=='click_sum':body['actions']['clicks']=1
    elif mutation=='future_profile_time':body['author_identity']['observed_at']='2099-01-01T00:00:00Z'
    elif mutation=='profile_port':body['author_identity']['profile_url']='https://space.bilibili.com:444/'+spec['target_site_uid']+'/dynamic'
    elif mutation=='classes_not_list':body['author_identity']['clicked_element']['classes']='bili-dyn-title__text'
    raw=encode(body).encode();Path(ref['file']).write_bytes(raw)
    actual_ref={**ref,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    with pytest.raises(HubError):validate_observation(settings,spec,spec['attempt_id'],actual_ref)


def test_legacy_v1_observation_keeps_zero_author_navigation(tmp_path):
    from tests.test_tweet_repost_check_v1 import write_observation
    spec={'check_job_id':'00000000-0000-4000-8000-000000000001','target_name':'Owned native account',
        'external_id':'1266077623570661433','url':'https://t.bilibili.com/1266077623570661433','not_before':time.time()-300,
        'attempt_id':'00000000-0000-4000-8000-000000000002'}
    settings=SimpleNamespace(root=tmp_path);ref=write_observation(settings,spec,lambda:None)
    actual=validate_observation(settings,spec,spec['attempt_id'],ref)
    assert actual['native_control_click_attempts']==actual['author_navigation_click_attempts']==actual['total_native_click_attempts']==0
