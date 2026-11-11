"""Owned true FFmpeg/delivery/public-evidence CAS and exact local unlinks.

Native visible DOM is the named publisher fixture seam, never a live Bilibili
claim. Only deployment-root admission is mocked in cleanup execution tests.
"""
from copy import deepcopy
import hashlib,json,sqlite3,time
from pathlib import Path
import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_parts_retention_v5 import ReplayPartsRetentionV5,VERSION,MARKER,retention_tick
from services.hub.replay_parts_v5 import STATE,GROUP_STATE
from services.hub.short_video_web import PRINCIPAL
from services.hub.video_source_retention_v2 import VideoSourceRetentionV2,PRINCIPAL as OWNER_PRINCIPAL,PUBLISHED
from services.hub.youtube_download import digest
from test_replay_parts_v5 import reserved,public_evidence
from test_replay_delivery_v5 import delivery
from test_ytdlp_highest_v2 import tracks
from test_replay_fifo_v4 import fifo
from test_replay_parts_v5 import publisher


@pytest.fixture
def completed(reserved,monkeypatch):
    f=reserved;f.publish.record_result(PRINCIPAL,f.child,public_evidence(f),lookup=True)
    f.retention=ReplayPartsRetentionV5(f.s,f.db)
    f.owner=VideoSourceRetentionV2(f.s,f.db).authorize(OWNER_PRINCIPAL,scope=PUBLISHED,
        actual_owner_response='Owned instruction fixture：录播上传B站成功后不需要保留。非真实用户授权。',owner_response_reference='Owned historical owner-message seam')
    monkeypatch.setattr(f.retention,'_release',lambda:(f.retention._authorization(f.owner['authorization_id']),'owned-root-release-only'))
    def refresh(child,bv):
        assert child==f.child and bv=='BV1owned0001'
        f.publish.record_result(PRINCIPAL,child,public_evidence(f),lookup=True)
    f.refresh=refresh;return f


def plan(f):return f.retention.plan(f.parent)
def apply(f,p):return f.retention.apply(p)


def test_true_n1_complete_raw_proof_and_owner_instruction_delete_only_canonical_once(completed):
    f=completed;source=f.db.get(f.job['job_id']);file=Path(source['result']['youtube_download_v4']['source_asset']['file'])
    kept=f.s.root/'media'/'owned-private-samples';kept.mkdir();sample=kept/'private-part.mp4';sample.write_bytes(b'owned independent private sample retained')
    pubs=f.db.query('SELECT * FROM publications');evidence=list((f.s.root/'media'/f.child).rglob('*.json'))
    old_query=f.db.enqueue('bilibili','owned-old-lookup','replay_part_lookup_v5','video',{'parent_publish_job_id':f.child},'replay-part-video-policy-v5')
    with f.db.transaction() as db:db.execute("UPDATE jobs SET state='failed',error_code='OWNED_OLD_QUERY_ERROR' WHERE job_id=?",(old_query,))
    p=plan(f);assert len(p['candidates'])==1 and p['candidates'][0]['file']==str(file) and file.is_file()
    result=apply(f,p)
    assert result['local_files_deleted']==1 and not file.exists() and sample.read_bytes()==b'owned independent private sample retained'
    assert result['cloud_backup_claimed'] is result['remote_video_deleted'] is result['human_review'] is False
    assert f.db.get(f.parent)['result'][MARKER]==f.db.get(f.job['job_id'])['result'][MARKER]==result
    assert all(p.is_file() for p in evidence) and f.db.query('SELECT * FROM publications')[0]['idempotency_key']==pubs[0]['idempotency_key']
    assert apply(f,p)==result and f.db.get(f.child)['attempts']==0
    assert f.db.get(old_query)['state']=='failed'  # Old query evidence is retained, not mistaken for an active P.


