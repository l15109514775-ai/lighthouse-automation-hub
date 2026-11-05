"""Exact observed share editor, old dialog, and readonly negative boundaries."""
import hashlib,json,os,subprocess,time
from pathlib import Path
from types import SimpleNamespace
import pytest
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.tweet_repost_check_v1 import validate_observation
from tests.test_tweet_repost_native_v1 import test_readonly_native_repost_observation as run_owned


def run_captured(tmp_path):
    repo=Path(__file__).resolve().parents[1];capture=json.loads((repo/'tests/fixtures/tweet_share_modal_captured_20261103.json').read_bytes())['expectation']
    spec={'version':'tweet-native-repost-check-v1','check_job_id':'00000000-0000-4000-8000-000000000001',
        'attempt_id':'00000000-0000-4000-8000-000000000002','submission_job_id':'00000000-0000-4000-8000-000000000003',
        'target_site_uid':capture['site_uid'],'target_name':capture['target_name'],'external_id':capture['external_id'],
        'url':'https://t.bilibili.com/'+capture['external_id'],'source_id':capture['source_id'],'source_url':capture['source_url'],
        'caption':capture['expected_caption'],'submission_recorded_at':float(int(time.time())-600)}
    spec['not_before']=spec['submission_recorded_at']+300;spec['caption_sha256']=hashlib.sha256(spec['caption'].encode()).hexdigest()
    request=tmp_path/'request.json';request.write_text(encode(spec),'utf-8')
    folder=tmp_path/'media'/spec['check_job_id']/'tweet-repost-check-v1'/spec['attempt_id']
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(repo/'compositor/runtime/browsers')
    p=subprocess.run(['node',str(repo/'tests/tweet_repost_check_v1_fixture.mjs'),'captured_share_editor',str(folder),str(request)],
        env=env,cwd=repo,capture_output=True,text=True,encoding='utf-8',timeout=30)
    assert p.returncode==0,p.stderr
    value=json.loads(p.stdout.strip().splitlines()[-1]);assert value['final_send_clicks']==0
    return validate_observation(SimpleNamespace(root=tmp_path),spec,spec['attempt_id'],value['result_reference'])


@pytest.mark.parametrize('scenario',['captured_share_editor','share_wrong_reference','share_nonmodal','share_without_editor','available'])
def test_exact_share_editor_and_legacy_dialog_do_not_click_final_publish(scenario,tmp_path):
    if scenario=='captured_share_editor':
        assert run_captured(tmp_path)['status']=='repost_dialog_opened_no_send'
    else:run_owned(scenario,tmp_path)
    path=next(tmp_path.glob('media/*/tweet-repost-check-v1/*/observation.json'))
    body=json.loads(path.read_bytes())
    assert body['actions']['submits']==body['functional_probe']['final_send_clicks']==0
    if scenario in ('captured_share_editor','available'):
        assert body['status']=='dialog_opened'
    else:assert body['status']=='probe_unconfirmed'


def test_share_title_and_publish_button_do_not_override_wrong_reference_proof(tmp_path):
    run_captured(tmp_path)
    path=next(tmp_path.glob('media/*/tweet-repost-check-v1/*/observation.json'))
    body=json.loads(path.read_bytes());spec=json.loads((tmp_path/'request.json').read_bytes())
    share=next(d for d in body['functional_probe']['dialogs'] if d['kind']=='bilibili_share_editor_v1')
    share['reference']['author_text']='@Another author'
    raw=encode(body).encode();path.write_bytes(raw)
    ref={'file':str(path),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    with pytest.raises(HubError):validate_observation(SimpleNamespace(root=tmp_path),spec,spec['attempt_id'],ref)
