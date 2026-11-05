"""No production side effects: inherited cutover gates remain exact."""
from copy import deepcopy
from pathlib import Path
import importlib.util
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('owned_editor_v10_deploy', ROOT/'scripts/ops/deploy_replay_editor_v10.py')
wrapper = importlib.util.module_from_spec(spec); spec.loader.exec_module(wrapper)


@pytest.fixture
def op(): return wrapper.operator()


def test_parent_bytes_and_exact_one_adaptations(monkeypatch):
    raw = (ROOT/'scripts/ops/deploy_download_access_v9.py').read_bytes()
    with pytest.raises(ValueError, match='PARENT_BYTES_CHANGED'): wrapper.derive(raw+b'\n')
    for old, _ in wrapper.REPLACEMENTS:
        changed = raw+('\n# '+old+'\n').encode()
        monkeypatch.setattr(wrapper, 'PARENT_HELPER_SHA', wrapper.hashlib.sha256(changed).hexdigest())
        with pytest.raises(ValueError, match='EXACT_ONE'): wrapper.derive(changed)


def test_unfilled_actual_pins_prevent_activation(op):
    op.IMAGE='PENDING_ACTUAL_IMAGE'
    assert not op.activation_constants_ready()
    assert op.SOURCES == {'adapters/replay_native_driver.mjs': wrapper.DRIVER_SHA}
    assert op.STATE == Path('/var/lib/lighthouse-replay-editor-v10')
    assert op.PARENT141_SHA == wrapper.PARENT200_SHA


def test_candidate_only_changes_video_image_and_readonly_proof(op):
    op.IMAGE='sha256:'+'b'*64; op.PROOF=Path('/owned/new-proof/test-attestation.json')
    before={'services':{'video-worker':{'image':op.OLD_IMAGE,'volumes':[{'target':'/run/qualifications/retention-v5',
      'source':str(op.OLD_PROOF),'read_only':True,'type':'bind','bind':{'create_host_path':False}}]},
      'tweet-worker':{'image':'unchanged'},'api':{'image':'unchanged'}}}
    after=op.candidate(before,op.PROOF.parent);back=deepcopy(after)
    back['services']['video-worker']['image']=op.OLD_IMAGE
    back['services']['video-worker']['volumes'][0]['source']=str(op.OLD_PROOF)
    assert back==before
    assert after['services']['video-worker']['volumes'][0]['read_only'] is True


def test_release_cannot_change_backup_or_another_runtime(op):
    op.SOURCES={'adapters/replay_native_driver.mjs':'b'*64}
    before={'test_attestation':{'sha256':op.OLD_PROOF_SHA},'authorization_sha256':op.OWNER_SHA,
      'source_files':{'adapters/replay_native_driver.mjs':'a'*64,'services/hub/cloudbase_backup_runtime.py':'c'*64}}
    after=deepcopy(before);after['source_files'].update(op.SOURCES)
    op.validate_release_change(before,after)
    after['source_files']['services/hub/cloudbase_backup_runtime.py']='d'*64
    with pytest.raises(ValueError,match='ONLY_QUALIFIED'):op.validate_release_change(before,after)


def test_image_ancestry_exact_nine_to_ten_and_no_security_change(op):
    op.IMAGE='sha256:'+'b'*64
    before={'Id':op.OLD_IMAGE,'RootFS':{'Layers':[str(i) for i in range(9)]},'Config':{'User':'10001:10001'},'Os':'linux','Architecture':'amd64','Variant':''}
    after=deepcopy(before);after['Id']=op.IMAGE;after['RootFS']['Layers'].append('delta')
    op.validate_image_lineage(before,after)
    after['Config']['User']='0'
    with pytest.raises(ValueError,match='CONFIG'):op.validate_image_lineage(before,after)
