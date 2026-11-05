"""Maintain an idle read-only WAL connection and publish sanitized snapshots.

The idle connection preserves SQLite auxiliary files while each ordinary
collector transaction remains short. No transaction stays open between cycles.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import signal
import sqlite3
import stat
import threading
import time


VERSION = 'lighthouse-observer-collector-daemon-v1'


class ObserverRuntimeError(Exception):
    pass


def checked_path(path):
    path = Path(path).absolute()
    if path.resolve() != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ObserverRuntimeError('OBSERVER_UNSAFE_PATH')
    return path


def database_identity(path):
    info = checked_path(path).stat()
    if not stat.S_ISREG(info.st_mode):
        raise ObserverRuntimeError('OBSERVER_DATABASE_NOT_REGULAR')
    return info.st_dev, info.st_ino


def load_collector():
    # -I intentionally ignores the working directory and inherited PYTHONPATH.
    path = checked_path(Path(__file__).absolute().parent / 'observer_snapshot_v1.py')
    spec = importlib.util.spec_from_file_location('lighthouse_trusted_snapshot_v1', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class IdleReadOnlyAnchor:
    def __init__(self, database, *, timeout=8.0, delay=0.1, clock=time.monotonic):
        if not 0 < timeout <= 8 or not 0 < delay <= 1:
            raise ValueError('OBSERVER_INVALID_RETRY_BOUND')
        self.path = checked_path(database)
        self.timeout, self.delay, self.clock = timeout, delay, clock
        self.connection, self.identity = None, None

    def close(self):
        if self.connection is not None:
            self.connection.close()
        self.connection, self.identity = None, None

    def matches(self):
        try:
            return (self.connection is not None and not self.connection.in_transaction and
                    self.identity == database_identity(self.path))
        except (OSError, sqlite3.Error, ObserverRuntimeError):
            return False

    def open(self, stop):
        self.close()
        deadline = self.clock() + self.timeout
        while not stop.is_set():
            connection = None
            try:
                before = database_identity(self.path)
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise ObserverRuntimeError('OBSERVER_ANCHOR_UNAVAILABLE')
                connection = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                    isolation_level=None, timeout=min(1.0, remaining))
                connection.execute('PRAGMA query_only=ON')
                connection.execute('BEGIN')
                connection.execute('SELECT 1 FROM sqlite_master LIMIT 1').fetchall()
                connection.execute('ROLLBACK')
                if before != database_identity(self.path):
                    raise ObserverRuntimeError('OBSERVER_DATABASE_REPLACED')
                if connection.in_transaction:
                    raise ObserverRuntimeError('OBSERVER_ANCHOR_TRANSACTION_OPEN')
                self.connection, self.identity = connection, before
                connection = None
                return True
            except sqlite3.Error as error:
                # Missing/recreated WAL sidecars cause CANTOPEN on a RO mount.
                # Other failures are not concealed as that lifecycle race.
                if getattr(error, 'sqlite_errorcode', -1) & 255 != sqlite3.SQLITE_CANTOPEN:
                    raise ObserverRuntimeError('OBSERVER_ANCHOR_DATABASE_ERROR') from None
            except ObserverRuntimeError as error:
                if str(error) != 'OBSERVER_DATABASE_REPLACED':
                    raise
            finally:
                if connection is not None:
                    connection.close()
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ObserverRuntimeError('OBSERVER_ANCHOR_UNAVAILABLE')
            stop.wait(min(self.delay, remaining))
        return False

    def ensure(self, stop):
        return True if self.matches() else self.open(stop)


def emit(value):
    print(json.dumps({'version': VERSION, **value}, separators=(',', ':')), flush=True)


def serve(root, database, output, *, backup=None, active_tweet_policy='machine-tweet-v4-api-relations',
          interval=30.0, stop=None, collector=None, anchor=None, log=emit, clock=time.monotonic):
    if not 5 <= interval <= 300:
        raise ObserverRuntimeError('OBSERVER_INVALID_INTERVAL')
    stop = stop if stop is not None else threading.Event()
    collector = collector if collector is not None else load_collector()
    anchor = anchor if anchor is not None else IdleReadOnlyAnchor(database)
    try:
        if not anchor.open(stop): return
        while not stop.is_set():
            started = clock()
            if not anchor.ensure(stop): break
            try:
                snapshot = collector.collect_snapshot(root, database, backup=backup,
                    active_tweet_policy=active_tweet_policy)
                # A replacement during collection must never become a fresh report.
                if not anchor.matches():
                    anchor.close()
                    raise ObserverRuntimeError('OBSERVER_DATABASE_REPLACED')
                if stop.is_set(): break
                collector.write_snapshot(output, snapshot)
                log({'state': 'snapshot_written', 'generated_at': snapshot['generated_at'],
                     'provider_requests': 0, 'anchor_transaction_open': False})
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, ObserverRuntimeError):
                # Preserve the previous file and timestamp; never print the exception.
                log({'state': 'snapshot_failed', 'error_code': 'OBSERVER_COLLECTION_FAILED'})
            stop.wait(max(0.1, interval - (clock() - started)))
    finally:
        anchor.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'database', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    parser.add_argument('--backup-status', type=Path)
    parser.add_argument('--active-tweet-policy', default='machine-tweet-v4-api-relations')
    parser.add_argument('--interval', type=float, default=30)
    args = parser.parse_args()
    stop = threading.Event()
    for name in ('SIGINT', 'SIGTERM'):
        signal.signal(getattr(signal, name), lambda *unused: stop.set())
    try:
        serve(args.root, args.database, args.output, backup=args.backup_status,
              active_tweet_policy=args.active_tweet_policy, interval=args.interval, stop=stop)
        emit({'state': 'stopped', 'provider_requests': 0})
    except Exception:
        # Startup/reconnect failure exits for the fixed systemd supervisor.
        emit({'state': 'stopped', 'error_code': 'OBSERVER_DAEMON_UNAVAILABLE'})
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
