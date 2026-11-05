"""Pure deployment-plan checks. Never Docker, host files, accounts or SSH."""
from copy import deepcopy
from pathlib import Path
import pytest
from scripts.ops import deploy_highest_video_worker_v2 as d


def config():
    return {'name':'owned-compose','services':{name:{'image':'Owned unchanged image','env_file':[{'path':'/owned/secret-path-not-read'}]}
        for name in (*d.CORE,'video-worker')},'networks':{'default':{'name':'owned-network'}}}


def attestation():
    value={'version':'replay-highest-worker-ubuntu-tests-v2','image':d.IMAGE,'tests':234,'full_suite_passed':True,
        'failures':0,'errors':0,'skipped':0,'exit_code':0,'oom_killed':False,'archive_sha256':d.ARCHIVE,
        'network':'none','application_data_mounted':False,'production_profile_mounted':False,
        'secrets_environment_injected':False,'whole_tree_deployed':False,'runtime_readonly_overlays_only':True,
        'runtime_matches_declared_candidate':True,'mounts_match_isolated_allowlist':True,'no_pid_or_oom_denial':True,
        'real_uploads':0,'publications':0,'groups':d.GROUPS}
    value['manifest']={'expected_total_tests':234,'expected_test_counts':d.GROUPS,'base_image':d.IMAGE,
        'whole_tree_deployment':False,'application_data_included':False,'credentials_included':False,
        'source_freeze_commit':d.COMMIT,'files':{str(i):{} for i in range(82)}}
    return value


def test_only_video_object_changes_and_narrow_key_never_enters_env():
    before=config();frozen=deepcopy(before)
    service=d.new_video_service(Path('/owned/root-group-readable-key'),{'services/hub/runtime.py':b'Owned public source'})
    after=d.candidate_config(before,service)
    assert before==frozen
    assert all(before['services'][n]==after['services'][n] for n in d.CORE)
    assert service['image']==d.IMAGE and service['command']==['python','-m',d.ENTRY]
    assert 'env_file' not in service and service['ports']==[] and 'build' not in service
    assert service['user']=='10001:10001' and service['read_only'] and service['init']
    assert service['pids_limit']==256 and service['mem_limit']=='1400m' and service['shm_size']=='256m'
    assert set(service['environment'])=={'HUB_ROOT','HUB_CONFIG','DRY_RUN','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','PYTHONDONTWRITEBYTECODE','YOUTUBE_API_KEY_FILE'}
    assert 'YOUTUBE_API_KEY' not in service['environment']
    profile=d.ROOT/'data/browser-profiles'
    mounts={v['target']:v for v in service['volumes']}
    assert mounts[str(profile/'youtube')]['read_only'] is True
    assert mounts[str(profile/'bilibili-video')]['read_only'] is False
    assert mounts['/run/secrets/youtube-api-key']['read_only'] is True
    assert any(v.startswith(str(profile)+':') for v in service['tmpfs'])
    assert all('docker.sock' not in str(v) and v['bind']['create_host_path'] is False for v in service['volumes'])


@pytest.mark.parametrize('change',['image','env_file','core_count'])
def test_other_service_or_global_config_change_is_rejected(change):
    before=config();after=deepcopy(before)
    if change=='image':after['services']['tweet-worker']['image']='Owned changed'
    elif change=='env_file':after['services']['api']['env_file']=[]
    else:after['networks']={}
    with pytest.raises(AssertionError):d.assert_other_services(before,after)


@pytest.mark.parametrize('field,value',[('full_suite_passed',False),('tests',233),('network','bridge'),
    ('application_data_mounted',True),('no_pid_or_oom_denial',False),('archive_sha256','0'*64)])
def test_gate_rejects_partial_live_or_unbound_evidence(field,value):
    evidence=attestation();d.validate_attestation(evidence);evidence[field]=value
    with pytest.raises(AssertionError):d.validate_attestation(evidence)


def test_runtime_source_scope_excludes_test_examples_and_preserves_real_imports():
    assert d.runtime_source('services/hub/replay_highest_worker_runtime_v2.py')
    assert d.runtime_source('adapters/replay_native_driver.mjs')
    assert d.runtime_source('vendor/subtitle-cloud-worker/observer/store.py')
    assert d.runtime_source('scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs')
    assert not any(d.runtime_source(name) for name in ('tests/test_owned.py','config/hub.toml','config/policy.example.toml','docs/README.md','scripts/ops/other-root-helper.py'))


@pytest.mark.parametrize('jobs,processing,expected',[(0,0,True),(1,0,False),(0,1,False)])
def test_rollback_never_restores_unaware_consumer_after_v2_admission(jobs,processing,expected):
    assert d.can_rollback({'v2_jobs':jobs,'video_processing':processing}) is expected


@pytest.mark.parametrize('before,after',[
    (b'youtube_highest_download_enabled = false # Owned comment\r\nother = 7\r\n',b'youtube_highest_download_enabled = true # Owned comment\r\nother = 7\r\n'),
    (b'other = 7\n[[sources]]\nname = "Owned"\n',b'other = 7\nyoutube_highest_download_enabled = true\n[[sources]]\nname = "Owned"\n'),
    (b'youtube_highest_download_enabled=true\nother=7\n',b'youtube_highest_download_enabled=true\nother=7\n')])
def test_preflight_hub_candidate_preserves_all_other_config_without_mutating_input(before,after):
    original=bytes(before);assert d.candidate_hub(before)==after;assert before==original
