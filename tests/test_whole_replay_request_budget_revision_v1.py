"""Owned redirect-budget revision checks; no media, network or production DB."""
from pathlib import Path

import pytest

from adapters import ytdlp_highest_v2 as engine
from services.hub.errors import HubError
from services.hub.replay_multipart_v4 import digest
from scripts.ops import revise_whole_replay_redirect_budget_v1 as operator


def plans():
    old = {
        'source_bytes': 7_782_099_687,
        'media_requests_plan': 782,
        'metadata_request_allowance': 64,
        'redirect_resume_request_allowance': 196,
        'max_http_requests': 1042,
    }
    new = {**old, 'redirect_resume_request_allowance': 782, 'max_http_requests': 1628}
    return old, new


class Store:
    def __init__(self, requests=1042):
        self.requests = requests

    def query(self, sql, args):
        assert 'SUM(units)' in sql
        return [{'n': self.requests}]


def revision(selection_sha='a' * 64):
    old, new = plans()
    return {
        'version': 'youtube-whole-request-budget-revision-v1',
        'job_id': 'owned-job',
        'previous_attempts': 2,
        'old_source_plan': old,
        'new_source_plan': new,
        'old_source_plan_sha256': digest(old),
        'new_source_plan_sha256': digest(new),
        'old_binding_sha256': 'b' * 64,
        'evidence': {'selection_sha256': selection_sha},
    }


def write_http(folder, plan, requests=1042):
    engine.atomic_json(
        folder / 'download-result.json',
        {
            'error_code': 'YOUTUBE_HIGHEST_REQUEST_LIMIT',
            'source_plan': plan,
            'partial_files_retained': True,
            'http': {
                'budget_history_reset': False,
                'provider': engine.PROVIDER,
                'request_limit': plan['max_http_requests'],
                'requests': requests,
                'response_bytes': 5_830_235_619,
                'response_kinds': {'html': 465, 'media': 569, 'json': 4, 'javascript': 2, 'manifest': 2},
            },
        },
    )


def test_observed_redirect_revision_reuses_exact_ledger_and_new_bound(tmp_path):
    old, new = plans()
    value = revision()
    job = {
        'job_id': 'owned-job',
        'attempts': 3,
        'result': {'download_request_budget_revision_v1': value},
    }
    binding = {'source_plan': new}
    write_http(tmp_path, old)
    carried = engine.resume_http_stats(Store(), job, binding, tmp_path)
    assert carried['requests'] == 1042
    assert carried['request_limit'] == 1628
    assert carried['response_kinds']['html'] == 465
    assert carried['response_kinds']['media'] == 569


def test_derived_whole_plan_digest_may_change_with_the_same_redirect_revision():
    old, new = plans()
    old['whole_plan_sha256'] = 'a' * 64
    new['whole_plan_sha256'] = 'b' * 64
    value = revision()
    value['old_source_plan'] = old
    value['new_source_plan'] = new
    value['old_source_plan_sha256'] = digest(old)
    value['new_source_plan_sha256'] = digest(new)
    job = {'job_id': 'owned-job', 'attempts': 3, 'result': {'download_request_budget_revision_v1': value}}
    result = engine.request_budget_revision(job, {'source_plan': new})
    assert result['old_plan'] == old and result['new_plan'] == new


@pytest.mark.parametrize(
    'change',
    [
        'wrong_job',
        'attempt_gap',
        'old_limit',
        'new_limit',
        'binding',
        'selection',
        'bad_http_history',
    ],
)
def test_revision_fails_closed_on_any_bound_change(tmp_path, change):
    old, new = plans()
    value = revision()
    job = {
        'job_id': 'owned-job',
        'attempts': 3,
        'result': {'download_request_budget_revision_v1': value},
    }
    binding = {'source_plan': new}
    write_http(tmp_path, old)
    if change == 'wrong_job':
        job['job_id'] = 'different'
    elif change == 'attempt_gap':
        job['attempts'] = 4
    elif change == 'old_limit':
        value['old_source_plan']['max_http_requests'] = 1043
    elif change == 'new_limit':
        binding['source_plan']['max_http_requests'] = 1629
    elif change == 'binding':
        binding['source_plan'] = {**new, 'source_bytes': new['source_bytes'] + 1}
    elif change == 'selection':
        value['evidence']['selection_sha256'] = 'bad'
    elif change == 'bad_http_history':
        write_http(tmp_path, old, requests=1041)
    with pytest.raises(HubError, match='YOUTUBE_HIGHEST_HTTP_HISTORY'):
        engine.resume_http_stats(Store(), job, binding, tmp_path)


def test_operator_changes_only_the_two_derived_request_fields():
    old, new = plans()
    whole = {'source_plan_basis': dict(old)}
    revised = operator.revise_whole(whole)
    assert revised['source_plan_basis'] == new
    assert {key for key in old if old[key] != revised['source_plan_basis'][key]} == {
        'redirect_resume_request_allowance',
        'max_http_requests',
    }