@pytest.mark.parametrize('change',['unknown','failed','processing','shared_completed','lookup_active','source_tombstone','foreign_publication'])
def test_not_all_public_or_active_shared_sources_never_delete(completed,change):
    f=completed;file=Path(f.db.get(f.child)['payload']['file']);before=file.read_bytes()
    if change=='shared_completed':
        other=f.db.enqueue('maintenance','owned-reader','owned-reader','system',{'file':str(file)},'owned')
        with f.db.transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(other,))
    elif change=='lookup_active':
        f.db.enqueue('bilibili','owned-query','replay_part_lookup_v5','video',{'parent_publish_job_id':f.child},'replay-part-video-policy-v5')
    else:
        with f.db.transaction() as db:
            if change=='source_tombstone':db.execute("INSERT INTO source_tombstones VALUES('youtube',?,'OWNER_BACKLOG_CLEARED',?,?,'owner_clear_all')",(f.raw['id'],time.time(),f.child))
            elif change=='foreign_publication':db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',('e'*64,f.job['job_id'],'bilibili','youtube',f.raw['id'],
                'site_uid:'+f.s.config['target_bilibili_site_uid'],'owned_foreign_kind','unknown',None,None,time.time()))
            elif change=='processing':db.execute("UPDATE jobs SET state='processing',lease_owner='owned-active',lease_until=? WHERE job_id=?",(time.time()+120,f.child))
            elif change=='unknown':
                job=f.db.get(f.child);job['result'][STATE]['result_observation']['state']='unknown'
                db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(job['result']),f.child))
            else:db.execute('UPDATE replay_part_children_v5 SET state=? WHERE child_job_id=?',(change,f.child))
    with pytest.raises(HubError):plan(f)
    assert file.read_bytes()==before and not f.db.get(f.parent)['result'].get(MARKER)


def test_unproven_pending_related_source_process_still_blocks_cleanup(completed):
    f=completed
    f.db.enqueue('youtube',f.raw['id'],'video_process','video',{},'machine-tweet-v3-api')
    with pytest.raises(HubError,match='SOURCE_IN_USE'):plan(f)


def test_group_admission_retires_exact_pending_legacy_process_and_is_idempotent(publisher):
    f=publisher;source=f.db.get(f.job['job_id'])
    legacy=f.db.enqueue('youtube',f.raw['id'],'video_process','video',
        {'video_id':f.raw['id'],'url':'https://www.youtube.com/watch?v='+f.raw['id']},
        'machine-tweet-v3-api')
    old=f.db.get(legacy)
    parent=f.groups.create(source['job_id'])
    after=f.db.get(legacy)
    assert after['state']=='failed'
    assert after['stage']=='superseded_by_replay_part_group'
    assert after['error_code']=='YOUTUBE_LEGACY_VIDEO_PROCESS_SUPERSEDED'
    assert after['attempts']==old['attempts']==0
    marker=after['result']['youtube_legacy_video_process_superseded_v5']
    assert marker['source_id']==f.raw['id'] and marker['group_id']==parent
    assert marker['remote_retry_performed'] is marker['publication_created'] is False
    record_file=Path(marker['record']['file']);record=json.loads(record_file.read_bytes())
    assert record['version']=='retire-legacy-source-video-process-v5'
    assert record['source_id']==f.raw['id'] and record['download_job_id']==source['job_id']
    assert record['parent_job_id']==parent and record['job']==old
    assert marker['old_job_sha256']==digest(old)
    audit=f.db.query("SELECT outcome FROM audit WHERE action='youtube_legacy_video_process_superseded_v5' AND target=?",(legacy,))
    assert len(audit)==1 and json.loads(audit[0]['outcome'])['plan_sha256']==marker['plan_sha256']
    assert f.groups.create(source['job_id'])==parent
    assert len(f.db.query("SELECT * FROM audit WHERE action='youtube_legacy_video_process_superseded_v5' AND target=?",(legacy,)))==1


