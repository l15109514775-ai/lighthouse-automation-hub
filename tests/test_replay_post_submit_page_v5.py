"""Actual driver/Chromium with real delivery bytes; all sites are owned routes."""
from pathlib import Path
import pytest

from test_replay_delivery_v5 import delivery,fifo,tracks
import test_replay_part_native_v5 as native_case


@pytest.mark.parametrize('scenario',['async_validation','async_confirmation','async_challenge','accepted_public',
    'manager_challenge_retained','diagnostic_screenshot_failure'])
def test_original_submit_page_survives_readonly_result_observation(delivery,monkeypatch,scenario):
    # Reuse the existing real FFmpeg/delivery/cover manifest builder. Only its
    # fixed owned browser-fixture path changes; production code is not patched.
    actual_run=native_case.subprocess.run
    fixture=Path(__file__).with_name('replay_post_submit_page_v5_fixture.mjs')
    def run(args,*a,**kw):
        if len(args)>1 and Path(args[1]).name=='replay_part_native_v5_fixture.mjs':
            args=[*args];args[1]=str(fixture)
        return actual_run(args,*a,**kw)
    monkeypatch.setattr(native_case.subprocess,'run',run)
    native_case.test_standalone_part_uses_real_delivery_and_one_native_file(delivery,scenario)
