"""Real processor/packets/cover and owned Chromium only; zero platform traffic."""
import hashlib,json,os,shutil,subprocess,uuid
from pathlib import Path
import pytest
from test_replay_multipart_v4 import owned
from services.hub.replay_multipart_v4 import MultipartMediaProcessingV4,digest
from services.hub.cover_normalizer_v4 import normalize_cover


@pytest.mark.parametrize('scenario',['complete','wrong_ticket','changed_file','missing_part','duplicate_playlist','expired_ticket'])
def test_native_multipart_owned_ui_and_full_public_part_coverage(owned,scenario):
    s,db,job,info,policy,plan,details=owned
    area=s.root/'media'/job['job_id']/'youtube-whole-v4';area.mkdir()
    source=area/'original.mp4';shutil.copyfile(info['file'],source);source.chmod(0o600)
    local={**info,'file':str(source)}
    prepared=MultipartMediaProcessingV4(s,db,policy=policy,plan=plan).run(job,local)
    assert len(prepared['parts'])>=2 and prepared['packet_coverage_verified'] is True
    ident,attempt=str(uuid.uuid4()),str(uuid.uuid4());url='https://www.youtube.com/watch?v='+job['source_id']
    jpeg=s.root/'media'/ident/'short-video-attempts'/attempt/'cover-source-official-maxres.jpg';jpeg.parent.mkdir(parents=True)
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=white:s=1280x720','-frames:v','1','-threads','1','-y',str(jpeg)],check=True,capture_output=True,timeout=20)
    normalize_cover(s.root,ident,job['source_id'],jpeg,hashlib.sha256(jpeg.read_bytes()).hexdigest(),
        'https://i.ytimg.com/vi/'+job['source_id']+'/maxresdefault.jpg')
    provenance=s.root/'media'/ident/'cover-assets-v4/provenance.json';p=json.loads(provenance.read_bytes());cover=p['output']
    parts=[{'index':i+1,'part_index':i,'file':x['file'],'filename':Path(x['file']).name,'bytes':x['bytes'],'sha256':x['sha256'],
        'duration_seconds':int(x['duration']) if float(x['duration']).is_integer() else x['duration'],
        'part_idempotency_key':x['part_idempotency_key']} for i,x in enumerate(prepared['parts'])]
    meta={'title':'Owned multipart complete replay','description':'Owned full original title\n原视频：'+url,
        'copyright':2,'source':url,'category_label':'娱乐','tags':['Owned']}
    manifest=Path(prepared['manifest']);raw=manifest.read_bytes()
    spec={'version':'replay-multipart-native-browser-manifest-v4','job_id':ident,'download_job_id':job['job_id'],'attempt_id':attempt,
        'target_site_uid':'1000000000000000','account_name':'Owned publisher','source_id':job['source_id'],'source_url':url,
        'channel_id':plan['channel_id'],'metadata':meta,'metadata_sha256':digest(meta),'binding_sha256':digest({'owned':True,'parts':parts}),
        'policy_sha256':digest(policy),'artifact_sha256':info['sha256'],'file_bytes':info['bytes'],'source_duration_seconds':details['duration'],
        'whole_plan_sha256':digest(plan),'parts_manifest_sha256':digest(parts),'parts':parts,
        'processing_manifest':{'file':str(manifest),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()},
        'cover':{**{k:cover[k] for k in ('file','bytes','sha256')},'source_url':p['input']['source_url']},
        'cover_derivation':{'version':'derived-cover-binding-v2','provenance_file':str(provenance),'provenance_sha256':hashlib.sha256(provenance.read_bytes()).hexdigest(),'provenance':p},
        'human_review':False,'per_artifact_owner_acceptance':False}
    document=s.root/'owned-browser-spec.json';document.write_text(json.dumps(spec,ensure_ascii=False),encoding='utf-8')
    repo=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(repo/'compositor/runtime/browsers')
    result=subprocess.run(['node',str(repo/'tests/multipart_native_v4_fixture.mjs'),scenario,str(s.root),str(document)],cwd=repo,env=env,
        capture_output=True,text=True,encoding='utf-8',timeout=120)
    assert result.returncode==0,result.stderr
    value=json.loads(result.stdout.strip().splitlines()[-1]);assert value['scenario']==scenario and value['owned'] is True
    assert value['platform_publications']==value['external_requests']==0
