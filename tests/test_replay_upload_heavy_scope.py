"""A replay upload is network I/O and must not monopolize the heavy lock."""
from types import SimpleNamespace

import pytest

from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.replay_video_browser import ReplayBrowserSession


def owned_session(root):
    session = object.__new__(ReplayBrowserSession)
    session.settings = SimpleNamespace(root=root)
    calls = []
    session._operation = lambda operation, **arguments: calls.append(operation) or {'operation': operation}
    return session, calls


def test_upload_is_admitted_while_heavy_is_held(tmp_path):
    session, calls = owned_session(tmp_path)
    with resource_guard(tmp_path / 'data/locks/heavy'):
        assert session.operation('upload') == {'operation': 'upload'}
        with pytest.raises(HubError, match='HEAVY_RESOURCE_BUSY'):
            session.operation('prepare')
    assert calls == ['upload']


def test_prepare_submit_lookup_still_use_heavy(tmp_path):
    session, calls = owned_session(tmp_path)
    for operation in ('prepare', 'submit', 'lookup'):
        with resource_guard(tmp_path / 'data/locks/heavy'):
            with pytest.raises(HubError, match='HEAVY_RESOURCE_BUSY'):
                session.operation(operation)
        assert session.operation(operation) == {'operation': operation}
    assert calls == ['prepare', 'submit', 'lookup']
