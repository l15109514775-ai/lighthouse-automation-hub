"""Actual SQLite/hash/unlink recovery, synthetic accepted-public protocol data.

Root deployment admission and real platform/media acceptance are explicit owned
seams. No test invokes main, a browser, a network request or production files.
The mature public DOM/upload validators and single-file primitive run for real.
"""
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace
import hashlib,importlib.util,json,sqlite3,time
import pytest

from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.archive_source_retention_v2 import _inspect
from services.hub.pilot_published_retention_v2 import PilotPublishedRetentionV2
from services.hub import replay_later_native_result_v1 as later
from services.hub.replay_highest_coverage_v2 import upload_coverage

FILE=Path(__file__).parents[2]/'scripts/ops/cleanup_published_pilot_master_once_v1.py'
spec=importlib.util.spec_from_file_location('owned_pilot_master_once',FILE);op=importlib.util.module_from_spec(spec);spec.loader.exec_module(op)

def put(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);raw=encode(value).encode();path.write_bytes(raw)
    return {'file':str(path),'bytes':len(raw),'sha256':op.sha(raw)}
def stamp(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()

@pytest.fixture
def owned(tmp_path,monkeypatch):
    root=tmp_path/'owned-root';master=root/'media'/op.DOWNLOAD/'youtube-highest-v2/original.mp4'
    master.parent.mkdir(parents=True);master.write_bytes(b'owned master bytes, not a real accepted movie\n'*127)
    monkeypatch.setattr(op,'ROOT',root);monkeypatch.setattr(op,'MASTER',master)
    monkeypatch.setattr(op,'BYTES',master.stat().st_size);monkeypatch.setattr(op,'MASTER_SHA',op.sha(master.read_bytes()))
    store=Store(root/'data/hub.sqlite3');store.init();now=time.time();at=now-3600
    settings=SimpleNamespace(root=root,db_path=store.path,config={'target_bilibili_site_uid':'1000000000000000','sources':[{'youtube_channel_id':'owned-channel'}]})
    binding={'job_id':op.JOB,'download_job_id':op.DOWNLOAD,'source_id':op.SOURCE,'channel_id':'owned-channel','site_uid':'1000000000000000',
        'source_url':'https://www.youtube.com/watch?v='+op.SOURCE,'account_name':'Owned uploader','file':str(master),
        'file_bytes':op.BYTES,'artifact_sha256':op.MASTER_SHA,'metadata_sha256':'a'*64,'policy_sha256':'b'*64,
        'metadata':{'title':'Owned completed long replay','description':'Owned exact source description'}}
    output=root/'media'/op.JOB/'replay-browser-highest-v2'/op.ATTEMPT
    body={'version':'known-bv-public-passive-inventory-v1','observed_at':stamp(at),'job_id':op.JOB,'attempt_id':op.ATTEMPT,
        'external_id':op.BV,'expected_title':binding['metadata']['title'],'file_selections':0,'save_clicks':0,'submit_clicks':0,'player_seeks':0,'source_or_media_modified':False,
        'visible_dom':{'url':'https://www.bilibili.com/video/'+op.BV,'titles':[{'text':binding['metadata']['title']}],
            'owner_candidates':[{'visible':True,'class':'up-name','text':'Owned uploader','url':'https://space.bilibili.com/1000000000000000/','card_text':'Owned uploader\nOwned account card'}],
            'descriptions':[{'visible':True,'class':'basic-desc-info','text':binding['source_url']+'\n'+binding['metadata']['description']}],
            'videos':[{'visible':True,'duration_seconds':25715,'current_time':25714,'ready_state':4,'error_code':None,'video_width':1920,'video_height':1080,'seekable_ranges':[{'start':0,'end':25715}]}]}}
    public=put(output/'owned-original-public.json',body)
    posted=datetime.fromtimestamp(at-60,timezone(timedelta(hours=8))).strftime('%Y年%m月%d日 %H:%M:%S')
    manager=put(output/'owned-original-manager.json',{'url':'https://member.bilibili.com/platform/upload-manager/article',
        'visible_text':'视频管理\n图文管理\n互动视频管理\n全部稿件 1\n进行中 0\n已发布 1\n未通过 0\n'+binding['metadata']['title']+'\n'+posted,
        'links':[{'url':'https://www.bilibili.com/video/'+op.BV,'text':binding['metadata']['title']}]})
    submit=put(output/'owned-public-submit.json',{'created_at':stamp(at-120)})
    monkeypatch.setattr(op,'SUBMIT_SHA',submit['sha256'])
    evidence={'version':later.VERSION,'origin':later.ORIGIN,'binding_sha256':op.digest(binding),'same_submit_attempt_observed':False,'human_review':False,'publication_authorization_inferred':False,
        'public_observation':public,'manager_observation':manager,'original_public_submit':submit,
        'screenshot':put(output/'owned-screenshot.json',{'owned':'screenshot protocol, not media acceptance'}),
        'manager_before_submit':put(output/'owned-manager-before.json',{'owned':'accepted before protocol'}),
        'original_unknown_result':put(output/'public-result.json',{'state':'unknown','submitted':None,'reconcile_required':True}),
        'original_machine_proof':put(output/'owned-machine-proof.json',{'owned':'accepted upstream seam'})}
    monkeypatch.setattr(op,'EVIDENCE_SHA',op.digest(evidence))
    uploaded={'job_id':op.JOB,'attempt_id':op.ATTEMPT,'site_uid':binding['site_uid'],'source_id':op.SOURCE,'source_url':binding['source_url'],
        'artifact_sha256':op.MASTER_SHA,'file_bytes':op.BYTES,'metadata_sha256':binding['metadata_sha256'],'binding_sha256':op.digest(binding),
        'version':'replay-highest-native-upload-coverage-v2','file':str(master),'selected_filename':'original.mp4','native_upload_complete':True,
        'whole_file_streamed':True,'streamed_bytes':op.BYTES,'streamed_sha256':op.MASTER_SHA,'server_file_hash_verified':False,'human_review':False,
        'native_filename':{'kind':'visible_filename','text':'original.mp4','observed':True},'observed_at':stamp(at-200)}
    uploadref=put(output/'upload-coverage.json',uploaded);uploaded={**uploaded,'evidence':uploadref}
    state={'state':'completed','attempt_id':op.ATTEMPT,'key':'owned-publication-key','result_reconciliation':evidence,
        'upload_observation':{'upload_coverage':uploadref},'upload_coverage':uploaded,
        'result_observation':{'state':'completed','external_id':op.BV,'url':'https://www.bilibili.com/video/'+op.BV,'site_uid':binding['site_uid'],'source_url':binding['source_url'],'metadata_sha256':binding['metadata_sha256']}}
    raw2={'version':'pilot-redundant-raw-only-receipt-v5','raw_files_deleted':2,'raw_logical_bytes_deleted':10921804989,
        'master_retained':{'file':str(master),'bytes':op.BYTES,'sha256':op.MASTER_SHA}}
    source_result={'youtube_download_v2':{'binding':{'duration':25715},'receipt':{'source_to_merged_packet_coverage_verified':True},
        'source_asset':{'file':str(master),'bytes':op.BYTES,'sha256':op.MASTER_SHA,'packet_evidence':{'owned':'already accepted packet-proof seam'}}},'redundant_source_tracks_retention_pilot_v5':raw2}
    auth={'authorization_id':op.AUTH,'scope':op.PUBLISHED,'artifact':None,'target_site_uid':binding['site_uid'],'actual_owner_response':'Owned synthetic disposal instruction; no actual authorization claim'}
    monkeypatch.setattr(op,'AUTH_SHA',op.digest(auth))
    with store.transaction() as db:
        for ident,kind,attempts,payload,result in ((op.DOWNLOAD,'youtube_download_v2',2,{'channel_id':'owned-channel'},source_result),(op.JOB,'replay_highest_video_publish_v2',3,binding,{'replay_highest_video_web_v2':state})):
            db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,attempts,policy_version,payload,result,external_publish_id) VALUES(?,'youtube',?,?,'video','completed',?,?,?,'owned',?,?,?)",
                (ident,op.SOURCE,kind,at-1000,at+30,attempts,encode(payload),encode(result),op.BV if ident==op.JOB else None))
        db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('owned-publication-key',op.JOB,'bilibili','youtube',op.SOURCE,'site_uid:'+binding['site_uid'],'video_website_replay_highest_v2','completed',op.BV,'https://www.bilibili.com/video/'+op.BV,at+30))
        db.execute('CREATE TABLE video_retention_authorizations_v2(id TEXT PRIMARY KEY,record TEXT NOT NULL)');db.execute('INSERT INTO video_retention_authorizations_v2 VALUES(?,?)',(op.AUTH,encode(auth)))
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(at+30,'operator:ssh-sudo',later.VERSION,op.JOB,encode({'origin':later.ORIGIN,'evidence_sha256':op.EVIDENCE_SHA,'same_submit_attempt_observed':False})))
    ret=PilotPublishedRetentionV2(settings,store)
    monkeypatch.setattr(ret,'_release',lambda p:'owned-root-qualified43-release-admission-only')
    prior=root/'data/owned-prior-plan.json';put(prior,{'job_id':op.JOB,'scope':op.PUBLISHED,'authorization_id':op.AUTH,'authorization_sha256':op.AUTH_SHA,
        'candidate':_inspect(root,master,14_000_000_000),'proof':{'basis':{'local_packet_evidence':source_result['youtube_download_v2']['source_asset']['packet_evidence']}}})
    monkeypatch.setattr(op,'PREVIOUS',prior);monkeypatch.setattr(op,'PREVIOUS_SHA',op.sha(prior.read_bytes()))
    return SimpleNamespace(ret=ret,store=store,master=master,public=public,evidence=evidence,at=at,index=tmp_path/'owned-intent.sqlite3')

