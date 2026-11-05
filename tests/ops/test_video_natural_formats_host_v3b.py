"""No Docker, network, root mutation or deletion: command/policy admission only."""
from pathlib import Path
from types import SimpleNamespace
import argparse,importlib.util,json,subprocess,tarfile,sqlite3,sys
import pytest

spec=importlib.util.spec_from_file_location('video_tracks_host',Path(__file__).resolve().parents[2]/'scripts/ops/video_natural_formats_host_v3b.py')
h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h)

@pytest.mark.parametrize('bad',[{}, {'actual_owner_response':'','owner_response_reference':'owned'},
 {'actual_owner_response':'owned','owner_response_reference':'owned','password':'must not be accepted'}])
def test_owner_request_is_exact_two_field_noncredential_instruction(bad):
 with pytest.raises(AssertionError):h.owner_request(json.dumps(bad).encode())

def test_cli_includes_versioned_track_mode_without_shell_input():
 assert h.cli('--plan-job','owned')==['python',str(h.ROOT/'scripts/video-source-retention-v2.py'),
  '--include-source-tracks','--natural-format-ids-v3','--plan-job','owned']

def config_fixture(tmp_path,monkeypatch):
 monkeypatch.setattr(h,'protected',lambda p:p)
 monkeypatch.setattr(h,'command',lambda *a,**k:h.IMAGE+'\n')
 archive=Path(__file__).resolve().parents[4]/'outputs/video-natural-formats-v3-overlay.tar.gz'
 with tarfile.open(archive) as tar:
  manifest=json.load(tar.extractfile('manifest.json'))
  for name in h.PRODUCTION_FILES:
   file=tmp_path/'runtime'/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(tar.extractfile(name).read())
 monkeypatch.setattr(h,'BASE',tmp_path)
 monkeypatch.setattr(h,'configuration_sources',lambda:(h.ROOT/'config/hub.toml',h.RELEASE))
 cfg={'manifest':manifest,'proof_directory':'/var/lib/owned-proof'}
 return cfg

def test_plan_command_has_no_network_profile_secret_environment_or_media_write(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);seen=[]
 def fake(cmd,**kw):seen.append((cmd,kw));return SimpleNamespace(stdout='{"state":"owned"}',stderr='',returncode=0)
 monkeypatch.setattr(h.subprocess,'run',fake);r=h._app_run(cfg,['python','-c','print("{}")'],media_write=False)
 cmd=seen[0][0];joined=' '.join(cmd).replace('\\','/')
 assert '--network none' in joined and '--user 10001:10001' in joined and '--read-only' in cmd
 assert '--env-file' not in cmd and 'API_KEY' not in joined and '/secrets/' not in joined
 assert '/opt/lighthouse-hub/data/browser-profiles:rw,size=1024' in joined
 assert 'src=/opt/lighthouse-hub/media,dst=/opt/lighthouse-hub/media,readonly' in joined
 assert r['network']=='none' and r['credentials_mounted'] is r['profile_mounted'] is False

def test_only_explicit_apply_makes_media_writable(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);seen=[]
 monkeypatch.setattr(h.subprocess,'run',lambda cmd,**kw:(seen.append(cmd) or SimpleNamespace(stdout='{}',stderr='',returncode=0)))
 h._app_run(cfg,['python','-c','print("{}")'],media_write=True)
 assert 'type=bind,src=/opt/lighthouse-hub/media,dst=/opt/lighthouse-hub/media' in [v.replace('\\','/') for v in seen[0]]

def test_timeout_gracefully_stops_only_own_named_container_and_reports_unknown(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);seen=[]
 def fake(cmd,**kw):
  seen.append(cmd)
  if len(seen)==1:raise subprocess.TimeoutExpired(cmd,1)
  return SimpleNamespace(stdout='',stderr='',returncode=0)
 monkeypatch.setattr(h.subprocess,'run',fake);r=h._app_run(cfg,['python','-c','print("{}")'])
 assert seen[1][0:4]==['/usr/bin/docker','stop','--time','30']
 assert seen[1][4]==seen[0][seen[0].index('--name')+1]
 assert r['exit_code']==124 and r['result']['files_deleted_unknown'] is True


