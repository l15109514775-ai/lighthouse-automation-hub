"""Load the actual Linux poller without starting its loop or reading credentials."""
import importlib.util
import os
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != 'posix', reason='Linux service imports fcntl')
@pytest.mark.parametrize('interval,expected', [(None, 1800), ('900', 900)])
def test_poller_default_and_operator_override(monkeypatch, interval, expected):
    if interval is None:
        monkeypatch.delenv('SUBTITLE_POLL_SECONDS', raising=False)
    else:
        monkeypatch.setenv('SUBTITLE_POLL_SECONDS', interval)
    root = Path(__file__).resolve().parents[1]
    source = root / 'subtitle-platform/runner/poll_shanghai.py'
    spec = importlib.util.spec_from_file_location('owned_poller_deployment_test', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.INTERVAL == expected
    assert module.FETCH == '/root/subtitle/fetch.py'
    unit = (source.parent / 'systemd/subtitle-poller.service').read_text()
    assert 'Environment=SUBTITLE_POLL_SECONDS=1800' in unit
    assert 'ExecStart=/usr/bin/python3 /root/subtitle/poll.py' in unit
