import json
import pytest
from scripts.audit_compose_runtime import compare, environment


def sample():
    service = {'image': 'sha256:test', 'user': '10001:10001', 'command': ['python', '-m', 'module'],
               'environment': ['TOKEN=synthetic-do-not-render'], 'read_only': True,
               'mem_limit': '512m', 'cpus': '0.5', 'pids_limit': 96,
               'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
               'restart': 'unless-stopped', 'volumes': [
                   {'type': 'bind', 'source': '/source/code.py', 'target': '/runtime/code.py', 'read_only': True}]}
    actual = {'Id': 'container', 'Image': 'sha256:test', 'RestartCount': 0,
              'State': {'Running': True},
              'Config': {'User': '10001:10001', 'Cmd': ['python', '-m', 'module'],
                         'Env': ['TOKEN=synthetic-do-not-render', 'IMAGE_DEFAULT=value']},
              'HostConfig': {'ReadonlyRootfs': True, 'Memory': 512*1024**2, 'NanoCpus': 500000000,
                             'PidsLimit': 96, 'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges=true'],
                             'RestartPolicy': {'Name': 'unless-stopped', 'MaximumRetryCount': 0},
                             'Tmpfs': {'/tmp': 'rw,size=64m'}},
              'Mounts': [{'Type': 'bind', 'Source': '/source/code.py', 'Destination': '/runtime/code.py', 'RW': False}]}
    return service, actual


def test_matches_explicit_config_without_rendering_secrets():
    service, actual = sample()
    report = compare(service, actual, 'sha256:test')
    assert report['matches']
    assert 'synthetic-do-not-render' not in json.dumps(report)
    assert 'TOKEN' not in json.dumps(report)


@pytest.mark.parametrize('field,value,failed', [
    ('Memory', 0, 'mem_limit'), ('NanoCpus', 0, 'cpus'),
    ('ReadonlyRootfs', False, 'read_only'), ('PidsLimit', 0, 'pids_limit')])
def test_resource_and_hardening_drift(field, value, failed):
    service, actual = sample(); actual['HostConfig'][field] = value
    report = compare(service, actual, 'sha256:test')
    assert not report['matches'] and not report['checks'][failed]


def test_stale_environment_and_overlay_are_reported():
    service, actual = sample()
    actual['Config']['Env'] = ['TOKEN=changed']
    actual['Mounts'][0]['Source'] = '/stale/code.py'
    report = compare(service, actual, 'sha256:test')
    assert not report['checks']['explicit_environment'] and not report['checks']['bind_mounts']
    assert 'changed' not in json.dumps(report)


def test_unexpected_mount_and_stopped_container_cannot_pass():
    service, actual = sample()
    actual['Mounts'].append({'Type': 'bind', 'Destination': '/extra', 'Source': '/unexpected', 'RW': True})
    actual['State']['Running'] = False
    report = compare(service, actual, 'sha256:test')
    assert not report['checks']['mount_targets'] and not report['checks']['running']


def test_unresolved_or_duplicate_environment_refused():
    for value in (['TOKEN'], ['TOKEN=1', 'TOKEN=2'], {'TOKEN': None}):
        with pytest.raises(ValueError): environment(value)


def test_null_entrypoint_means_image_default_not_empty_override():
    service, actual = sample(); service['entrypoint'] = None
    actual['Config']['Entrypoint'] = ['/image/entrypoint']
    report = compare(service, actual, 'sha256:test')
    assert report['matches'] and 'entrypoint' not in report['checks']


def test_seccomp_path_and_docker_embedded_profile_match_by_content():
    service, actual = sample()
    profile = {'defaultAction': 'SCMP_ACT_ERRNO', 'syscalls': []}
    service['security_opt'].append('seccomp=/policy.json')
    actual['HostConfig']['SecurityOpt'].append('seccomp=' + json.dumps(profile))
    assert compare(service, actual, 'sha256:test', {'/policy.json': profile})['matches']
    changed = {'defaultAction': 'SCMP_ACT_ALLOW', 'syscalls': []}
    result = compare(service, actual, 'sha256:test', {'/policy.json': changed})
    assert not result['matches'] and not result['checks']['security_opt']
    with pytest.raises(ValueError, match='SECCOMP_PROFILE_NOT_LOADED'):
        compare(service, actual, 'sha256:test')
