from services.hub.observer_snapshot_v1 import is_normal_wait


def test_identity_change_is_normal_wait_not_failure():
    row = {'state': 'awaiting_review', 'error_code': 'REPLAY_FIFO_SOURCE_IDENTITY_CHANGED',
           'queue': 'video', 'job_type': 'youtube_replay_plan_v4'}
    assert is_normal_wait(row) is True


def test_real_review_is_still_an_error():
    row = {'state': 'awaiting_review', 'error_code': 'HUMAN_SOURCE_AND_TRANSLATION_REVIEW_REQUIRED',
           'queue': 'tweet', 'job_type': 'tweet_process'}
    assert is_normal_wait(row) is False


def test_failed_identity_job_is_not_normal_wait():
    row = {'state': 'failed', 'error_code': 'REPLAY_FIFO_SOURCE_IDENTITY_CHANGED',
           'queue': 'video', 'job_type': 'youtube_replay_plan_v4'}
    assert is_normal_wait(row) is False
