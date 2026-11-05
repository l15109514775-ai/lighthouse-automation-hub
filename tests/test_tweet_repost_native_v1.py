"""Actual owned Chromium; all HTTP intercepted, no platform/account action."""
import json,os,subprocess,time,hashlib
from pathlib import Path
from types import SimpleNamespace
import pytest
from services.hub.db import encode
from services.hub.tweet_repost_check_v1 import validate_observation

@pytest.mark.parametrize('scenario',['available','disabled','not_found','auth','wrong_uid','challenge','wrong_caption','denied','offscreen','cached_probe','after_challenge','final_only'])
def test_readonly_native_repost_observation(scenario,tmp_path):
    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    spec={'version':'tweet-native-repost-check-v1','check_job_id':'00000000-0000-4000-8000-000000000001','attempt_id':'00000000-0000-4000-8000-000000000002',
        'submission_job_id':'00000000-0000-4000-8000-000000000003','target_site_uid':'1000000000000000','target_name':'Owned native account',
        'external_id':'1266077623570661433','url':'https://t.bilibili.com/1266077623570661433','source_id':'2117189314744590552',
        'source_url':'https://x.com/example-channel-b/status/2117189314744590552','caption':'Owned native translation https://x.com/example-channel-b/status/2117189314744590552',
        'submission_recorded_at':float(int(time.time())-600)}
    spec['not_before']=spec['submission_recorded_at']+300;spec['caption_sha256']=hashlib.sha256(spec['caption'].encode()).hexdigest()
    request=tmp_path/'request.json';request.write_text(encode(spec),'utf-8')
    folder=tmp_path/'media'/spec['check_job_id']/'tweet-repost-check-v1'/spec['attempt_id']
    run=subprocess.run(['node',str(root/'tests/tweet_repost_check_v1_fixture.mjs'),scenario,str(folder),str(request)],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert run.returncode==0,run.stderr
    result=json.loads(run.stdout.strip().splitlines()[-1]);assert result['scenario']==scenario and result['owned'] is True and result['final_send_clicks']==0
    actual=validate_observation(SimpleNamespace(root=tmp_path),spec,spec['attempt_id'],result['result_reference'])
    assert actual['send_requests']==0 and actual['public_visibility_verified'] is actual['platform_cause_verified'] is False