def test_actual_95_file_proof_mounts_only_explicit_code_and_real_configuration_once(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);seen=[]
 monkeypatch.setattr(h.subprocess,'run',lambda cmd,**kw:(seen.append(cmd) or SimpleNamespace(stdout='{}',stderr='',returncode=0)))
 r=h._app_run(cfg,['python','-c','print("{}")']);cmd=seen[0]
 mounts=[cmd[i+1].replace('\\','/') for i,arg in enumerate(cmd) if arg=='--mount']
 targets=[value.split(',dst=')[1].split(',')[0] for value in mounts]
 assert len(targets)==len(set(targets))
 assert targets.count('/opt/lighthouse-hub/config/hub.toml')==1
 real=[value for value in mounts if ',dst=/opt/lighthouse-hub/config/hub.toml,' in value]
 assert real==['type=bind,src=/opt/lighthouse-hub/config/hub.toml,dst=/opt/lighthouse-hub/config/hub.toml,readonly']
 assert not any('/tests/' in v or '/fixtures/' in v or 'example.toml' in v or 'pyproject.toml' in v for v in mounts)
 assert r['production_code_files_mounted']==33 and r['test_or_example_configuration_mounted'] is False


def test_duplicate_configuration_target_is_blocked_before_docker(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);seen=[]
 monkeypatch.setattr(h,'configuration_sources',lambda:(h.ROOT/'config/hub.toml',h.ROOT/'config/hub.toml'))
 monkeypatch.setattr(h.subprocess,'run',lambda *a,**k:seen.append(a))
 with pytest.raises(AssertionError,match='DUPLICATE_PRODUCTION_MOUNT_TARGET'):h._app_run(cfg,['python','-c','print("{}")'])
 assert seen==[]


def test_missing_production_dependency_is_rejected_without_test_config_fallback(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch)
 del cfg['manifest']['files']['services/hub/db.py']
 with pytest.raises(AssertionError):h.production_sources(cfg['manifest'])


def test_default_main_db_and_media_are_readonly_and_only_control_locks_have_write_access(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);seen=[]
 monkeypatch.setattr(h.subprocess,'run',lambda cmd,**kw:(seen.append(cmd) or SimpleNamespace(stdout='{}',stderr='',returncode=0)))
 r=h._app_run(cfg,['python','-c','print("{}")']);cmd=seen[0]
 mounts=[cmd[i+1].replace('\\','/') for i,arg in enumerate(cmd) if arg=='--mount']
 assert 'type=bind,src=/opt/lighthouse-hub/data,dst=/opt/lighthouse-hub/data,readonly' in mounts
 assert 'type=bind,src=/opt/lighthouse-hub/media,dst=/opt/lighthouse-hub/media,readonly' in mounts
 assert 'type=bind,src=/opt/lighthouse-hub/data/locks,dst=/opt/lighthouse-hub/data/locks' in mounts
 assert r['main_database_writable'] is r['media_writable'] is False
 assert r['natural_policy_or_grant_mounted'] is False
 assert not any('workflow-launch' in v or 'pilot.json' in v or 'policy.toml' in v for v in mounts)


def snapshot_owned(tmp_path,monkeypatch,capsys):
 repo=Path(__file__).resolve().parents[2];monkeypatch.syspath_prepend(str(repo))
 source=tmp_path/'live.sqlite3';target=tmp_path/'consistent.sqlite3'
 writer=sqlite3.connect(source);writer.execute('PRAGMA journal_mode=WAL');writer.execute('CREATE TABLE owned(value TEXT)')
 writer.execute("INSERT INTO owned VALUES('committed current observation')");writer.commit();writer.close()
 assert not Path(str(source)+'-wal').exists() and not Path(str(source)+'-shm').exists()
 import os
 monkeypatch.setattr(os,'getuid',lambda:10001,raising=False);monkeypatch.setattr(os,'getgid',lambda:10001,raising=False)
 if os.name!='posix':
  # Only the production POSIX permission/fd-fsync boundary is stubbed. SQLite
  # performs the actual backup, commit, integrity/FK check and byte hash here.
  monkeypatch.setattr(os,'fchmod',lambda fd,mode:None,raising=False)
  monkeypatch.setattr(os,'fsync',lambda fd:None)
 monkeypatch.setattr(sys,'argv',['snapshot',str(source),str(target)])
 with pytest.raises(SystemExit) as exited:exec(h.ONLINE_SNAPSHOT,{})
 assert exited.value.code==0
 record=json.loads(capsys.readouterr().out);assert record['state']=='consistent_snapshot_created'
 ns={};exec(h.READ_ONLY_STORE.replace('/run/retention-snapshot/hub.sqlite3',target.as_posix()),ns)
 return source,target,record,ns['ReadOnlyStore']


