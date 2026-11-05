"""Pure source/mount proof checks only. No Docker, SSH or host mutation."""
from copy import deepcopy
from pathlib import Path
import pytest
from scripts.ops import apply_highest_video_worker_v2b as patch
from scripts.ops import deploy_highest_video_worker_v2 as deploy


def config():
    return {'name':'Owned','services':{**{key:{'image':'Owned unchanged'} for key in deploy.CORE},
        'video-worker':deploy.new_video_service(Path('/owned/key'),{patch.NAME:b'Owned source'})}}


def test_candidate_changes_exactly_one_readonly_source_mount_and_keeps_other_four():
    before=config();original=deepcopy(before);target=deploy.INSTALL/'deltas'/patch.COMMIT/patch.NAME
    after=patch.patch_config(before,target,deploy)
    assert before==original
    matches=[m for m in after['services']['video-worker']['volumes'] if m['target']==str(deploy.ROOT/patch.NAME)]
    assert len(matches)==1 and matches[0]['source']==str(target) and matches[0]['read_only'] is True
    changed=deepcopy(after);next(m for m in changed['services']['video-worker']['volumes'] if m['target']==str(deploy.ROOT/patch.NAME))['source']=str(deploy.INSTALL/'code'/patch.NAME)
    assert changed==before


@pytest.mark.parametrize('change',['foreign_source','duplicate','writable'])
def test_unexpected_source_mount_is_not_rewritten(change):
    before=config();volumes=before['services']['video-worker']['volumes'];bound=next(m for m in volumes if m['target']==str(deploy.ROOT/patch.NAME))
    if change=='foreign_source':bound['source']='/owned/unrecognized-handler.py'
    elif change=='duplicate':volumes.append(deepcopy(bound))
    else:bound['read_only']=False
    with pytest.raises(AssertionError):patch.patch_config(before,Path('/owned/new'),deploy)


@pytest.mark.parametrize('field',['freshness_seconds_changed','stored_observed_at_changed','final_reservation_verification_removed'])
def test_delta_never_admits_changed_freshness_or_removed_final_check(field):
    value={'version':'replay-highest-v2b-scan-delta-ubuntu-tests-v1','image':deploy.IMAGE,'archive_sha256':patch.ARCHIVE,
        'full_suite_passed':True,'tests':20,'groups':patch.GROUPS,'failures':0,'errors':0,'skipped':0,'exit_code':0,'oom_killed':False,
        'network':'none','runtime_readonly_overlays_only':True,'runtime_matches_declared_candidate':True,
        'mounts_match_isolated_allowlist':True,'no_pid_or_oom_denial':True,'application_data_mounted':False,
        'production_profile_mounted':False,'secrets_environment_injected':False,'whole_tree_deployed':False}
    value['manifest']={'version':'replay-highest-v2b-scan-delta-ro-overlay-v1','source_freeze_commit':patch.COMMIT,
        'parent_source_archive_sha256':deploy.ARCHIVE,'changed_files':[patch.NAME,'tests/test_replay_video_worker_native.py'],
        'production_runtime_changed_files':[patch.NAME],'expected_total_tests':20,'expected_test_counts':patch.GROUPS,
        'freshness_seconds_changed':False,'stored_observed_at_changed':False,'final_reservation_verification_removed':False,
        'files':{**{str(i):{} for i in range(81)},patch.NAME:{'sha256':patch.NEW}}}
    patch.validate_delta(value,deploy);value['manifest'][field]=True
    with pytest.raises(AssertionError):patch.validate_delta(value,deploy)
