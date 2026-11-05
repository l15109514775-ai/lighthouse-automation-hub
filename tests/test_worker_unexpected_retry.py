"""Unexpected runtime errors retry only resumable acquisition jobs.

A worker crash must not strand a download that still has attempts and retained
partials, while an unexpected error after a real publication attempt must never be
replayed. The queue, lease, attempts and database are the genuine ones.
"""
from services.hub.worker import Worker


def boom(job):
    raise RuntimeError('owned unexpected failure')


def test_resumable_download_retries_without_resetting_attempts(runtime):
    settings, store = runtime
    job_id = store.enqueue('youtube', 'owned-source', 'youtube_replay_download_v4', 'video',
                           {'owned': True}, 'replay-fifo-v4')
    worker = Worker(settings, store, {'youtube_replay_download_v4': boom})
    assert worker.once('video') is True
    job = store.get(job_id)
    assert (job['state'], job['error_code'], job['attempts']) == ('retry_scheduled', 'UNEXPECTED_RUNTIME_ERROR', 1)
    assert job['lease_owner'] is None and job['next_run'] > 0


def test_publication_job_stays_failed_after_an_unexpected_error(runtime):
    settings, store = runtime
    job_id = store.enqueue('youtube', 'owned-source', 'replay_multipart_publish_v4', 'video',
                           {'owned': True}, 'replay-fifo-v4')
    worker = Worker(settings, store, {'replay_multipart_publish_v4': boom})
    assert worker.once('video') is True
    job = store.get(job_id)
    assert (job['state'], job['error_code'], job['attempts']) == ('failed', 'UNEXPECTED_RUNTIME_ERROR', 1)
    assert not store.query("SELECT 1 FROM jobs WHERE state='retry_scheduled'")
