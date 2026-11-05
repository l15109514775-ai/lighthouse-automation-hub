"""Owned temporary-root image operations only; never retire the repository."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from services.hub.config import Settings

REPO=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('owned_visual_retirement',REPO/'scripts/retire_x_visual_image.py')
retirement=importlib.util.module_from_spec(spec);spec.loader.exec_module(retirement)
PIN='owned.fixture.test/lighthouse@sha256:'+'a'*64


@pytest.fixture
def owned_root(tmp_path,monkeypatch):
    root=tmp_path/'owned-image';root.mkdir()
    manifest={folder:{'commit':'owned_fixture_only','files':{}} for folder in ('compositor','vendor/subtitle-cloud-worker')}
    for index in range(281):
        folder='compositor' if index%2 else 'vendor/subtitle-cloud-worker'
        relative=f'owned-{index}.txt';file=root/folder/relative;file.parent.mkdir(parents=True,exist_ok=True)
        data=f'Owned synthetic locked file {index}'.encode();file.write_bytes(data)
        manifest[folder]['files'][relative]=hashlib.sha256(data).hexdigest()
    for relative in [*retirement.PATHS,*retirement.PROTECTED,
        'data/hub.sqlite3','media/owned-source.json','profiles/owned-placeholder',
        'adapters/browser_reply_machine.mjs','services/hub/web_source.py']:
        file=root/relative;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(('Owned '+relative).encode())
    (root/'config').mkdir(exist_ok=True)
    (root/'config/upstream-lock.json').write_text(json.dumps(manifest),'utf-8')
    (root/'config/hub.toml').write_bytes((REPO/'config/hub.toml').read_bytes())
    monkeypatch.setenv('HUB_ROOT',str(root));monkeypatch.setenv('HUB_CONFIG',str(root/'config/hub.toml'))
    return root


def write_marker(root,value):
    file=root/'x-visual-retired.json';file.chmod(0o600)
    file.write_text(json.dumps(value),'utf-8')


def test_image_retirement_unlinks_only_explicit_files_and_preserves_all_others(owned_root):
    root=owned_root
    before={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    report=retirement.retire(root,base_image=PIN)
    assert report['version']=='x-visual-runtime-retired-v1' and report['upstream_files_verified']==281
    assert report['paths']==[{'path':p,'sha256':hashlib.sha256(before[p]).hexdigest()} for p in retirement.PATHS]
    assert retirement.verify_upstream(root)==281
    for relative,data in before.items():
        if relative.replace('\\','/') in retirement.PATHS:assert not (root/relative).exists()
        else:assert (root/relative).read_bytes()==data
    assert (root/'scripts/login-gui').is_dir()
    assert Settings.load().config['x_visual_retired'] is True
    with pytest.raises(RuntimeError,match='MARKER_ALREADY_EXISTS'):retirement.retire(root,base_image=PIN)


def test_local_backup_without_marker_keeps_default_and_real_repository_untouched(owned_root):
    assert Settings.load().config.get('x_visual_retired',False) is False
    assert all((REPO/p).is_file() for p in retirement.PATHS)


@pytest.mark.parametrize('relative',['adapters/x_web.py','scripts/login-gui'])
def test_reported_symlink_target_or_ancestor_is_refused_without_any_unlink(owned_root,monkeypatch,relative):
    # Owned lstat-boundary fault injection also runs on Windows without needing
    # elevated privileges to create an OS symlink. Linux uses the same check.
    original=Path.is_symlink;selected=owned_root/relative
    monkeypatch.setattr(Path,'is_symlink',lambda self:self==selected or original(self))
    with pytest.raises(RuntimeError,match='SYMLINK_REFUSED'):retirement.retire(owned_root,base_image=PIN)
    assert all((owned_root/p).is_file() for p in retirement.PATHS)


@pytest.mark.parametrize('bad',['image:latest','sha256:abc','image@sha256:'+'G'*64,'image@sha256:'+'a'*63])
def test_unpinned_base_is_rejected_before_any_removal(owned_root,bad):
    with pytest.raises(RuntimeError,match='PINNED_BASE_IMAGE_REQUIRED'):retirement.retire(owned_root,base_image=bad)
    assert all((owned_root/p).is_file() for p in retirement.PATHS)


@pytest.mark.parametrize('failure',['missing_target','target_directory','changed_upstream','bad_upstream_count','missing_publisher'])
def test_all_prerequisites_are_checked_before_first_unlink(owned_root,failure):
    root=owned_root
    if failure in ('missing_target','target_directory'):
        target=root/retirement.PATHS[-1];target.unlink()
        if failure=='target_directory':target.mkdir()
    elif failure=='changed_upstream':(root/'compositor/owned-1.txt').write_text('corrupted')
    elif failure=='bad_upstream_count':
        file=root/'config/upstream-lock.json';data=json.loads(file.read_bytes())
        data['compositor']['files'].pop('owned-1.txt');file.write_text(json.dumps(data))
    else:(root/retirement.PROTECTED[0]).unlink()
    with pytest.raises((RuntimeError,OSError)):retirement.retire(root,base_image=PIN)
    assert all((root/p).is_file() for p in retirement.PATHS[:-1])
    assert not (root/'x-visual-retired.json').exists()


@pytest.mark.parametrize('change',['version','path','hash','count','base','duplicate_path','protected_hash','extra','restored_target','bad_json'])
def test_bad_marker_or_restored_entry_fails_closed(owned_root,change):
    root=owned_root;report=retirement.retire(root,base_image=PIN);value=deepcopy(report)
    if change=='version':value['version']='unversioned'
    elif change=='path':value['paths'][0]['path']='../outside'
    elif change=='hash':value['paths'][0]['sha256']='unknown'
    elif change=='count':value['upstream_files_verified']=280
    elif change=='base':value['base_image']='image:latest'
    elif change=='duplicate_path':value['paths'][1]=value['paths'][0]
    elif change=='protected_hash':value['protected'][0]['sha256']='b'*64
    elif change=='extra':value['shell']='never allowed'
    elif change=='restored_target':(root/retirement.PATHS[0]).write_bytes(b'owned restored entry')
    write_marker(root,value)
    if change=='bad_json':(root/'x-visual-retired.json').write_bytes(b'{not-json')
    with pytest.raises(ValueError,match='retirement image marker'):Settings.load()


def test_old_configuration_cannot_reactivate_visual_transport(owned_root):
    root=owned_root;retirement.retire(root,base_image=PIN)
    file=root/'config/hub.toml';text=file.read_text('utf-8')
    text=text.replace('x_web_enabled = false','x_web_enabled = true')
    file.write_text(text,'utf-8')
    with pytest.raises(ValueError,match='visual source transport has been retired'):Settings.load()


def test_invalid_flag_type_remains_rejected_even_when_image_marker_forces_retirement(owned_root):
    root=owned_root;retirement.retire(root,base_image=PIN)
    file=root/'config/hub.toml';file.write_text('x_visual_retired = "false"\n'+file.read_text('utf-8'),'utf-8')
    with pytest.raises(ValueError,match='visual source transport has been retired'):Settings.load()


def test_dockerfile_has_no_mutable_default_or_root_runtime():
    text=(REPO/'Dockerfile.api-only').read_text('utf-8')
    assert 'ARG HUB_BASE_IMAGE\nFROM ${HUB_BASE_IMAGE}' in text
    assert 'USER 0:0' in text and text.rstrip().endswith('USER 10001:10001')
    assert 'retire_x_visual_image.py --base-image "$HUB_BASE_IMAGE"' in text
    assert 'COPY services/hub/config.py' in text and 'env_file' not in text