def apply(f,plan):
    with sqlite3.connect(f.index) as index:return op.apply_plan(f.ret,later,plan,op.digest(plan),index)

def test_plan_and_apply_one_current_hash_reuse_packet_proof_keep_pub_receipts_and_replay_idempotent(owned,monkeypatch):
    f=owned;scans=[];original=op._inspect
    monkeypatch.setattr(op,'_inspect',lambda *a,**k:(scans.append(a[1]),original(*a,**k))[1])
    pubs=f.store.query('SELECT * FROM publications');plan=op.create_plan(f.ret,later)
    assert plan['candidate_count']==1 and plan['candidate']['file']==str(f.master) and plan['historical_public_confirmation']['current_access_requeried'] is False
    result=apply(f,plan);assert not f.master.exists() and result['local_files_deleted']==1 and scans==[f.master]
    assert apply(f,plan)==result and f.store.query('SELECT * FROM publications')==pubs
    assert f.store.get(op.DOWNLOAD)['result']['redundant_source_tracks_retention_pilot_v5']['raw_files_deleted']==2
    assert Path(f.public['file']).exists() and f.store.get(op.JOB)['attempts']==3

def test_only_real_matching_original_audit_supplies_historical_time_not_payload(owned):
    f=owned;job,binding,state,source,pub=f.ret._context(op.JOB)
    with pytest.raises(HubError):later.validate_public(json.loads(Path(f.public['file']).read_bytes()),binding,op.ATTEMPT,25715)
    assert op.historical_confirmation(f.ret,later)['acceptance_audit_at']==f.at+30
    with f.store.transaction() as db:db.execute("UPDATE audit SET outcome='{}' WHERE action=?",(later.VERSION,))
    with pytest.raises(HubError,match='ACCEPTANCE_AUDIT'):op.historical_confirmation(f.ret,later)
    assert f.master.exists()

