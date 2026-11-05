import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('install_youtube',Path(__file__).resolve().parents[1]/'scripts/install-youtube-settings.py')
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_installing_youtube_settings_preserves_model_and_disables_no_safeguard():
    current='MODEL_API_KEY=synthetic-model\nYOUTUBE_API_KEY=\nDRY_RUN=true\nHUB_READ_TOKEN=synthetic-reader\n'
    key='synthetic-youtube-key-1234567890'
    result=installer.merge(current,'YOUTUBE_API_KEY='+key)
    assert 'MODEL_API_KEY=synthetic-model\n' in result and 'DRY_RUN=true\n' in result
    assert 'HUB_READ_TOKEN=synthetic-reader\n' in result and result.count(key)==1


@pytest.mark.parametrize('incoming',['YOUTUBE_API_KEY=short','MODEL_API_KEY=unauthorized-field',
    'YOUTUBE_API_KEY=synthetic-youtube-key-1234567890\nDRY_RUN=false',
    'YOUTUBE_API_KEY=contains secret spaces and is invalid'])
def test_youtube_install_rejects_unrelated_or_invalid_settings(incoming):
    with pytest.raises(ValueError):
        installer.merge('YOUTUBE_API_KEY=\n',incoming)