def test_explicit_legacy_video_process_retirement_allows_cleanup(completed):
    from services.hub.archive_source_retention_v2 import digest
    f=completed;source=f.db.get(f.db.get(f.parent)['payload']['download_job_id'])
    job_id=f.db.enqueue('youtube',f.raw['id'],'video_process','video',{},'machine-tweet-v3-api')
    old=f.db.get(job_id);plan_sha='a'*64
    folder=f.s.root/'media'/source['job_id']/'legacy-source-process-retirement-v5';folder.mkdir(parents=True)
    record={'version':'retire-legacy-source-video-process-v5','plan_sha256':plan_sha,
        'source_id':f.raw['id'],'download_job_id':source['job_id'],'job':old}
    file=folder/(plan_sha+'.json');raw=encode(record).encode();file.write_bytes(raw)
    marker={'version':'youtube-legacy-video-process-superseded-v5','plan_sha256':plan_sha,
        'record':{'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()},
        'old_job_sha256':digest(old),'remote_retry_performed':False,'publication_created':False}
    with f.db.transaction() as db:
        result={**old['result'],'youtube_legacy_video_process_superseded_v5':marker}
        db.execute("UPDATE jobs SET state='failed',stage='superseded_by_replay_part_group',error_code='YOUTUBE_LEGACY_VIDEO_PROCESS_SUPERSEDED',result=? WHERE job_id=?",
            (encode(result),job_id))
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (time.time(),'owned','youtube_legacy_video_process_superseded_v5',job_id,encode({'plan_sha256':plan_sha})))
    p=plan(f);assert p['group_id']==f.parent


@pytest.mark.parametrize('kind,audited',[('child',True),('child',False),('parent',True)])
def test_superseded_suffix_requires_exact_archive_audit(completed,kind,audited):
    f=completed;source=f.db.get(f.db.get(f.parent)['payload']['download_job_id'])
    if kind=='child':
        job_id=f.db.enqueue('youtube',f.raw['id'],'video_process','video',{},'owned')
        old_type='video_process';new_type=old_type+'_superseded';children=[]
    else:
        job_id=f.db.enqueue('youtube',f.raw['id'],'video_process','video',{},'owned-parent')
        old_type='replay_parts_publish_parent_v5';new_type=old_type+'_superseded';children=[]
        for index,policy in enumerate(('owned-child-1','owned-child-2'),1):
            child=f.db.enqueue('youtube',f.raw['id'],'owned_child_'+str(index),'video',{},policy)
            child_old='replay_part_publish_v5_p%02d'%index;child_new=child_old+'_superseded'
            with f.db.transaction() as db:
                db.execute("UPDATE jobs SET job_type=?,state='failed',stage='superseded',error_code='REPLAY_PARTS_SUPERSEDED_BY_NEW_LIMITS' WHERE job_id=?",
                    (child_new,child))
            children.append({'job_id':child,'old':child_old,'new':child_new})
    with f.db.transaction() as db:
        db.execute("UPDATE jobs SET job_type=?,state='failed',stage='superseded',error_code='REPLAY_PARTS_SUPERSEDED_BY_NEW_LIMITS' WHERE job_id=?",
            (new_type,job_id))
        if kind=='parent':
            db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode({'replay_parts_v5':{'part_count':2}}),job_id))
        if audited:
            outcome={'children':children if kind=='parent' else [{'job_id':job_id,'old':old_type,'new':new_type}]}
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (time.time(),'owned','archive_superseded_child_types',source['job_id'],encode(outcome)))
    if audited:
        row=f.db.get(job_id)
        assert f.retention._proven_history(row,source,row['result'])
        assert plan(f)['group_id']==f.parent
    else:
        with pytest.raises(HubError,match='SOURCE_IN_USE'):plan(f)


