"""Offline deployment scope and backup preservation; no host/network calls."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest
from scripts.ops import deploy_source_inventory_v11 as wrapper

@pytest.fixture
def op():return wrapper.operator()

def test_pending_actual_pins_prevent_activation_and_backup_baseline_is_mandatory(op,monkeypatch):
    # The deployed template may now contain real evidence; force only this
    # owned case back to an incomplete candidate to exercise fail-closed.
    monkeypatch.setattr(op,'IMAGE','PENDING_ACTUAL_IMAGE')
    assert not op.activation_constants_ready()
    assert op.SOURCES=={'adapters/ytdlp_whole_v4.py':wrapper.WHOLE_SHA}
    assert op.PARENT141_SHA==wrapper.PARENT45_SHA and op.STATE==Path('/var/lib/lighthouse-source-inventory-v11')
    with pytest.raises(ValueError,match='BACKUP_FINGERPRINT_BASELINE_REQUIRED'):op.backup_guard()

def test_frozen_parent_and_exact_one_replacements(monkeypatch):
    raw=(Path(__file__).parents[2]/'scripts/ops/deploy_download_access_v9.py').read_bytes()
    with pytest.raises(ValueError,match='PARENT_BYTES_CHANGED'):wrapper.derive(raw+b'\n')
    for old,_ in wrapper.REPLACEMENTS:
        changed=raw+('\n# '+old+'\n').encode()
        monkeypatch.setattr(wrapper,'PARENT_HELPER_SHA',wrapper.hashlib.sha256(changed).hexdigest())
        with pytest.raises(ValueError,match='EXACT_ONE'):wrapper.derive(changed)

def test_only_video_image_and_readonly_qualification_mount_can_change(op):
    op.IMAGE='sha256:'+'b'*64;op.PROOF=Path('/owned/new-proof/test-attestation.json')
    before={'services':{'video-worker':{'image':op.OLD_IMAGE,'volumes':[{'target':'/run/qualifications/retention-v5',
        'source':str(op.OLD_PROOF),'read_only':True,'type':'bind','bind':{'create_host_path':False}}]},
        'tweet-worker':{'image':'unchanged'}}}
    after=op.candidate(before,op.PROOF.parent);back=deepcopy(after)
    back['services']['video-worker']['image']=op.OLD_IMAGE
    back['services']['video-worker']['volumes'][0]['source']=str(op.OLD_PROOF)
    assert back==before and after['services']['video-worker']['volumes'][0]['read_only']

def test_release_must_retain_backup_and_all_unrelated_sources(op):
    before={'test_attestation':{'sha256':op.OLD_PROOF_SHA},'authorization_sha256':op.OWNER_SHA,
        'source_files':{'adapters/ytdlp_whole_v4.py':'a'*64,'services/hub/cloudbase_backup_runtime.py':'c'*64}}
    after=deepcopy(before);after['source_files'].update(op.SOURCES);op.validate_release_change(before,after)
    after['source_files']['services/hub/cloudbase_backup_runtime.py']='d'*64
    with pytest.raises(ValueError,match='ONLY_QUALIFIED'):op.validate_release_change(before,after)

def test_exact_ten_to_eleven_layers_preserves_complete_security_configuration(op):
    op.IMAGE='sha256:'+'b'*64
    before={'Id':op.OLD_IMAGE,'RootFS':{'Layers':[str(i) for i in range(10)]},'Config':{'User':'10001:10001'},
        'Os':'linux','Architecture':'amd64','Variant':''}
    after=deepcopy(before);after['Id']=op.IMAGE;after['RootFS']['Layers'].append('one delta')
    op.validate_image_lineage(before,after);after['Config']['User']='0'
    with pytest.raises(ValueError,match='CONFIG'):op.validate_image_lineage(before,after)

def fake_systemctl(*,enabled='enabled',timer='active',service='failed',pid='0',started='17'):
    def run(args,**kwargs):
        assert args[:2]==['/usr/bin/systemctl','show'] and 'stop' not in args and 'start' not in args
        values={'UnitFileState':enabled,'ActiveState':timer,'SubState':'waiting'}
        if args[2].endswith('.service'):
            values={'UnitFileState':'static','ActiveState':service,'SubState':'failed' if service=='failed' else 'dead',
                'MainPID':pid,'ExecMainStartTimestampMonotonic':started,'ExecMainExitTimestampMonotonic':'18'}
        return SimpleNamespace(returncode=0,stdout='\n'.join(k+'='+v for k,v in values.items()))
    return run

def test_enabled_active_backup_timer_is_accepted_and_preserved():
    run=fake_systemctl();before=wrapper.backup_fingerprint(run)
    assert before['lighthouse-cloudbase-backup.timer']['UnitFileState']=='enabled'
    wrapper.preserve_backup(before,run)

@pytest.mark.parametrize('change',[{'enabled':'disabled'},{'timer':'inactive'},{'started':'19'}])
def test_backup_fingerprint_changes_are_refused_without_reconfiguring_it(change):
    before=wrapper.backup_fingerprint(fake_systemctl())
    with pytest.raises(ValueError,match='FINGERPRINT_CHANGED'):wrapper.preserve_backup(before,fake_systemctl(**change))

@pytest.mark.parametrize('change',[{'service':'active'},{'pid':'123'}])
def test_running_backup_service_blocks_video_cutover(change):
    with pytest.raises(ValueError,match='BACKUP_SERVICE_RUNNING'):wrapper.backup_fingerprint(fake_systemctl(**change))