def test_online_copy_of_quiescent_live_wal_is_readonly_consistent_and_write_protected(tmp_path,monkeypatch,capsys):
 source,target,record,reader_class=snapshot_owned(tmp_path,monkeypatch,capsys)
 reader=reader_class(Path('/opt/lighthouse-hub/data/hub.sqlite3'))
 assert reader.query('SELECT value FROM owned')==[{'value':'committed current observation'}]
 with reader.transaction() as db:
  with pytest.raises(sqlite3.OperationalError):db.execute("INSERT INTO owned VALUES('forbidden')")
 assert record['online_sqlite_backup'] is record['foreign_key_check'] is True and record['journal_mode']=='delete'
 assert not Path(str(target)+'-wal').exists() and not Path(str(target)+'-shm').exists()
 with pytest.raises(AssertionError):reader_class(source)


def test_reused_authorization_must_match_the_original_recorded_instruction_and_actual_38_proof():
 owned={'archive_sha256':'51b6f980f25bfe18da248625939873dc33078125ecbe9c2cd49b7624aa9f15ff',
  'proof_sha256':'deb675fc049a5639d445d85d5039e571eb361ea2b54e11b711b3a992057e6a71',
  'owner_request_sha256':'a'*64,'authorization_id':'5b428096-e517-45e5-b3d5-981ac525fa7d'}
 assert h.validate_prior_runtime(owned,'a'*64)==owned['authorization_id']
 with pytest.raises(AssertionError):h.validate_prior_runtime(owned,'b'*64)
 with pytest.raises(AssertionError):h.validate_prior_runtime({**owned,'proof_sha256':'0'*64},'a'*64)


def test_apply_input_plan_is_exact_readonly_mount_and_main_writes_require_explicit_apply(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);seen=[];file=tmp_path/'owned-plan.json';file.write_text('{}')
 monkeypatch.setattr(h.subprocess,'run',lambda cmd,**kw:(seen.append(cmd) or SimpleNamespace(stdout='{}',stderr='',returncode=0)))
 with pytest.raises(AssertionError):h._app_run(cfg,['python','-c','print("{}")'],input_plan=file)
 assert seen==[]
 r=h._app_run(cfg,['python','-c','print("{}")'],media_write=True,input_plan=file);cmd=seen[0]
 mounts=[cmd[i+1].replace('\\','/') for i,arg in enumerate(cmd) if arg=='--mount']
 assert any(',dst=/run/retention-input-plan.json,readonly' in v for v in mounts)
 assert 'type=bind,src=/opt/lighthouse-hub/data,dst=/opt/lighthouse-hub/data' in mounts
 assert r['main_database_writable'] is r['media_writable'] is True


def test_readonly_media_plan_redirects_only_diagnostics_not_commands_or_validation(tmp_path,monkeypatch):
 repo=Path(__file__).resolve().parents[2];monkeypatch.syspath_prepend(str(repo))
 import adapters.media_tools as media
 import adapters.ytdlp_replay as replay
 seen=[];check=lambda:None
 def original(command,folder,timeout,callback):seen.append((command,folder,timeout,callback));return iter((b'owned packet bytes',))
 old_media,old_replay=media.process_lines,replay.process_lines
 try:
  media.process_lines=original
  ns={};exec(h.READ_ONLY_MEDIA_SCRATCH,ns)
  command=['ffprobe','-protocol_whitelist','file,pipe','/owned/source/original.mp4']
  assert list(media.process_lines(command,Path('/owned/source'),7200,check))==[b'owned packet bytes']
  assert list(replay.process_lines(command,Path('/owned/source'),30,check))==[b'owned packet bytes']
  assert seen==[(command,Path('/tmp'),7200,check),(command,Path('/tmp'),30,check)]
 finally:media.process_lines=old_media;replay.process_lines=old_replay


def test_proof_digest_accepts_exact_raw_64_and_rejects_terminal_duplicate_65_before_io():
 raw='5824da326998a5857bdde4cc38ef605b20f870cfc4a62940f2ef7c715a80400b'
 terminal_duplicate='5824da326998a5857bdde4cc38ef605bb20f870cfc4a62940f2ef7c715a80400b'
 assert len(raw)==64 and h.sha256_argument(raw)==raw
 assert len(terminal_duplicate)==65
 with pytest.raises(argparse.ArgumentTypeError,match='exactly 64'):h.sha256_argument(terminal_duplicate)


