"""Real bounded SQLite/file maintenance, no live account or HTTP calls."""
import sqlite3
from pathlib import Path
import pytest
from test_short_video_web import short_video
from test_video_source_retention_v2 import owned
from test_video_source_retention_tracks_v2 import tracks,planned,allow
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.video_retention_scheduler_v2 import VideoRetentionCompletedSchedulerV2


def scheduler(f):return VideoRetentionCompletedSchedulerV2(f['settings'],f['store'],retention=f['service'])


def test_default_scheduler_only_saves_a_plan_for_completed_bound_video(tracks):
    f=tracks;r=scheduler(f).run_once(f['auth']['authorization_id'])
    assert r['apply'] is False and r['processed'][0]['result']['local_files_deleted']==0
    assert f['video'].exists() and f['audio'].exists() and f['file'].exists()
    assert Path(r['report_file']).exists() and r['external_requests']==0


def test_authorized_scheduler_removes_three_files_once_and_preserves_publications(tracks,monkeypatch):
    f=tracks;allow(f,monkeypatch);before=f['store'].query('SELECT * FROM publications')
    r=scheduler(f).run_once(f['auth']['authorization_id'],apply=True)
    assert r['processed'][0]['result']['local_files_deleted']==3
    assert scheduler(f).run_once(f['auth']['authorization_id'],apply=True)['processed']==[]
    assert f['store'].query('SELECT * FROM publications')==before and f['public'].exists()


def test_processing_or_unknown_job_is_never_selected(tracks):
    f=tracks
    for state in ('processing','publish_unknown','awaiting_review'):
        with f['store'].transaction() as db:db.execute('UPDATE jobs SET state=? WHERE job_id=?',(state,f['id']))
        assert scheduler(f).run_once(f['auth']['authorization_id'],apply=True)['processed']==[]
    assert f['file'].exists() and f['video'].exists()


def test_ineligible_completed_job_retains_files_and_reports_reason(tracks):
    f=tracks
    with f['store'].transaction() as db:db.execute("UPDATE publications SET state='publish_unknown'")
    r=scheduler(f).run_once(f['auth']['authorization_id'],apply=True)
    assert r['processed']==[] and r['retained'][0]['error_code']=='VIDEO_RETENTION_PUBLICATION_UNRESOLVED'
    assert f['file'].exists() and f['video'].exists()


def test_scheduler_resumes_same_durable_plan_after_raw_unlink_crash(tracks,monkeypatch):
    f=tracks;allow(f,monkeypatch);p=planned(f);original=f['service']._stat_unlink
    def crash(item,ctime):original(item,ctime);raise OSError('owned machine exit')
    monkeypatch.setattr(f['service'],'_stat_unlink',crash)
    with pytest.raises(OSError):f['service'].apply(p)
    monkeypatch.setattr(f['service'],'_stat_unlink',original)
    r=scheduler(f).run_once(f['auth']['authorization_id'],apply=True)
    assert r['processed'][0]['resumed'] is True and r['processed'][0]['result']['plan_id']==p['plan_id']
    assert not f['file'].exists() and not f['audio'].exists()


def test_manual_pass_flag_and_wrong_max_jobs_do_not_schedule_work(tracks):
    f=tracks
    with pytest.raises(HubError):scheduler(f).run_once(f['auth']['authorization_id'],apply='true')
    with pytest.raises(HubError):scheduler(f).run_once(f['auth']['authorization_id'],max_jobs=4)
    r=scheduler(f).run_once(f['auth']['authorization_id'],apply=True)
    assert r['processed']==[] and r['retained'][0]['error_code']=='VIDEO_RETENTION_RELEASE_REQUIRED'
    assert f['file'].exists() and f['video'].exists()