def test_changed_public_bytes_or_false_owner_cannot_hide_behind_old_accepted_boolean(owned):
    f=owned;file=Path(f.public['file']);value=json.loads(file.read_bytes());value['visible_dom']['owner_candidates'][0]['url']='https://space.bilibili.com/999/'
    file.write_text(encode(value),'utf-8')
    with pytest.raises(HubError):op.create_plan(f.ret,later)
    assert f.master.exists()

def test_same_size_master_change_after_plan_is_refused_before_unlink(owned):
    f=owned;plan=op.create_plan(f.ret,later);time.sleep(.01);raw=f.master.read_bytes();f.master.write_bytes(b'X'+raw[1:])
    with pytest.raises(HubError,match='ORIGINAL_CHANGED'):apply(f,plan)
    assert f.master.exists() and not f.store.query('SELECT 1 FROM audit WHERE action=?',(op.VERSION,))

def test_shared_reference_or_publication_unknown_refuses_without_deletion(owned):
    f=owned;plan=op.create_plan(f.ret,later)
    ident=f.store.enqueue('maintenance','owned-reader','owned-reader','system',{'file':str(f.master)},'owned')
    with f.store.transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(ident,))
    with pytest.raises(HubError,match='SHARED_ORIGINAL'):apply(f,plan)
    with f.store.transaction() as db:db.execute('DELETE FROM jobs WHERE job_id=?',(ident,));db.execute("UPDATE publications SET state='unknown'")
    with pytest.raises(HubError,match='PUBLICATION_UNRESOLVED'):apply(f,plan)
    assert f.master.exists()

def test_crash_after_unlink_before_main_commit_recovers_saved_deleting_intent_only(owned,monkeypatch):
    f=owned;plan=op.create_plan(f.ret,later);original=f.ret._stat_unlink;calls=[]
    def crash(item,ctime):calls.append(item['file']);original(item,ctime);raise OSError('owned power loss after unlink')
    monkeypatch.setattr(f.ret,'_stat_unlink',crash)
    with pytest.raises(OSError):apply(f,plan)
    assert not f.master.exists() and 'local_original_retention_v2' not in f.store.get(op.JOB)['result']
    result=apply(f,plan);assert result['local_files_deleted']==1 and calls==[str(f.master)]

def test_crash_after_main_commit_before_index_commit_reconciles_both_exact_markers(owned):
    f=owned;plan=op.create_plan(f.ret,later);result=apply(f,plan)
    with sqlite3.connect(f.index) as db:db.execute("UPDATE operations SET state='deleting',record=?",(encode({'plan':plan}),))
    assert apply(f,plan)==result
    with sqlite3.connect(f.index) as db:assert db.execute('SELECT state FROM operations').fetchone()[0]=='completed'

def test_missing_master_without_durable_intent_is_not_assumed_deleted(owned):
    f=owned;plan=op.create_plan(f.ret,later);f.master.unlink()
    with pytest.raises(HubError,match='UNEXPECTED_MISSING'):apply(f,plan)
    assert 'local_original_retention_v2' not in f.store.get(op.JOB)['result']