def test_snapshot_stays_consistent_after_live_changes_without_wal_mix(tmp_path,monkeypatch,capsys):
 source,target,record,reader_class=snapshot_owned(tmp_path,monkeypatch,capsys)
 writer=sqlite3.connect(source);writer.execute("INSERT INTO owned VALUES('new live data only')");writer.commit();writer.close()
 reader=reader_class(Path('/opt/lighthouse-hub/data/hub.sqlite3'))
 assert len(reader.query('SELECT value FROM owned'))==1
 with sqlite3.connect(source) as live:assert live.execute('SELECT COUNT(*) FROM owned').fetchone()[0]==2
 assert not Path(str(target)+'-wal').exists() and not Path(str(target)+'-shm').exists()


def test_explicit_apply_uses_live_store_path_and_never_creates_snapshot(tmp_path,monkeypatch):
 monkeypatch.setattr(h,'create_snapshot',lambda *a:pytest.fail('apply must use live DB/CAS'))
 seen=[];monkeypatch.setattr(h,'_app_run',lambda config,args,**kwargs:(seen.append(kwargs) or {'state':'owned live apply boundary'}))
 assert h.app_run({},['python','-c','print("{}")'],media_write=True)['state']=='owned live apply boundary'
 assert seen[0]['media_write'] is True and 'snapshot' not in seen[0]
 assert 'ReadOnlyStore' not in h.APPLY_SAVED_PLAN and 'Store(s.db_path)' in h.APPLY_SAVED_PLAN


def test_readonly_snapshot_mount_uses_independent_run_namespace_and_exact_hash(tmp_path,monkeypatch):
 cfg=config_fixture(tmp_path,monkeypatch);file=tmp_path/'copy.sqlite3';file.write_bytes(b'owned copy fixture bytes')
 record={'sha256':h.sha(file.read_bytes()),'bytes':file.stat().st_size,'online_sqlite_backup':True};seen=[];removed=[]
 monkeypatch.setattr(h,'create_snapshot',lambda directory:(file,record))
 monkeypatch.setattr(h,'remove_snapshot',lambda directory:removed.append(directory))
 monkeypatch.setattr(h.subprocess,'run',lambda cmd,**kwargs:(seen.append(cmd) or SimpleNamespace(stdout='{}',stderr='',returncode=0)))
 value=h.app_run(cfg,['python','-c','print("{}")'])
 mounts=[seen[0][n+1].replace('\\','/') for n,arg in enumerate(seen[0]) if arg=='--mount']
 assert sum(',dst=/run/retention-snapshot/hub.sqlite3,readonly' in v for v in mounts)==1
 assert not any(',dst=/opt/lighthouse-hub/data/hub.sqlite3' in v for v in mounts)
 assert value['database_snapshot']['live_database_path_overlay'] is False and removed


def test_safe_error_wrapper_exposes_class_and_sqlite_code_without_message_or_sql(monkeypatch,capsys):
 secret='Bearer do_not_log_this_value; SELECT secret FROM credentials'
 payload="import sqlite3;e=sqlite3.OperationalError("+repr(secret)+");e.sqlite_errorcode=14;e.sqlite_errorname='SQLITE_CANTOPEN';raise e"
 monkeypatch.setattr(sys,'argv',['-c',payload])
 with pytest.raises(SystemExit) as exited:exec(h.SAFE_APP,{})
 raw=capsys.readouterr().out;value=json.loads(raw)
 assert exited.value.code==2 and value['sqlite_errorcode']==14 and value['sqlite_errorname']=='SQLITE_CANTOPEN'
 assert value['exception_class']=='OperationalError' and secret not in raw and 'SELECT' not in raw
 assert value['raw_message_included'] is False


def test_snapshot_cleanup_rejects_outside_namespace_without_touching_files(tmp_path):
 directory=tmp_path/'not_run';directory.mkdir();file=directory/'hub.sqlite3';file.write_bytes(b'owned preserve')
 with pytest.raises(AssertionError):h.remove_snapshot(directory)
 assert file.read_bytes()==b'owned preserve'
