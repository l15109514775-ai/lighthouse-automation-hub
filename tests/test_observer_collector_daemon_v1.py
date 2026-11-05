"""Owned WAL lifecycle and daemon failures, with no business data or network."""
from pathlib import Path
from types import SimpleNamespace
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import pytest

from services.hub import observer_collector_daemon_v1 as daemon
from services.hub import observer_snapshot_v1 as collector
from test_observer_snapshot_v1 import owned, NOW


class Cycles:
    def __init__(self, maximum=1): self.count,self.maximum=0,maximum
    def is_set(self): return self.count>=self.maximum
    def wait(self, seconds): self.count+=1;return self.is_set()


class RetryClock:
    def __init__(self): self.now=0.0
    def is_set(self): return False
    def wait(self, seconds): self.now+=seconds;return False


def cantopen():
    error=sqlite3.OperationalError('owned-secret-must-never-appear')
    error.sqlite_errorcode=sqlite3.SQLITE_CANTOPEN
    return error


def test_idle_anchor_keeps_auxiliary_files_but_does_not_pin_wal_checkpoint(owned):
    anchor=daemon.IdleReadOnlyAnchor(owned['database']);assert anchor.open(threading.Event())
    try:
        assert anchor.matches() and not anchor.connection.in_transaction
        owned['db'].close()
        assert all(Path(str(owned['database'])+suffix).exists() for suffix in ('-wal','-shm'))
        writer=sqlite3.connect(owned['database'],isolation_level=None)
        try:
            writer.execute('UPDATE jobs SET attempts=7 WHERE job_id=?',(owned['current'],))
            assert writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()==(0,0,0)
            assert Path(str(owned['database'])+'-wal').stat().st_size==0
        finally: writer.close()
        assert all(Path(str(owned['database'])+suffix).exists() for suffix in ('-wal','-shm'))
        assert anchor.connection.execute('SELECT attempts FROM jobs WHERE job_id=?',(owned['current'],)).fetchone()[0]==7
        assert not anchor.connection.in_transaction
        with pytest.raises(sqlite3.OperationalError): anchor.connection.execute('UPDATE jobs SET attempts=8')
    finally: anchor.close()
    writer=sqlite3.connect(owned['database']);writer.execute('SELECT COUNT(*) FROM jobs').fetchone();writer.close()
    assert all(not Path(str(owned['database'])+suffix).exists() for suffix in ('-wal','-shm'))


def test_startup_retries_only_cantopen_with_bounded_clock_then_keeps_readonly_connection(owned,monkeypatch):
    clock=RetryClock();real=sqlite3.connect;calls=[]
    def connect(filename,**kwargs):
        calls.append((filename,kwargs))
        if len(calls)<3: raise cantopen()
        return real(filename,**kwargs)
    monkeypatch.setattr(daemon.sqlite3,'connect',connect)
    anchor=daemon.IdleReadOnlyAnchor(owned['database'],clock=lambda:clock.now)
    try:
        assert anchor.open(clock) and len(calls)==3 and clock.now==pytest.approx(.2)
        assert all(uri.endswith('?mode=ro') and args['uri'] for uri,args in calls)
        assert anchor.connection.execute('PRAGMA query_only').fetchone()==(1,)
    finally: anchor.close()


def test_cantopen_timeout_leaves_no_connection_or_source_change(owned,monkeypatch):
    clock=RetryClock();calls=[];before=owned['db'].execute('SELECT attempts FROM jobs ORDER BY job_id').fetchall()
    def fail(*args,**kwargs): calls.append(True);raise cantopen()
    monkeypatch.setattr(daemon.sqlite3,'connect',fail)
    anchor=daemon.IdleReadOnlyAnchor(owned['database'],timeout=.3,clock=lambda:clock.now)
    with pytest.raises(daemon.ObserverRuntimeError,match='OBSERVER_ANCHOR_UNAVAILABLE'): anchor.open(clock)
    assert 3<=len(calls)<=4 and clock.now==pytest.approx(.3) and anchor.connection is None
    assert owned['db'].execute('SELECT attempts FROM jobs ORDER BY job_id').fetchall()==before


def test_other_database_errors_are_not_retried_as_missing_auxiliary_files(owned,monkeypatch):
    calls=[]
    def fail(*args,**kwargs):
        calls.append(True);error=sqlite3.OperationalError('owned-secret');error.sqlite_errorcode=sqlite3.SQLITE_CORRUPT;raise error
    monkeypatch.setattr(daemon.sqlite3,'connect',fail)
    anchor=daemon.IdleReadOnlyAnchor(owned['database'])
    with pytest.raises(daemon.ObserverRuntimeError,match='OBSERVER_ANCHOR_DATABASE_ERROR'): anchor.open(threading.Event())
    assert len(calls)==1 and anchor.connection is None


def test_inode_change_closes_old_anchor_and_reopens_without_a_transaction(owned,monkeypatch):
    anchor=daemon.IdleReadOnlyAnchor(owned['database']);anchor.open(threading.Event());old=anchor.connection
    identity=daemon.database_identity(owned['database'])
    monkeypatch.setattr(daemon,'database_identity',lambda path:(identity[0],identity[1]+1))
    try:
        assert not anchor.matches() and anchor.ensure(threading.Event())
        assert anchor.matches() and anchor.connection is not old and not anchor.connection.in_transaction
        with pytest.raises(sqlite3.ProgrammingError): old.execute('SELECT 1')
    finally: anchor.close()


