"""A save redirect during the UID check, using owned Chromium and real bytes."""
import subprocess
from pathlib import Path

import pytest

from test_replay_part_native_v5 import test_standalone_part_uses_real_delivery_and_one_native_file as run_owned
from test_replay_delivery_v5 import delivery, fifo, tracks


@pytest.mark.timeout(120)
@pytest.mark.parametrize('scenario', ['redirect', 'delayed_complete', 'wrong_source', 'never_complete', 'challenge',
                                    'manager_entry', 'already_draft_delayed', 'already_draft_never', 'foreign_entry'])
def test_saved_draft_navigation_during_identity_check(delivery, monkeypatch, scenario):
    original = subprocess.run
    fixture = Path(__file__).with_name('replay_saved_draft_redirect_v10_fixture.mjs')

    def owned_run(arguments, **kwargs):
        if isinstance(arguments, list) and len(arguments) > 1 and Path(arguments[1]).name == 'replay_part_native_v5_fixture.mjs':
            arguments = [arguments[0], str(fixture), *arguments[2:], scenario]
        return original(arguments, **kwargs)

    monkeypatch.setattr(subprocess, 'run', owned_run)
    run_owned(delivery, 'prepare_public')
