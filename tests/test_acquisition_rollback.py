import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('rollback_acquisition',
    Path(__file__).resolve().parents[1] / 'scripts/rollback_acquisition.py')
rollback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rollback)


def fixture(root):
    return {'Id':'a'*64,'Image':'sha256:'+'b'*64,
        'Config':{'Labels':{'com.docker.compose.service':'replay-acquisition-worker'},
            'Cmd':['python','-m','services.hub.replay_multipart_worker_runtime_v4','--acquisition-only']},
        'Mounts':[{'Type':'bind','Source':str(root),'Destination':'/opt/lighthouse-hub/data'}],
        'State':{'Status':'running'}}


def test_rollback_identifies_only_expected_container_and_data(tmp_path):
    info = fixture(tmp_path)
    plan = rollback.validate(info, info['Image'], str(tmp_path))
    assert plan['database_restore'] is False
    assert plan['historical_workers_started'] is False
    assert plan['container_id'] == info['Id']


@pytest.mark.parametrize('field', ['service','command','image','mount','digest'])
def test_rollback_refuses_changed_target(tmp_path, field):
    info = fixture(tmp_path); expected = info['Image']
    if field == 'service':info['Config']['Labels']['com.docker.compose.service'] = 'video-worker'
    if field == 'command':info['Config']['Cmd'] = ['python','-m','services.hub.cli','worker']
    if field == 'image':info['Image'] = 'sha256:'+'c'*64
    if field == 'mount':info['Mounts'][0]['Source'] = str(tmp_path/'other')
    if field == 'digest':expected = 'mutable-tag:latest'
    with pytest.raises(ValueError):rollback.validate(info, expected, str(tmp_path))
