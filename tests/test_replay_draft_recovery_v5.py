"""Owned root-mode seam; real delivery, native prepare and one recovery click.

The original submission ticket is a declared historical QA-input fixture; no
live source or root authorization is claimed. No platform network is opened.
"""
from copy import deepcopy
import hashlib,json,os,subprocess,uuid
from pathlib import Path
import pytest
from test_replay_delivery_v5 import delivery,fifo,tracks
from test_cover_normalizer_v4 import make_jpeg
from services.hub.cover_normalizer_v4 import normalize_cover
from services.hub.youtube_download import digest
from test_replay_parts_v5 import publisher



@pytest.mark.parametrize('scenario',['once_unknown','once_public','expired_grant','wrong_draft','intent_changed','file_changed','writable_grant','grant_changed'])
def test_root_grant_can_recover_exact_saved_draft_once_without_new_upload(delivery,scenario):
    f=delivery;result=f.service.process(f.job);state=result['youtube_download_v4'];d=state['delivery_v5'];output=d['outputs'][0]
    assert d['processing_model']=='single_whole_passthrough_v5' and len(d['outputs'])==1
    assert not (Path(output['file']).parent/'parts-v4').exists()
    ident,parent,attempt=(str(uuid.uuid4()) for _ in range(3));url='https://www.youtube.com/watch?v='+f.raw['id']
    jpeg=f.s.root/'media'/ident/'short-video-attempts'/attempt/'cover-source-official-maxres.jpg';jpeg.parent.mkdir(parents=True)
    make_jpeg(jpeg)
    normalize_cover(f.s.root,ident,f.raw['id'],jpeg,hashlib.sha256(jpeg.read_bytes()).hexdigest(),
        'https://i.ytimg.com/vi/'+f.raw['id']+'/maxresdefault.jpg')
    prov=f.s.root/'media'/ident/'cover-assets-v4/provenance.json';p=json.loads(prov.read_bytes())
    verified=Path(output['file']).parent/'verified.json'
    ref=lambda file:{'file':str(file),'bytes':file.stat().st_size,'sha256':hashlib.sha256(file.read_bytes()).hexdigest()}
    contract={'version':'replay-part-source-contract-v5','processing_model':d['processing_model'],'download_job_id':f.job['job_id'],
        'whole_plan_sha256':d['whole_plan_sha256'],'source_sha256':d['source_sha256'],'source_bytes':state['source_asset']['bytes'],
        'source_duration_seconds':state['source_asset']['duration'],'source_actual_start_time':f.raw['liveStreamingDetails']['actualStartTime'],
        'operating_not_before':'2026-10-31T16:00:00Z','delivery_manifest':d['manifest_reference'],'download_receipt':ref(verified),'output':output}
    meta={'title':'Owned original replay P1/1','description':'Owned complete original title\n原视频：'+url+'\n原频道：Owned channel',
        'copyright':2,'source':url,'category_label':'娱乐','tags':['Owned']}
    spec={'version':'replay-part-native-browser-manifest-v5','job_id':ident,'parent_job_id':parent,'download_job_id':f.job['job_id'],
        'attempt_id':attempt,'target_site_uid':'1000000000000000','account_name':'Owned publisher','source_id':f.raw['id'],'source_url':url,
        'channel_id':f.raw['snippet']['channelId'],'file':output['file'],'file_bytes':output['bytes'],'artifact_sha256':output['sha256'],
        'duration_seconds':output['duration'],'source_duration_seconds':state['source_asset']['duration'],'metadata':meta,'metadata_sha256':digest(meta),
        'binding_sha256':digest({'owned':True,'source_contract':contract,'metadata':meta}),'policy_sha256':'b'*64,
        'whole_plan_sha256':d['whole_plan_sha256'],'parts_manifest_sha256':d['manifest_reference']['sha256'],'part_index':0,'part_count':1,
        'part_idempotency_key':digest(['bilibili','youtube',f.raw['id'],'site_uid:1000000000000000',d['manifest_reference']['sha256'],0,output['sha256'],
            'video_website_replay_part_v5_p01']),'processing_manifest':d['manifest_reference'],'source_contract':contract,
        'cover':{**{k:p['output'][k] for k in ('file','bytes','sha256')},'source_url':p['input']['source_url']},
        'cover_derivation':{'version':'derived-cover-binding-v2','provenance_file':str(prov),'provenance_sha256':ref(prov)['sha256'],'provenance':p},
        'human_review':False,'per_artifact_owner_acceptance':False}
    file=f.s.root/'owned-v5-manifest.json';file.write_text(json.dumps(spec,ensure_ascii=False),encoding='utf-8')

    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA','ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    file=f.s.root/'recovery-spec.json';file.write_text(json.dumps(spec,ensure_ascii=False),encoding='utf-8')
    run=subprocess.run(['node',str(root/'tests/replay_draft_recovery_v5_fixture.mjs'),str(f.s.root),str(file),scenario],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=90)
    assert run.returncode==0,run.stderr
    result=json.loads(run.stdout.strip().splitlines()[-1])
    assert result['owned'] and result['external_requests']==0 and result['recovery_submits'] in (0,1)
