"""Real SQLite policy maintenance; no source refresh, job reset or HTTP."""
from copy import deepcopy
import json
import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_fifo_v4 import POLICY_FILE, sha
from test_replay_fifo_v4 import fifo, seed


def change(f):
    old=f.q.policy()['_sha256']
    new=deepcopy(f.p);new['planning_policy_sha256']=sha({'Owned future resource model':True})
    (f.s.root/'config'/POLICY_FILE).write_text(encode(new),encoding='utf-8')
    return old


def test_only_unstarted_rows_rebind_preserving_order_source_payload_and_no_new_jobs(fifo):
    f=fifo
    for index in (2,1):seed(f,index)
    f.q.register_all_observed();before=f.db.query('SELECT * FROM replay_fifo_v4 ORDER BY actual_end')
    old=change(f);result=f.q.rebind_unstarted_candidates(expected_previous_policy_sha256=old,maintenance_reference='Owned explicit resource-model change')
    assert result['rebound']==[r['candidate_id'] for r in before] and not result['retained']
    after=f.db.query('SELECT * FROM replay_fifo_v4 ORDER BY actual_end')
    for previous,current in zip(before,after):
        assert {k:v for k,v in previous.items() if k not in ('policy_sha256','updated_at')}=={k:v for k,v in current.items() if k not in ('policy_sha256','updated_at')}
        assert current['policy_sha256']==f.q.policy()['_sha256']
    assert not f.db.query('SELECT * FROM jobs') and not f.db.query('SELECT * FROM publications') and not f.db.query('SELECT * FROM api_calls')
    audits=f.db.query("SELECT * FROM audit WHERE action='replay_fifo_unstarted_policy_rebind'")
    assert len(audits)==2 and all(json.loads(row['outcome'])['old_policy_sha256']==old for row in audits)


@pytest.mark.parametrize('change_kind',['plan','refresh','hold','source_job','tombstone','publication','identity'])
def test_started_held_recorded_or_changed_source_is_retained_without_drift(fifo,change_kind):
    f=fifo;raw,_=seed(f,1);f.q.register_all_observed();candidate=f.db.query('SELECT * FROM replay_fifo_v4')[0]
    evidence=f.db.enqueue('maintenance','Owned-existing-ref','Owned-context','system',{},'Owned')
    with f.db.transaction() as db:
        if change_kind in ('plan','refresh'):
            db.execute('UPDATE replay_fifo_v4 SET '+('plan_job_id' if change_kind=='plan' else 'refresh_job_id')+'=? WHERE candidate_id=?',(evidence,candidate['candidate_id']))
        elif change_kind=='hold':db.execute("UPDATE replay_fifo_v4 SET state='hold' WHERE candidate_id=?",(candidate['candidate_id'],))
        elif change_kind=='identity':db.execute("UPDATE youtube_videos SET duration='PT2H' WHERE video_id=?",(raw['id'],))
        elif change_kind=='tombstone':db.execute('INSERT INTO source_tombstones(source_platform,source_id,reason,marked_at,evidence_job_id,origin) VALUES(?,?,?,?,?,?)',
            ('youtube',raw['id'],'OWNER_BACKLOG_CLEARED',f.clock[0],evidence,'owner_clear_all'))
        elif change_kind=='publication':db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('Owned unknown',evidence,'bilibili','youtube',raw['id'],'site_uid:'+f.p['target_site_uid'],'Owned','unknown',None,None,f.clock[0]))
    if change_kind=='source_job':f.db.enqueue('youtube',raw['id'],'Owned unrecognized type','video',{},'Owned')
    before=f.db.query('SELECT * FROM replay_fifo_v4');jobs=f.db.query('SELECT * FROM jobs');old=change(f)
    result=f.q.rebind_unstarted_candidates(expected_previous_policy_sha256=old,maintenance_reference='Owned')
    assert not result['rebound'] and len(result['retained'])==1
    assert f.db.query('SELECT * FROM replay_fifo_v4')==before and f.db.query('SELECT * FROM jobs')==jobs
    assert not f.db.query("SELECT * FROM audit WHERE action='replay_fifo_unstarted_policy_rebind'")


def test_wrong_old_sha_is_noop_and_reference_is_required(fifo):
    f=fifo;seed(f,1);f.q.register_all_observed();old=change(f)
    result=f.q.rebind_unstarted_candidates(expected_previous_policy_sha256='a'*64,maintenance_reference='Owned')
    assert result['rebound']==result['retained']==[]
    with pytest.raises(HubError,match='SCOPE_INVALID'):
        f.q.rebind_unstarted_candidates(expected_previous_policy_sha256=old,maintenance_reference='')