def test_retired_group_marker_requires_matching_immutable_record(completed):
    from services.hub.archive_source_retention_v2 import digest
    f=completed;source=f.db.get(f.db.get(f.parent)['payload']['download_job_id'])
    job_id=f.db.enqueue('youtube',f.raw['id'],'video_process','video',{},'owned')
    old=f.db.get(job_id);plan_sha='b'*64
    folder=f.s.root/'media'/source['job_id']/'replay-part-retirement-v5'/plan_sha;folder.mkdir(parents=True)
    record={'version':'retire-interrupted-replay-part-group-v5','source_id':f.raw['id'],
        'download_job_id':source['job_id'],'jobs':[old]}
    file=folder/'record.json';raw=encode(record).encode();file.write_bytes(raw)
    marker={'version':'replay-part-interrupted-group-retirement-v5','plan_sha256':plan_sha,
        'record':{'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()},
        'old_job_sha256':digest(old),'new_attempts':0,'platform_retry_performed':False}
    with f.db.transaction() as db:
        result={**old['result'],'replay_part_retirement_v5':marker}
        db.execute("UPDATE jobs SET job_type=?,state='failed',stage='superseded_after_interrupted_upload',error_code='REPLAY_PART_GROUP_SUPERSEDED',result=? WHERE job_id=?",
            ('video_process_retired_v5_'+plan_sha[:12],encode(result),job_id))
    assert plan(f)['group_id']==f.parent
    file.write_bytes(raw+b' ')
    with pytest.raises(HubError):plan(f)


@pytest.mark.parametrize('change',['bytes','manifest','owner'])
def test_changed_file_binding_or_owner_record_does_not_reuse_saved_plan(completed,change):
    f=completed;p=plan(f);file=Path(p['candidates'][0]['file'])
    if change=='bytes':
        raw=file.read_bytes();file.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
    elif change=='manifest':
        document=Path(f.db.get(f.child)['payload']['source_contract']['delivery_manifest']['file']);document.write_bytes(document.read_bytes()+b' ')
    else:
        record=deepcopy(f.owner);record['owner_response_reference']='Changed after exact plan'
        with f.db.transaction() as db:db.execute('UPDATE video_retention_authorizations_v2 SET record=? WHERE id=?',(encode(record),record['authorization_id']))
    with pytest.raises(HubError):apply(f,p)
    assert file.is_file() and not f.db.get(f.parent)['result'].get(MARKER)


def test_exit_after_actual_unlink_resumes_same_plan_without_unlinking_again(completed,monkeypatch):
    f=completed;p=plan(f)
    from services.hub.video_source_retention_v2 import VideoSourceTracksRetentionV2
    real=VideoSourceTracksRetentionV2._stat_unlink;calls=[]
    def crash(self,item,ctime):calls.append(item['file']);real(self,item,ctime);raise OSError('owned crash after unlink before progress commit')
    monkeypatch.setattr(VideoSourceTracksRetentionV2,'_stat_unlink',crash)
    with pytest.raises(OSError):apply(f,p)
    assert not Path(p['candidates'][0]['file']).exists() and not f.db.get(f.parent)['result'].get(MARKER)
    result=apply(f,p);assert result['local_files_deleted']==1 and len(calls)==1


def test_unexpected_missing_before_durable_delete_intent_is_never_claimed_as_our_cleanup(completed):
    f=completed;p=plan(f);Path(p['candidates'][0]['file']).unlink()  # This test's own synthetic file only.
    with pytest.raises(HubError,match='UNEXPECTED_MISSING'):apply(f,p)
    assert not f.db.get(f.parent)['result'].get(MARKER)


def test_stale_public_proof_or_fake_refresh_boolean_never_authorizes_deletion(completed):
    f=completed;p=plan(f);state=f.db.get(f.child)['result'][STATE]
    file=Path(state['result_observation']['native_result_evidence']['file']);value=json.loads(file.read_bytes());value['observed_at']='2026-01-01T00:00:00Z';file.write_text(encode(value),'utf-8')
    with pytest.raises(HubError):f.retention.apply(p)
    assert Path(p['candidates'][0]['file']).is_file()


def test_default_automatic_tick_disabled_without_root_release_and_zero_public_lookup(runtime):
    s,db=runtime;assert retention_tick(s,db)=={'version':VERSION,'state':'disabled','files_deleted':0,'external_requests':0}


def test_owner_scope_and_passed_boolean_cannot_substitute_real_protected_qualification(completed):
    f=completed;service=ReplayPartsRetentionV5(f.s,f.db)
    (f.s.root/'config'/'replay-parts-retention-v5-release.json').write_text('{"enabled":true,"passed":true}','utf-8')
    with pytest.raises(HubError):service.plan(f.parent)
    assert Path(f.db.get(f.child)['payload']['file']).is_file()


def test_same_plan_after_main_receipt_commit_but_index_commit_loss_keeps_original_receipt(completed):
    f=completed;p=plan(f);first=apply(f,p)
    with sqlite3.connect(f.retention.index) as db:
        db.execute("UPDATE operations SET state='deleting',record=? WHERE id=?",(encode({'version':VERSION,'plan':p,'progress':{i['file']:'deleted' for i in p['candidates']}}),p['plan_id']))
    again=apply(f,p)
    assert again==first


def owned_two_file_unlink_protocol(f,p,monkeypatch):
    """Exact unlink/restart seam only, not acceptance of a fake split plan.

    The real public N1 fixture is kept. This named seam supplies a second
    locally owned file solely to exercise durable multi-file progress.
    Production _context independently enforces the actual delivery allowlist.
    """
    import shutil
    from services.hub.archive_source_retention_v2 import digest
    original=Path(p['candidates'][0]['file']);second=original.parent/'parts-v4/part-000.mp4';second.parent.mkdir()
    shutil.copyfile(original,second);item=f.retention._inspect(second,p['candidates'][0]['bytes'])
    p=deepcopy(p);p['candidates'].append(item);p['plan_sha256']=digest({k:v for k,v in p.items() if k!='plan_sha256'})
    real=f.retention._context
    def owned_media_admission(group_id,*,candidates=None):
        context=real(group_id);context['files'].append({k:item[k] for k in ('file','bytes','sha256')})
        if candidates is not None:assert [{k:i[k] for k in ('file','bytes','sha256')} for i in candidates]==context['files']
        return context
    monkeypatch.setattr(f.retention,'_context',owned_media_admission)
    return p


def test_multi_file_progress_survives_exit_after_first_unlink_and_finishes_exact_remaining_file(completed,monkeypatch):
    f=completed;p=owned_two_file_unlink_protocol(f,plan(f),monkeypatch)
    from services.hub.video_source_retention_v2 import VideoSourceTracksRetentionV2
    real=VideoSourceTracksRetentionV2._stat_unlink;calls=[]
    def crash_once(self,item,ctime):
        calls.append(item['file']);real(self,item,ctime)
        if len(calls)==1:raise OSError('owned first unlink power loss')
    monkeypatch.setattr(VideoSourceTracksRetentionV2,'_stat_unlink',crash_once)
    with pytest.raises(OSError):apply(f,p)
    assert not Path(p['candidates'][0]['file']).exists() and Path(p['candidates'][1]['file']).exists()
    result=apply(f,p);assert result['local_files_deleted']==2 and calls==[i['file'] for i in p['candidates']]


def test_replaced_path_after_interrupted_unlink_is_preserved_and_never_counted_as_old_file(completed,monkeypatch):
    f=completed;p=owned_two_file_unlink_protocol(f,plan(f),monkeypatch)
    from services.hub.video_source_retention_v2 import VideoSourceTracksRetentionV2
    real=VideoSourceTracksRetentionV2._stat_unlink
    def crash(self,item,ctime):real(self,item,ctime);raise OSError('owned post-unlink exit')
    monkeypatch.setattr(VideoSourceTracksRetentionV2,'_stat_unlink',crash)
    with pytest.raises(OSError):apply(f,p)
    first=Path(p['candidates'][0]['file']);first.write_bytes(b'owned replacement that must never be removed')
    with pytest.raises(HubError):apply(f,p)
    assert first.read_bytes()==b'owned replacement that must never be removed' and Path(p['candidates'][1]['file']).is_file()


def test_automatic_tick_holds_missing_completion_receipt_then_cleans_proven_group_and_idles(completed,monkeypatch):
    from types import SimpleNamespace
    from services.hub import replay_parts_retention_v5 as retention
    f=completed;(f.s.root/'config'/'replay-parts-retention-v5-release.json').write_text('Owned root gate seam','utf-8')
    monkeypatch.setattr(ReplayPartsRetentionV5,'_release',lambda self:(self._authorization(f.owner['authorization_id']),'owned-root-release-only'))
    original=f.db.get(f.parent)['result']
    missing=dict(original)
    del missing['replay_parts_first_completion_v5']
    file=Path(f.db.get(f.child)['payload']['file'])
    before=file.read_bytes()
    publications=f.db.query('SELECT * FROM publications')
    with f.db.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(missing),f.parent))
    held=retention_tick(f.s,f.db)
    assert (held['state'],held['code'],held['files_deleted'])==('held','REPLAY_PART_EVIDENCE_REFERENCE_INVALID',0)
    assert file.read_bytes()==before and f.db.get(f.parent)['result']==missing
    assert f.db.query('SELECT * FROM publications')==publications
    assert retention_tick(f.s,f.db)['state']=='idle'
    with sqlite3.connect(f.retention.index) as db:
        due,status,code=db.execute('SELECT next_due,status,last_code FROM maintenance WHERE group_id=?',(f.parent,)).fetchone()
        assert status=='held' and code==held['code']
        assert db.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==0
    with f.db.transaction() as db:
        db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(original),f.parent))
    monkeypatch.setattr(retention,'time',SimpleNamespace(time=lambda:due+1))
    value=retention_tick(f.s,f.db)
    assert value['state']=='completed' and value['files_deleted']==1
    assert value['receipt']['external_requests']==0
    assert retention_tick(f.s,f.db)['state']=='idle'


