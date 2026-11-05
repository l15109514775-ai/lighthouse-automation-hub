"""Dedicated revision-sync commands on routed owned Chromium, never live data."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('scenario',['sync_success','sync_wrong_old_description','sync_missing_capability','nav_unique','nav_duplicate','nav_draft_tab'])
def test_broker_only_syncs_exact_old_description_under_a_two_revision_capability(tmp_path,scenario):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run(['node',str(root/'tests/browser_metadata_revision_sync_fixture.mjs'),str(tmp_path/'owned'),scenario],
        cwd=root,capture_output=True,text=True,encoding='utf-8',timeout=55,
        env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':str(root/'compositor/runtime/browsers')})
    assert result.returncode==0,result.stderr
    value=json.loads(next(line.removeprefix('FIXTURE_RESULT:') for line in result.stdout.splitlines()
                         if line.startswith('FIXTURE_RESULT:')))
    assert value['scenario']==scenario
    assert value['sync_start_receipt_writes']==(1 if scenario=='sync_success' else 0)
    assert value['controller_file']==('short_video_saved_draft_navigation_control.mjs'
        if scenario.startswith('nav_') or scenario=='sync_missing_capability' else 'short_video_metadata_revision_sync_control.mjs')
    assert value['native_editor_navigations']==(1 if scenario in ('nav_unique','nav_draft_tab') else 0)
    assert value['draft_tab_clicks']==(1 if scenario=='nav_draft_tab' else 0)
    if scenario=='sync_success':
        assert value['description_input_events']>0
        assert value['description_input_events']==value['before_repeat_input_events']
    else:
        assert value['description_input_events']==0
    assert value['file_selections']==value['save_clicks']==value['public_submit']==value['external_network_calls']==0
    assert value['unowned_http_responses']==0
    assert value['real_owner_approval'] is value['live_tested'] is False