def test_collection_failure_preserves_file_timestamp_and_redacts_error(owned):
    output=owned['root']/'snapshot.json';output.write_text('{"generated_at":1}',encoding='utf-8')
    before=output.read_bytes(),output.stat().st_mtime_ns;logs=[]
    def fail(*args,**kwargs): raise ValueError('owned-secret /private/credentials')
    def forbidden(*args,**kwargs): pytest.fail('A failed collection must not write')
    anchor=daemon.IdleReadOnlyAnchor(owned['database'])
    daemon.serve(owned['root'],owned['database'],output,stop=Cycles(),anchor=anchor,
        collector=SimpleNamespace(collect_snapshot=fail,write_snapshot=forbidden),log=logs.append)
    assert (output.read_bytes(),output.stat().st_mtime_ns)==before and anchor.connection is None
    assert logs==[{'state':'snapshot_failed','error_code':'OBSERVER_COLLECTION_FAILED'}]


def test_replacement_during_collection_discards_projection(owned):
    output=owned['root']/'snapshot.json';output.write_text('old',encoding='utf-8');logs=[]
    anchor=daemon.IdleReadOnlyAnchor(owned['database'])
    def collect(*args,**kwargs):
        value=collector.collect_snapshot(*args,**kwargs);anchor.identity=(-1,-1);return value
    daemon.serve(owned['root'],owned['database'],output,stop=Cycles(),anchor=anchor,
        collector=SimpleNamespace(collect_snapshot=collect,write_snapshot=collector.write_snapshot),log=logs.append)
    assert output.read_text()=='old' and logs[0]['state']=='snapshot_failed' and anchor.connection is None


def test_multiple_cycles_update_only_safe_snapshot_with_idle_anchor(owned):
    output=owned['root']/'snapshot.json';logs=[];anchor=daemon.IdleReadOnlyAnchor(owned['database'])
    def collect(*args,**kwargs):
        assert anchor.matches() and not anchor.connection.in_transaction
        return collector.collect_snapshot(*args,**kwargs,now=NOW+len(logs))
    daemon.serve(owned['root'],owned['database'],output,stop=Cycles(2),anchor=anchor,
        collector=SimpleNamespace(collect_snapshot=collect,write_snapshot=collector.write_snapshot),log=logs.append)
    assert [entry['generated_at'] for entry in logs]==[NOW,NOW+1]
    assert all(entry['state']=='snapshot_written' and not entry['anchor_transaction_open'] for entry in logs)
    assert json.loads(output.read_bytes())['generated_at']==NOW+1 and anchor.connection is None
    assert 'secret-value-never-export' not in output.read_text()


def test_stop_during_collection_does_not_publish_and_releases_anchor(owned):
    output=owned['root']/'snapshot.json';stop=threading.Event();anchor=daemon.IdleReadOnlyAnchor(owned['database'])
    def collect(*args,**kwargs): stop.set();return {'generated_at':NOW}
    def forbidden(*args,**kwargs): pytest.fail('Stopping must not publish a new snapshot')
    daemon.serve(owned['root'],owned['database'],output,stop=stop,anchor=anchor,
        collector=SimpleNamespace(collect_snapshot=collect,write_snapshot=forbidden))
    assert not output.exists() and anchor.connection is None


def test_trusted_peer_import_ignores_working_directory(tmp_path,monkeypatch):
    (tmp_path/'observer_snapshot_v1.py').write_text('raise RuntimeError("untrusted cwd imported")',encoding='utf-8')
    monkeypatch.chdir(tmp_path)
    assert daemon.load_collector().VERSION==collector.VERSION


def test_sigterm_handler_stops_cleanly_including_isolated_python_entrypoint(owned,monkeypatch,capsys):
    if os.name!='posix':
        handlers={};monkeypatch.setattr(daemon.signal,'signal',lambda signum,handler:handlers.setdefault(signum,handler))
        monkeypatch.setattr(sys,'argv',['observer','--root',str(owned['root']),'--database',str(owned['database']),'--output',str(owned['root']/'snapshot.json')])
        def serve(*args,**kwargs): handlers[signal.SIGTERM]();assert kwargs['stop'].is_set()
        monkeypatch.setattr(daemon,'serve',serve);daemon.main()
        assert json.loads(capsys.readouterr().out)['state']=='stopped'
        return
    output=owned['root']/'snapshot.json'
    args=[sys.executable,'-I','-B','-u',str(Path(daemon.__file__).resolve()),'--root',str(owned['root']),
        '--database',str(owned['database']),'--output',str(output),'--interval','5']
    proc=subprocess.Popen(args,cwd=owned['root'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    try:
        first=json.loads(proc.stdout.readline());assert first['state']=='snapshot_written'
        proc.send_signal(signal.SIGTERM);out,err=proc.communicate(timeout=5)
        assert proc.returncode==0 and not err and json.loads(out)['state']=='stopped'
    finally:
        if proc.poll() is None: proc.kill();proc.wait(timeout=5)