def test_full_protected_release_protocol_binds_owner_real_source_hashes_junit_and_resources(completed,monkeypatch):
    """Protocol-only synthetic attestation, never an Ubuntu acceptance claim."""
    from services.hub import replay_parts_retention_v5 as module
    from services.hub.archive_source_retention_v2 import digest
    f=completed;service=ReplayPartsRetentionV5(f.s,f.db);code=Path(module.__file__).resolve().parents[2]
    monkeypatch.setattr(module,'_root_protected',lambda _:None)  # Only owned root-FS admission seam.
    sources={n:hashlib.sha256((code/n).read_bytes()).hexdigest() for n in module.CRITICAL_SOURCES}
    junit=f.s.root/'data/owned-retention-qualification.xml'
    junit.write_text('<testsuites><testsuite>'+''.join('<testcase classname="tests.test_replay_parts_retention_v5" name="owned-'+str(i)+'"/>' for i in range(module.TEST_COUNT))+'</testsuite></testsuites>','utf-8')
    evidence={'version':module.TEST_VERSION,'tests':module.TEST_COUNT,'source_files':sources,'junit_file':str(junit),
        'junit_sha256':hashlib.sha256(junit.read_bytes()).hexdigest(),'network':'none',
        **{k:0 for k in ('exit_code','failures','errors','skipped')},
        **{k:False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','oom_killed')},
        **{k:True for k in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate','mounts_match_isolated_allowlist','mounted_sources_unchanged')}}
    proof=f.s.root/'data/owned-retention-qualification.json';proof.write_text(encode(evidence),'utf-8')
    release={'version':'replay-parts-retention-release-v5','enabled':True,'target_site_uid':f.s.config['target_bilibili_site_uid'],
        'scope':'all_parts_verified_public_exact_local_media_only','authorization_id':f.owner['authorization_id'],
        'authorization_sha256':digest(f.owner),'test_attestation':{'file':str(proof),'sha256':hashlib.sha256(proof.read_bytes()).hexdigest()},'source_files':sources}
    file=f.s.root/'config'/module.RELEASE;file.write_text(encode(release),'utf-8')
    owner,checksum=service._release();assert owner==f.owner and checksum==hashlib.sha256(file.read_bytes()).hexdigest()
    evidence['production_profile_mounted']=True;proof.write_text(encode(evidence),'utf-8')
    release['test_attestation']['sha256']=hashlib.sha256(proof.read_bytes()).hexdigest();file.write_text(encode(release),'utf-8')
    with pytest.raises(HubError):service._release()


def test_same_automatic_plan_apply_performs_one_real_sha_round_and_no_platform_or_packet_rescan(completed,monkeypatch):
    f=completed;calls=[];real=f.retention._inspect
    def inspect(*args):calls.append(str(args[0]));return real(*args)
    monkeypatch.setattr(f.retention,'_inspect',inspect)
    import services.hub.replay_parts_v5 as source
    monkeypatch.setattr(source,'inspect_media',lambda *a,**k:pytest.fail('Confirmed immutable packet proof must not trigger ffprobe again'))
    from services.hub.replay_part_publish_worker_v5 import PartReplayBrowser
    monkeypatch.setattr(PartReplayBrowser,'session',lambda *a,**k:pytest.fail('Retention cannot query Bilibili again'))
    p=plan(f);result=apply(f,p)
    assert result['local_files_deleted']==1 and calls==[p['candidates'][0]['file']]
    assert result['historical_confirmation'] is True and result['current_access_requeried'] is False and result['external_requests']==0


@pytest.mark.parametrize('change',['missing','wrong_reference','wrong_time'])
def test_historical_confirmation_must_come_from_matching_original_real_acceptance_audit(completed,change):
    f=completed
    with f.db.transaction() as db:
        if change=='missing':db.execute("DELETE FROM audit WHERE action='replay_part_later_public_result_v5' AND target=?",(f.child,))
        elif change=='wrong_time':db.execute("UPDATE audit SET at=at-1000 WHERE action='replay_part_later_public_result_v5' AND target=?",(f.child,))
        else:db.execute("UPDATE audit SET outcome='{}' WHERE action='replay_part_later_public_result_v5' AND target=?",(f.child,))
    with pytest.raises(HubError):plan(f)
    assert Path(f.db.get(f.child)['payload']['file']).is_file()


def test_32_independent_sqlite_public_results_replay_original_audits_after_two_days_with_zero_http(reserved,monkeypatch):
    """Real 32 result transactions/raw validators, not oversized-media admission.

    The source/delivery admission boundary is deliberately not exercised by
    this small-fixture liveness test. It never claims a real 32-part recording.
    Each distinct job/BV goes through the actual result validator/CAS/audit;
    only the owned native DOM and pre-existing machine proof are fixtures.
    """
    from datetime import datetime,timezone
    import shutil,uuid
    from services.hub.replay_parts_v5 import ReplayPartVideoWeb,part_type,part_kind,part_metadata,POLICY
    from services.hub.replay_parts_v5 import document
    from services.hub.youtube_download import digest
    from test_replay_parts_v5 import owned_preparation,write,ref
    f=reserved;original_id=f.child;template=f.db.get(original_id);template_state=deepcopy(template['result'][STATE])
    original_proof=json.loads(Path(template_state['machine_acceptance']['evidence_file']).read_bytes());source=f.db.get(f.job['job_id'])
    group=f.db.get(f.parent);group['payload']['part_count']=32
    with f.db.transaction() as db:
        db.execute('UPDATE jobs SET payload=? WHERE job_id=?',(encode(group['payload']),f.parent))
        db.execute('UPDATE replay_part_groups_v5 SET part_count=32 WHERE parent_job_id=?',(f.parent,))
        db.execute('DELETE FROM replay_part_children_v5 WHERE parent_job_id=?',(f.parent,))
        db.execute('DELETE FROM publications WHERE job_id=?',(original_id,))
    children=[]
    for index in range(32):
        binding=deepcopy(template['payload']);binding.update(part_index=index,part_count=32)
        binding['metadata']=part_metadata(f.s,source,index,32);binding['metadata_sha256']=digest(binding['metadata'])
        key=digest(['bilibili','youtube',template['source_id'],'site_uid:'+binding['site_uid'],binding['parts_manifest_sha256'],index,binding['artifact_sha256'],part_kind(index)])
        binding['part_idempotency_key']=key
        ident=original_id if index==0 else f.db.enqueue('youtube',template['source_id'],part_type(index),'video',binding,POLICY)
        state=deepcopy(template_state);state.update(key=key,attempt_id=str(uuid.uuid4()),state='submit_unknown')
        with f.db.transaction() as db:
            db.execute("UPDATE jobs SET payload=?,result=?,state='publish_unknown',lease_owner=NULL,lease_until=NULL WHERE job_id=?",(encode(binding),encode({STATE:state}),ident))
            db.execute('INSERT INTO replay_part_children_v5 VALUES(?,?,?,?,?,?,?)',(f.parent,index,ident,key,binding['artifact_sha256'],'unknown',None))
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,ident,'bilibili','youtube',template['source_id'],
                'site_uid:'+binding['site_uid'],part_kind(index),'unknown',None,None,time.time()))
        children.append(ident)
    for index,ident in enumerate(children):
        service=ReplayPartVideoWeb(f.s,f.db,part_index=index);job,binding,state=service.snapshot(ident,write=False)
        proof=deepcopy(original_proof);proof.update(job_id=ident,binding_sha256=digest(binding))
        proof_file=f.s.root/'media'/ident/service.MACHINE_PROOF_DIRECTORY/str(uuid.uuid4())/'receipt.json'
        proofref=write(proof_file,proof);accepted={**proof,'evidence_file':proofref['file'],'evidence_bytes':proofref['bytes'],'evidence_sha256':proofref['sha256']}
        state['machine_acceptance']=accepted
        base=service._native_base(job,binding,state);state['upload_observation']={**state['upload_observation'],**base,
            'upload_reference_sha256':digest(['owned-native-upload-boundary',ident])}
        with f.db.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode({STATE:state}),ident))
        f.publish=service;f.child=ident
        (f.s.root/'media'/ident/service.NATIVE_DIRECTORY/state['attempt_id']).mkdir(parents=True,exist_ok=True)
        prep=owned_preparation(f,service,accepted);state['native_preparation']=prep
        f.clicked=datetime.now(timezone.utc);issued=f.clicked.timestamp()-1
        ticket={**f.ticket,**base,'idempotency_key':state['key'],'ticket_id':str(uuid.uuid4()),'issued_at':issued,'expires_at':issued+120,
            'machine_evidence_sha256':accepted['evidence_sha256'],'upload_reference_sha256':state['upload_observation']['upload_reference_sha256']}
        write(f.s.root/'media'/ident/'replay-native-intents/public-submit.json',{'version':'replay-part-one-use-intent-v5',**base,'kind':'public-submit',
            'created_at':f.clicked.isoformat(),'reservation':ticket})
        with f.db.transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode({STATE:state}),ident))
        observation=public_evidence(f);old=observation['external_id'];new='BV'+str(index).zfill(10)
        evidence_file=Path(observation['native_result_evidence']['file']);evidence=json.loads(evidence_file.read_bytes())
        def replace(value):
            if isinstance(value,dict):return {k:replace(v) for k,v in value.items()}
            if isinstance(value,list):return [replace(v) for v in value]
            return value.replace(old,new) if isinstance(value,str) else value
        for key in ('manager_observation','public_observation'):
            file=Path(evidence[key]['file']);evidence[key]=write(file,replace(json.loads(file.read_bytes())))
        evidence['external_id']=new;observation['native_result_evidence']=write(evidence_file,evidence)
        observation.update(external_id=new,url='https://www.bilibili.com/video/'+new)
        service.record_result(PRINCIPAL,ident,observation,lookup=True)
    parent=f.db.get(f.parent);first=parent['result']['replay_parts_first_completion_v5'];saved=Path(first['file']).read_bytes()
    assert parent['state']=='completed' and parent['result'][GROUP_STATE]['completed_parts']==32
    assert len(f.db.query("SELECT * FROM audit WHERE action='replay_part_later_public_result_v5'"))==32
    completion=json.loads(saved);rows=f.db.query('SELECT * FROM replay_part_children_v5 WHERE parent_job_id=? ORDER BY part_index',(f.parent,))
    # Only immutable delivery admission is outside this historical-only test.
    # Actual per-child registry/raw binding, timestamps and SQL audits remain real.
    service=ReplayPartsRetentionV5(f.s,f.db)
    monkeypatch.setattr(service,'_context',lambda group_id:{'children':rows,'completion':completion,'identity':{'first_completion':first}})
    import services.hub.replay_part_public_evidence_v5 as validator
    original_now=time.time();monkeypatch.setattr(validator.time,'time',lambda:original_now+2*86400)
    current=f.db.get(children[-1]);last_service=ReplayPartVideoWeb(f.s,f.db,part_index=31)
    with pytest.raises(HubError):validator.validate_result(last_service,current,current['payload'],current['result'][STATE],current['result'][STATE]['result_observation'])
    from services.hub.replay_part_publish_worker_v5 import PartReplayBrowser
    monkeypatch.setattr(PartReplayBrowser,'session',lambda *a,**k:pytest.fail('32-P retention cannot requery the platform'))
    historical=service._public(f.parent)
    assert len(historical)==32 and len({p['external_id'] for p in historical})==32
    assert all(p['historical_confirmation'] is True and p['current_access_requeried'] is False for p in historical)
    assert max(p['original_accepted_at'] for p in historical)<time.time()-300
    assert Path(first['file']).read_bytes()==saved
