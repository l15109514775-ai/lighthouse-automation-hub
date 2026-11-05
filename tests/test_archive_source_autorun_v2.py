"""Owned actual archive/SQLite/cleanup/recovery; no remote calls or real files."""
import json,os
from pathlib import Path
import pytest

from test_archive_source_retention_v2 import owned,release as base_release,acceptance,sha
from services.hub.archive_source_autorun_v2 import ArchiveSourceDailyAutorunV2,_DailyRetention
from services.hub import archive_source_retention_v2 as core
from services.hub.cloudbase_backup_runtime import OperationalArchive
from services.hub.errors import HubError
from services.hub.db import encode


def release(f,monkeypatch):
    result=base_release(f,monkeypatch)
    if os.name=='nt':
        original=_DailyRetention._unlink_exact
        def windows_owned_attribute(self,item):
            if Path(item['file'])==f['sealed']:
                actual=core._inspect(f['settings'].root,f['sealed']);assert all(actual[key]==item[key] for key in actual)
                f['sealed'].chmod(0o600)  # Owned Windows attribute boundary, no production change.
            return original(self,item)
        monkeypatch.setattr(_DailyRetention,'_unlink_exact',windows_owned_attribute)
    return result


def run(f,*,apply=False,autorun=None):
    return (autorun or ArchiveSourceDailyAutorunV2(f['settings'],f['store'])).run_once(f['report'],sha(f['report'].read_bytes()),apply=apply)


def test_daily_dry_run_then_explicit_policy_apply_is_persistent_and_no_second_owner_request(owned,monkeypatch):
    f=owned;release(f,monkeypatch)
    first=run(f);assert first['state']=='planned' and first['source_files_deleted'] is None and f['image'].exists()
    result=run(f,apply=True)
    assert result['state']=='completed' and result['source_files_deleted']==4 and result['owner_reconfirmation_required'] is False
    assert not f['image'].exists() and f['active'].exists()
    assert run(f,apply=True)==result


def test_single_held_or_unavailable_file_does_not_fail_other_candidates(owned,monkeypatch):
    f=owned;release(f,monkeypatch);f['image'].unlink()
    result=run(f,apply=True)
    assert result['state']=='completed' and result['source_files_deleted']==3
    assert any(item['error_code']=='ARCHIVE_RETENTION_FILE_UNAVAILABLE' for item in result['rejected'])
    assert not f['source'].exists() and f['active'].exists()


def test_unresolved_publication_is_retained_but_verified_sealed_log_can_clean(owned,monkeypatch):
    f=owned;release(f,monkeypatch)
    with f['store'].transaction() as db:db.execute("UPDATE publications SET state='unknown'")
    result=run(f,apply=True)
    assert result['source_files_deleted']==1 and f['image'].exists() and not f['sealed'].exists()
    assert len(result['rejected'])==3


def test_new_source_user_between_plan_and_apply_rejects_without_deletion(owned,monkeypatch):
    f=owned;release(f,monkeypatch);run(f)
    f['store'].enqueue('maintenance','owned-reader','owned-reader','system',{'file':str(f['image'])},'owned')
    result=run(f,apply=True)
    assert result['state']=='retained_after_revalidation_change' and f['image'].exists() and f['sealed'].exists()


def test_apply_crash_journal_survives_and_dry_run_never_resumes_unlink(owned,monkeypatch):
    f=owned;release(f,monkeypatch);a=ArchiveSourceDailyAutorunV2(f['settings'],f['store']);original=a.retention._unlink_exact;calls=[]
    def crash(item):calls.append(item['file']);original(item);raise RuntimeError('owned process exit after unlink')
    monkeypatch.setattr(a.retention,'_unlink_exact',crash)
    with pytest.raises(RuntimeError):run(f,apply=True,autorun=a)
    before=list(calls);result=run(f,autorun=a)
    assert result['state']=='applying' and calls==before
    monkeypatch.setattr(a.retention,'_unlink_exact',original)
    assert run(f,apply=True,autorun=a)['source_files_deleted']==4


def test_later_backup_same_bytes_does_not_break_exact_old_intent_resume(owned,monkeypatch):
    f=owned;release(f,monkeypatch);a=ArchiveSourceDailyAutorunV2(f['settings'],f['store']);original=a.retention._unlink_exact
    def once(item):original(item);raise OSError('owned interruption after unlink')
    monkeypatch.setattr(a.retention,'_unlink_exact',once)
    first=run(f,apply=True,autorun=a);assert first['state']=='applying'
    bundle=OperationalArchive(f['settings'].root,f['limits']).build(__import__('uuid').uuid4().__str__())
    report,reader,*_=acceptance(f['settings'],bundle)
    a.retention.register_verified_archive(report,ledger_reader=reader)
    monkeypatch.setattr(a.retention,'_unlink_exact',original)
    assert run(f,apply=True,autorun=a)['state']=='completed'
    assert not f['image'].exists() and not f['source'].exists()


def test_live_author_cache_is_checked_inside_core_business_gate(owned,monkeypatch):
    f=owned;release(f,monkeypatch);a=ArchiveSourceDailyAutorunV2(f['settings'],f['store'])
    # The root file itself is a simple owned image; patch only the cache-index
    # reader boundary to prove the same database-transaction safety fence.
    monkeypatch.setattr('services.hub.archive_source_autorun_v2._cache_files',lambda *_:{str(f['image'])})
    result=run(f,apply=True,autorun=a)
    assert result['state']=='completed' and result['source_files_deleted']==3 and f['image'].exists()
    assert any(item['error_code']=='ARCHIVE_RETENTION_ACTIVE_SOURCE_CACHE' for item in result['rejected'])


def test_missing_true_owner_release_never_starts_automatic_cleanup(owned):
    with pytest.raises((HubError,OSError)):run(owned,apply=True)
    assert owned['image'].exists() and owned['sealed'].exists()


def test_daily_batch_is_bounded_and_deferred_files_remain_for_next_operation(owned,monkeypatch):
    f=owned;release(f,monkeypatch)
    monkeypatch.setattr('services.hub.archive_source_autorun_v2.MAX_FILES_PER_RUN',1)
    result=run(f,apply=True)
    assert result['state']=='completed' and result['source_files_deleted']==1
    assert sum(item['error_code']=='ARCHIVE_RETENTION_BATCH_LIMIT_DEFERRED' for item in result['rejected'])==3
    assert f['image'].exists() and f['source'].exists()
