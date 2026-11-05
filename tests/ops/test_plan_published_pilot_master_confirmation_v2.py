"""Owned historical protocol/SQLite fixtures; no actual platform or movie claim.

Reuse the old eight fixture's named root/acceptance seams, keeping real six-ref
reads, public/upload validators, SQLite, current file hashing and old gate.
Only owned tiny files are unlinked by the mature primitive; no production main.
"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import base64,importlib.util,json,os,sqlite3,time

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub import replay_later_native_result_v1 as later
from test_cleanup_published_pilot_master_once_v1 import owned,op as legacy,put,stamp

file=Path(__file__).resolve().parents[2]/'scripts/ops/plan_published_pilot_master_confirmation_v2.py'
spec=importlib.util.spec_from_file_location('owned_public_cleanup_plan',file)
plan=importlib.util.module_from_spec(spec);spec.loader.exec_module(plan)


@pytest.fixture
def confirmation_case(owned,monkeypatch):
    f=owned;job,binding,state,source,pub=f.ret._context(legacy.JOB)
    folder=f.ret.root/'media'/legacy.JOB/plan.PLAYBACK_FOLDER
    folder.mkdir(parents=True)
    # A valid tiny PNG is an owned screenshot-format fixture, not UI evidence.
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/RpkAAAAASUVORK5CYII=')
    body=json.loads(Path(f.public['file']).read_bytes())
    body.update(version='known-bv-public-playback-inventory-v2',observed_at=stamp(f.at+9),player_seeks=2)
    body['visible_dom']['descriptions'][0]['text']=binding['metadata']['description']
    body['visible_dom']['videos'][0].update(paused=False,current_time=25708.0)
    samples=[]
    for label,target,offset in (('middle',12857.5,1),('near-end',25705.0,9)):
        image=folder/('public-'+label+'.png');image.write_bytes(png)
        ref={'file':str(image),'bytes':len(png),'sha256':legacy.sha(png)}
        player=deepcopy(body['visible_dom']['videos'][0]);player['current_time']=target+3
        samples.append({'label':label,'target_time':target,'play_requested':True,'observed_at':stamp(f.at+offset),
            'videos':[player],'screenshot':ref})
    body['visible_dom']['playback_samples']=samples
    ref=put(folder/'public-native-inventory.json',body)
    monkeypatch.setattr(plan,'PLAYBACK_SHA',ref['sha256'])  # Explicit owned raw-capture pin only.
    return SimpleNamespace(f=f,body=body,ref=ref,file=Path(ref['file']))


def create(c):return plan.create_plan(c.f.ret,legacy,later,c.ref)


def test_separate_dry_plan_verifies_six_refs_without_recovering_or_weakening_old_seventh(confirmation_case,monkeypatch):
    c=confirmation_case;f=c.f
    missing=Path(f.evidence['manager_before_submit']['file']);missing.write_bytes(b'owned later overwritten inventory')
    with pytest.raises(HubError):legacy.historical_confirmation(f.ret,later)
    before=f.store.query('SELECT * FROM jobs');pubs=f.store.query('SELECT * FROM publications');audits=f.store.query('SELECT * FROM audit')
    def forbidden(*args):raise AssertionError('A dry plan must not unlink')
    monkeypatch.setattr(f.ret,'_stat_unlink',forbidden)
    scanner=plan._inspect
    monkeypatch.setattr(plan,'_inspect',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Metadata plan must not scan the movie')))
    value=create(c)
    assert value['candidate_count']==1 and value['candidate']['file']==str(f.master)
    assert value['files_deleted']==0 and value['platform_requests']==0 and value['apply_allowed'] is False
    assert value['current_master_full_sha_verified'] is False and value['candidate']['expected_sha256']==legacy.MASTER_SHA
    history=value['historical_public_confirmation']
    assert len(history['intact_historical_references'])==6 and history['pre_submit_inventory_used'] is False
    assert history['unavailable_legacy_reference_preserved']==f.evidence['manager_before_submit']
    assert history['public_playback']['middle_and_near_end_playback_confirmed'] is True
    assert f.master.is_file() and f.store.query('SELECT * FROM jobs')==before
    assert f.store.query('SELECT * FROM publications')==pubs and f.store.query('SELECT * FROM audit')==audits
    monkeypatch.setattr(plan,'_inspect',scanner)
    hashed=plan.create_plan(f.ret,legacy,later,c.ref,verify_current_master=True)
    assert hashed['current_master_full_sha_verified'] is True and hashed['candidate']['sha256']==legacy.MASTER_SHA


def test_any_required_six_reference_tamper_still_blocks_plan(confirmation_case):
    c=confirmation_case
    Path(c.f.evidence['original_machine_proof']['file']).write_bytes(b'changed owned proof')
    with pytest.raises(HubError):create(c)
    assert c.f.master.exists()


@pytest.mark.parametrize('change',['no_playback_advance','wrong_up'])
def test_coherent_playback_raw_hash_cannot_replace_semantic_player_or_owner_checks(confirmation_case,monkeypatch,change):
    c=confirmation_case
    if change=='no_playback_advance':
        sample=c.body['visible_dom']['playback_samples'][1];sample['videos'][0]['current_time']=sample['target_time']
    else:c.body['visible_dom']['owner_candidates'][0]['url']='https://space.bilibili.com/999/'
    c.ref=put(c.file,c.body);monkeypatch.setattr(plan,'PLAYBACK_SHA',c.ref['sha256'])
    with pytest.raises(HubError):create(c)
    assert c.f.master.exists()


def test_completed_publication_and_real_original_audit_are_both_required(confirmation_case):
    c=confirmation_case;f=c.f
    with f.store.transaction() as db:db.execute("UPDATE publications SET state='unknown'")
    with pytest.raises(HubError):create(c)
    with f.store.transaction() as db:
        db.execute("UPDATE publications SET state='completed'")
        db.execute('DELETE FROM audit WHERE action=?',(later.VERSION,))
    with pytest.raises(HubError,match='COMPLETED_AUDIT'):create(c)
    assert f.master.exists()


def test_shared_dependency_and_same_size_master_change_never_become_a_dryplan_candidate(confirmation_case):
    c=confirmation_case;f=c.f
    ident=f.store.enqueue('maintenance','owned-reader','owned-reader','system',{'file':str(f.master)},'owned')
    with f.store.transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(ident,))
    with pytest.raises(HubError,match='SHARED_ORIGINAL'):create(c)
    with f.store.transaction() as db:db.execute('DELETE FROM jobs WHERE job_id=?',(ident,))
    before=f.master.stat();raw=f.master.read_bytes();f.master.write_bytes(b'X'+raw[1:])
    os.utime(f.master,ns=(before.st_atime_ns,before.st_mtime_ns))
    with pytest.raises(HubError,match='CURRENT_MASTER_CHANGED'):
        plan.create_plan(f.ret,legacy,later,c.ref,verify_current_master=True)
    assert f.master.exists()


def full_plan(c):return plan.create_plan(c.f.ret,legacy,later,c.ref,verify_current_master=True)


def apply(c,value,checksum=None):
    with sqlite3.connect(c.f.index) as index:
        return plan.apply_plan(c.f.ret,legacy,later,value,checksum or legacy.digest(value),index)


def test_apply_refuses_metadata_only_wrong_saved_sha_or_changed_operator(confirmation_case):
    c=confirmation_case
    with pytest.raises(HubError,match='EXACT_FULL_SHA_PLAN'):apply(c,create(c))
    value=full_plan(c)
    with pytest.raises(HubError,match='EXACT_FULL_SHA_PLAN'):apply(c,value,'0'*64)
    value['operator_sha256']='0'*64
    with pytest.raises(HubError,match='OPERATOR_CHANGED'):apply(c,value)
    assert c.f.master.exists() and not c.f.store.query('SELECT 1 FROM audit WHERE action=?',(plan.RECEIPT_VERSION,))


def test_full_hash_plan_unlinks_only_master_once_and_preserves_every_business_field(confirmation_case,monkeypatch):
    c=confirmation_case;f=c.f;calls=[];scans=[];scanner=plan._inspect;unlink=f.ret._stat_unlink
    Path(f.evidence['manager_before_submit']['file']).write_bytes(b'owned overwritten old inventory stays unchanged')
    sample=f.ret.root/'media/owned-private-samples/part-001.mp4';sample.parent.mkdir();sample.write_bytes(b'owned private sample')
    monkeypatch.setattr(plan,'_inspect',lambda *a,**k:(scans.append(a[1]),scanner(*a,**k))[1])
    monkeypatch.setattr(f.ret,'_stat_unlink',lambda *a:(calls.append(a[0]['file']),unlink(*a))[1])
    before={ident:f.store.get(ident) for ident in (legacy.JOB,legacy.DOWNLOAD)}
    pubs=f.store.query('SELECT * FROM publications');value=full_plan(c);result=apply(c,value)
    assert not f.master.exists() and result['local_files_deleted']==1 and result['file_bytes']==legacy.BYTES
    assert result['historical_confirmation'] is True and result['current_access_requeried'] is False and result['platform_requests']==0
    assert scans==[f.master] and calls==[str(f.master)]
    assert apply(c,value)==result and calls==[str(f.master)]
    for ident,old in before.items():
        current=f.store.get(ident);marker=current['result'].pop(plan.MARKER);current['updated_at']=old['updated_at']
        assert marker==result and current==old  # Includes states, attempts, payload, lease, BV and source custody.
    assert f.store.query('SELECT * FROM publications')==pubs
    assert len(f.store.query('SELECT * FROM audit WHERE action=?',(plan.RECEIPT_VERSION,)))==1
    assert Path(f.public['file']).exists() and sample.read_bytes()==b'owned private sample'
    assert Path(f.evidence['manager_before_submit']['file']).read_bytes()==b'owned overwritten old inventory stays unchanged'


@pytest.mark.parametrize('change',['same_size','missing'])
def test_changed_or_missing_master_after_full_plan_is_never_assumed_safe(confirmation_case,change):
    c=confirmation_case;f=c.f;value=full_plan(c);before=f.master.stat();raw=f.master.read_bytes()
    if change=='missing':
        f.master.unlink()
        with pytest.raises(HubError,match='UNEXPECTED_MISSING'):apply(c,value)
        assert plan.MARKER not in f.store.get(legacy.JOB)['result'];return
    time.sleep(.01);f.master.write_bytes(b'X'+raw[1:])
    # Linux ctime fences writes even if mtime is restored. Windows st_ctime is
    # creation time; its local check keeps the genuine changed mtime instead.
    # Production main explicitly requires POSIX; Ubuntu exercises restored mtime.
    if os.name=='posix':os.utime(f.master,ns=(before.st_atime_ns,before.st_mtime_ns))
    with pytest.raises(HubError,match='ORIGINAL_CHANGED'):apply(c,value)
    assert f.master.exists() and plan.MARKER not in f.store.get(legacy.JOB)['result']


def test_post_plan_unknown_publication_shared_or_processing_dependency_blocks_unlink(confirmation_case):
    c=confirmation_case;f=c.f;value=full_plan(c)
    with f.store.transaction() as db:db.execute("UPDATE publications SET state='unknown'")
    with pytest.raises(HubError,match='PUBLICATION_UNRESOLVED'):apply(c,value)
    with f.store.transaction() as db:db.execute("UPDATE publications SET state='completed'")
    ident=f.store.enqueue('maintenance','owned-reader','owned-reader','system',{'file':str(f.master)},'owned')
    with f.store.transaction() as db:db.execute("UPDATE jobs SET state='completed' WHERE job_id=?",(ident,))
    with pytest.raises(HubError,match='SHARED_ORIGINAL'):apply(c,value)
    with f.store.transaction() as db:
        db.execute("UPDATE jobs SET payload='{}',queue='video',state='processing' WHERE job_id=?",(ident,))
    with pytest.raises(HubError,match='VIDEO_WORKER_ACTIVE'):apply(c,value)
    assert f.master.exists() and plan.MARKER not in f.store.get(legacy.JOB)['result']


def test_crash_after_real_unlink_recovers_only_exact_durable_intent_without_second_unlink(confirmation_case,monkeypatch):
    c=confirmation_case;f=c.f;value=full_plan(c);original=f.ret._stat_unlink;calls=[]
    def crash(item,ctime):
        calls.append(item['file']);original(item,ctime);raise OSError('owned crash immediately after unlink')
    monkeypatch.setattr(f.ret,'_stat_unlink',crash)
    with pytest.raises(OSError):apply(c,value)
    assert not f.master.exists() and plan.MARKER not in f.store.get(legacy.JOB)['result']
    with sqlite3.connect(f.index) as index:
        assert index.execute('SELECT state FROM operations').fetchone()[0]=='deleting'
    result=apply(c,value)
    assert result['local_files_deleted']==1 and calls==[str(f.master)]
    assert len(f.store.query('SELECT * FROM audit WHERE action=?',(plan.RECEIPT_VERSION,)))==1


def test_main_commit_recovery_matches_both_markers_and_never_deletes_recreated_path(confirmation_case):
    c=confirmation_case;f=c.f;value=full_plan(c);result=apply(c,value)
    with sqlite3.connect(f.index) as index:
        index.execute("UPDATE operations SET state='deleting',record=?",(encode({'plan':value}),))
    assert apply(c,value)==result
    with sqlite3.connect(f.index) as index:assert index.execute('SELECT state FROM operations').fetchone()[0]=='completed'
    f.master.write_bytes(b'owned unrelated replacement must be retained')
    with pytest.raises(HubError,match='RECOVERY_CHANGED'):apply(c,value)
    assert f.master.read_bytes()==b'owned unrelated replacement must be retained'
    assert len(f.store.query('SELECT * FROM audit WHERE action=?',(plan.RECEIPT_VERSION,)))==1
