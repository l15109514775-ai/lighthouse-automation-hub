"""Exercise the actual process boundary without network or real credentials."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from adapters.ytdlp_replay import private_environment
from services.hub.errors import HubError


def test_helper_loads_parent_video_config_and_drops_secrets(tmp_path, monkeypatch):
    repo = Path(__file__).resolve().parents[1]
    config = tmp_path / 'config'
    config.mkdir()
    original = (repo / 'config/hub.toml').read_text('utf-8')
    original, count = re.subn(r'^youtube_whole_replay_download_enabled\s*=.*$',
        'youtube_whole_replay_download_enabled = false', original, flags=re.M)
    assert count <= 1
    if count == 0:
        original = 'youtube_whole_replay_download_enabled = false\n' + original
    (config / 'hub.toml').write_text(original, encoding='utf-8')
    selected = config / 'video-runtime.toml'
    content, count = re.subn(r'^youtube_whole_replay_download_enabled\s*=\s*false\s*$',
        'youtube_whole_replay_download_enabled = true', original, flags=re.M)
    assert count == 1
    selected.write_text(content, encoding='utf-8')
    monkeypatch.setenv('HUB_CONFIG', str(selected))
    secret_keys = ('YOUTUBE_API_KEY', 'YOUTUBE_API_KEY_FILE', 'MODEL_API_KEY',
                   'HTTPS_PROXY', 'PYTHONPATH', 'BILIBILI_ACCESS_TOKEN')
    for key in secret_keys:
        monkeypatch.setenv(key, 'synthetic-private-value')
    child_env = private_environment(tmp_path)
    assert child_env['HUB_CONFIG'] == str(selected)
    assert not set(secret_keys) & set(child_env)
    code = """import json,os
from services.hub.config import Settings
s=Settings.load()
print(json.dumps({'enabled':s.config['youtube_whole_replay_download_enabled'],
                  'secret_present':any(v=='synthetic-private-value' for v in os.environ.values())}))
"""
    result = subprocess.run([sys.executable, '-c', code], cwd=repo,
                            env=child_env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'enabled': True, 'secret_present': False}


def test_default_config_behavior_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv('HUB_CONFIG', raising=False)
    assert 'HUB_CONFIG' not in private_environment(tmp_path)


@pytest.mark.parametrize('kind', ['missing', 'relative', 'outside', 'directory', 'oversize'])
def test_invalid_child_config_stops_before_process_start(tmp_path, monkeypatch, kind):
    root = tmp_path / 'app'
    root.mkdir()
    file = root / 'config.toml'
    if kind == 'relative':
        file = Path('config.toml')
    elif kind == 'outside':
        file = tmp_path / 'outside.toml'
        file.write_text('owned=true')
    elif kind == 'directory':
        file.mkdir()
    elif kind == 'oversize':
        file.write_bytes(b' ' * 262145)
    monkeypatch.setenv('HUB_CONFIG', str(file))
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_CONFIG_PATH_INVALID'):
        private_environment(root)


@pytest.mark.skipif(os.name != 'posix', reason='Linux production symlink boundary')
def test_symlink_config_is_rejected(tmp_path, monkeypatch):
    original = tmp_path / 'source.toml'
    original.write_text('owned=true')
    link = tmp_path / 'link.toml'
    link.symlink_to(original)
    monkeypatch.setenv('HUB_CONFIG', str(link))
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_CONFIG_PATH_INVALID'):
        private_environment(tmp_path)
