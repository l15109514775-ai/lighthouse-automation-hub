"""The new GUI image only: real Xvfb/VNC/websocket with entirely owned pages.

The qualification runner supplies the fixed root-owned three-key test config
and an empty media tmpfs. Never mount real data, cookies or account settings.
"""
import json,os,subprocess
from pathlib import Path
import pytest

@pytest.mark.skipif(os.name!='posix',reason='The explicit GUI-image smoke requires Linux Xvfb/x11vnc/websockify')
def test_real_gui_runtime_keeps_same_owned_page_and_removes_only_nonce_credentials():
 root=Path(__file__).resolve().parents[1]
 env={k:os.environ[k] for k in ('PATH','HOME','LANG','LC_ALL','XDG_CONFIG_HOME','XDG_CACHE_HOME') if k in os.environ}
 env['PLAYWRIGHT_BROWSERS_PATH']=str(root/'compositor/runtime/browsers')
 p=subprocess.run(['node',str(root/'tests/replay_part_handoff_v5_fixture.mjs'),'runtime_gui'],cwd=root,env=env,
  capture_output=True,text=True,encoding='utf-8',timeout=60)
 assert p.returncode==0,p.stderr
 value=json.loads(p.stdout.strip().splitlines()[-1])
 assert value['platform_actions']==0 and all(value[k] is True for k in ('owned','real_xvfb_vnc_websockify','loopback_only','temporary_credentials_removed'))
