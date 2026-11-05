"""Owned Chromium hydration regressions; native identity is never filled or normalized."""
from pathlib import Path
import json,os,subprocess
import pytest

@pytest.mark.parametrize('scenario',['sidebar_then_ready','values_while_blocked','body_timeout','real_challenge','real_dialog','real_auth'])
def test_body_only_loading_never_overrides_guard_or_real_protection(tmp_path,scenario):
    root=Path(__file__).resolve().parents[1]
    env={k:os.environ[k] for k in ('PATH','SystemRoot','SYSTEMROOT','SystemDrive','SYSTEMDRIVE','ProgramData','PROGRAMDATA','ALLUSERSPROFILE','LOCALAPPDATA','APPDATA','USERPROFILE','TEMP','TMP','HOME','XDG_CONFIG_HOME','XDG_CACHE_HOME','LANG','LC_ALL') if k in os.environ}
    env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
    p=subprocess.run(['node',str(root/'tests/draft_passive_readiness_v8_fixture.mjs'),scenario,str(tmp_path)],cwd=root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=50)
    assert p.returncode==0,p.stderr
    value=json.loads(p.stdout.strip().splitlines()[-1])
    assert value['owned'] and value['external_requests']==value['public_submits']==value['choosers']==0
