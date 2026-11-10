"""Real V5 delivery/packet/cover bytes; all Chromium pages are owned fixtures."""
from copy import deepcopy
import hashlib,json,os,subprocess,uuid
from pathlib import Path
import pytest
from test_replay_delivery_v5 import delivery,fifo,tracks
from test_cover_normalizer_v4 import make_jpeg
from services.hub.cover_normalizer_v4 import normalize_cover
from services.hub.youtube_download import digest
from test_replay_parts_v5 import publisher


# The negative playback case waits through the real bounded 30-second observer,
# plus browser preparation; its child still has an 80-second hard timeout.
@pytest.mark.parametrize('scenario',['upload_one','prepare_public','foreign_sidebar','delivery_changed','selected_file_changed',
    'submit_file_changed','upload_receipt_rewritten','expired_ticket',pytest.param('public_no_progress', marks=pytest.mark.timeout(100)),'public_challenge','pending_manager','submit_manager_loading'])
def test_standalone_part_uses_real_delivery_and_one_native_file(delivery,scenario):
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
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    proc=subprocess.run(['node',str(root/'tests/replay_part_native_v5_fixture.mjs'),str(f.s.root),str(file),scenario],cwd=root,env=env,
        capture_output=True,text=True,encoding='utf-8',timeout=80)
    assert proc.returncode==0,proc.stderr
    evidence=json.loads(proc.stdout.strip().splitlines()[-1]);assert evidence['scenario']==scenario and evidence['owned'] is True
    assert evidence['platform_actions']==evidence['external_requests']==0


def test_actual_python_qa_reservation_and_node_raw_evidence_complete_same_bound_part(publisher):
    """Real coordinator, real file/packet/cover QA and real owned Chromium.

    Only the public site is a routed fixture. No hand-written passed proof,
    synthetic preparation, fake ticket or fabricated playback enters the CAS.
    """
    import queue,threading
    from test_replay_parts_v5 import create,cover
    from services.hub.replay_part_publish_worker_v5 import part_replay_manifest
    from services.hub.replay_parts_v5 import STATE,GROUP_STATE
    from services.hub.replay_video_web import WORKER_PRINCIPAL
    f=publisher;create(f);claimed=f.db.claim('video',120,4);assert claimed['job_id']==f.child
    root=Path(__file__).resolve().parents[1];attempt=str(uuid.uuid4())
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA',
        'ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    with f.publish.worker_context(claimed):
        f.publish.bind_browser_attempt(WORKER_PRINCIPAL,f.child,attempt_id=attempt);cover(f,attempt)
        first=f.publish.verify_for_publish(f.child)
        job,binding,state=f.publish.snapshot(f.child,write=False)
        manifest=part_replay_manifest(f.s,f.db,job,binding,state,attempt)
        file=f.s.root/'owned-actual-python-manifest.json';file.write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
        child=subprocess.Popen(['node',str(root/'tests/replay_part_native_v5_fixture.mjs'),str(f.s.root),str(file),'integration'],
            cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8')
        replies=queue.Queue();errors=[]
        def read():
            for line in child.stdout:replies.put(line)
        def stderr():errors.append(child.stderr.read())
        reader=threading.Thread(target=read,daemon=True);reader.start()
        error_reader=threading.Thread(target=stderr,daemon=True);error_reader.start()
        def receive():
            try:return json.loads(replies.get(timeout=60))
            except queue.Empty:raise AssertionError('Owned Node timed out: '+''.join(errors))
        def operation(name,**arguments):
            child.stdin.write(json.dumps({'operation':name,'arguments':arguments})+'\n');child.stdin.flush()
            reply=receive();assert reply.get('ok') is True,reply;return reply['result']
        try:
            assert receive()=={'state':'owned_ready'}
            f.publish.start_upload(WORKER_PRINCIPAL,f.child,attempt_id=attempt,site_uid=binding['site_uid'])
            uploaded=operation('upload');f.publish.record_upload(WORKER_PRINCIPAL,f.child,uploaded['upload_observation'])
            ticket=f.publish.reserve_submit(WORKER_PRINCIPAL,f.child,attempt_id=attempt,site_uid=binding['site_uid'],
                upload_reference_sha256=uploaded['upload_observation']['upload_reference_sha256'],
                native_refresh=lambda:operation('prepare')['native_preparation'])
            assert ticket['machine_evidence_sha256']!=first['evidence_sha256']
            actual=operation('submit',reservation=ticket)
            recorded=f.publish.record_result(WORKER_PRINCIPAL,f.child,actual['result_observation'],lookup=True,result_reference=actual['result_reference'])
            assert recorded['parent_state']=='completed'
            state=f.db.get(f.child)['result'][STATE]
            assert state['public_coverage']['complete_part_playability_verified'] is True
            assert state['result_observation']['same_submit_attempt_observed'] is False
            assert len(f.db.query('SELECT * FROM publications'))==1 and f.db.get(f.child)['attempts']==1
            assert f.db.get(f.parent)['result'][GROUP_STATE]['completed_parts']==1
            child.stdin.write('{"operation":"close"}\n');child.stdin.flush()
            assert receive()['submits']==1
            assert child.wait(timeout=15)==0,''.join(errors)
        finally:
            if child.poll() is None:
                child.stdin.close()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=10)
            reader.join(timeout=1);error_reader.join(timeout=1)
