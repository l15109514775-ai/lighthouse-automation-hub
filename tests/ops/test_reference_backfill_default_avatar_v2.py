"""Exact per-avatar permission; no HTTP, API, model or production source claims."""
from copy import deepcopy
from types import SimpleNamespace
import uuid
import pytest
from services.hub.config import Settings
from services.hub.errors import HubError
from scripts.ops import oneoff_reference_backfill_v2 as m


def fixture(runtime):
    settings, store = runtime
    settings.config['allowed_media_hosts'] = ['pbs.twimg.com']
    settings.config[m.AVATAR_CONFIG_KEY] = deepcopy(m.AVATAR_CONFIG)
    job = {'job_id': str(uuid.uuid4()), 'source_platform': 'x', 'source_id': '2116863407962108290'}
    source = {'proof': {'source_live_verified': True}, 'nodes': [{'role': 'quote', 'source': {
        'source_api': 'x-v2-official', 'official_source': {'version': m.SOURCE_VERSION},
        'capture': {'official_api_receipt': True}, 'author': {'id': '123', 'username': 'owned_fixture',
            'profile_image_url': m.DEFAULT_AVATAR_URL}}}]}
    destination = settings.root/'media'/job['job_id']/'machine-api-relations-v2'/str(uuid.uuid4())/'quote-avatar.bin'
    return settings, store, job, source, destination


def test_only_exact_api_bound_default_avatar_gets_per_call_host_without_config_change(runtime):
    settings, store, job, source, destination = fixture(runtime);before = deepcopy(settings.config)
    hosts, author = m.avatar_download_hosts(settings, job, source, 'machine-api-relations-v2', m.DEFAULT_AVATAR_URL, destination)
    assert hosts == ['pbs.twimg.com', 'abs.twimg.com'] and author['username'] == 'owned_fixture'
    assert settings.config == before


@pytest.mark.parametrize('change', ['path', 'non_api_node', 'role'])
def test_arbitrary_abs_path_unverified_node_and_non_avatar_target_are_rejected(runtime, change):
    settings, store, job, source, destination = fixture(runtime);url = m.DEFAULT_AVATAR_URL
    if change == 'path':url = 'https://abs.twimg.com/other/asset.png'
    elif change == 'non_api_node':source['nodes'][0]['source']['source_api'] = 'manual-input'
    else:destination = destination.with_name('unrelated-photo.bin')
    with pytest.raises(HubError):m.avatar_download_hosts(settings, job, source, 'machine-api-relations-v2', url, destination)
