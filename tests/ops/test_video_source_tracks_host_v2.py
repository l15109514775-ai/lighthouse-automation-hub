"""No Docker, network, root mutation or deletion: command/policy admission only."""
from pathlib import Path
from types import SimpleNamespace
import importlib.util,json,subprocess
import pytest

spec=importlib.util.spec_from_file_location('video_tracks_host',Path(__file__).resolve().parents[2]/'scripts/ops/video_source_tracks_host_v2.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)

@pytest.mark.parametrize('bad',[{}, {'actual_owner_response':'','owner_response_reference':'owned'},
 {'actual_owner_response':'owned','owner_response_reference':'owned','password':'must not be accepted'}])
def test_owner_request_is_exact_two_field_noncredential_instruction(bad):
 with pytest.raises(AssertionError):h.owner_request(json.dumps(bad).encode())

def test_cli_includes_versioned_track_mode_without_shell_input():
 assert h.cli('--plan-job','owned')==['python',str(h.ROOT/'scripts/video-source-retention-v2.py'),
  '--include-source-tracks','--plan-job','owned']

def config_fixture(tmp_path,monkeypatch):
 monkeypatch.setattr(h,'protected',lambda p:p)
 monkeypatch.setattr(h,'command',lambda *a,**k:h.IMAGE+'\n')
 cfg={'manifest':{'files':{}},'proof_directory':'/var/lib/owned-proof'}
 return cfg

def test_plan_command_has_no_network_profile_secret_environment_or_media_write(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);seen=[]
 def fake(cmd,**kw):seen.append((cmd,kw));return SimpleNamespace(stdout='{"state":"owned"}',stderr='',returncode=0)
 monkeypatch.setattr(h.subprocess,'run',fake);r=h.app_run(cfg,['python','owned'],media_write=False)
 cmd=seen[0][0];joined=' '.join(cmd).replace('\\','/')
 assert '--network none' in joined and '--user 10001:10001' in joined and '--read-only' in cmd
 assert '--env-file' not in cmd and 'API_KEY' not in joined and '/secrets/' not in joined
 assert '/opt/lighthouse-hub/data/browser-profiles:rw,size=1024' in joined
 assert 'src=/opt/lighthouse-hub/media,dst=/opt/lighthouse-hub/media,readonly' in joined
 assert r['network']=='none' and r['credentials_mounted'] is r['profile_mounted'] is False

def test_only_explicit_apply_makes_media_writable(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);seen=[]
 monkeypatch.setattr(h.subprocess,'run',lambda cmd,**kw:(seen.append(cmd) or SimpleNamespace(stdout='{}',stderr='',returncode=0)))
 h.app_run(cfg,['python','owned'],media_write=True)
 assert 'type=bind,src=/opt/lighthouse-hub/media,dst=/opt/lighthouse-hub/media' in [v.replace('\\','/') for v in seen[0]]

def test_timeout_gracefully_stops_only_own_named_container_and_reports_unknown(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);seen=[]
 def fake(cmd,**kw):
  seen.append(cmd)
  if len(seen)==1:raise subprocess.TimeoutExpired(cmd,1)
  return SimpleNamespace(stdout='',stderr='',returncode=0)
 monkeypatch.setattr(h.subprocess,'run',fake);r=h.app_run(cfg,['python','owned'])
 assert seen[1][0:4]==['/usr/bin/docker','stop','--time','30']
 assert seen[1][4]==seen[0][seen[0].index('--name')+1]
 assert r['exit_code']==124 and r['result']['files_deleted_unknown'] is True
