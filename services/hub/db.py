from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import time
import uuid
from .job_scope import job_type_filter
from .errors import LeaseLost, stable_error_code


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('PRAGMA synchronous=FULL')
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def init(self):
        db = self.connect()
        try:
            db.execute('PRAGMA journal_mode=WAL')
            folder = Path(__file__).resolve().parents[2] / 'scheduler/migrations'
            for script in sorted(folder.glob('*.sql')):
                db.executescript(script.read_text('utf-8'))
        finally:
            db.close()

    def query(self, sql, args=()):
        db = self.connect()
        try:
            return [dict(r) for r in db.execute(sql, args).fetchall()]
        finally:
            db.close()

    def enqueue(self, platform, source_id, kind, queue, payload, policy):
        created = time.time()
        job_id = str(uuid.uuid4())
        with self.transaction() as db:
            db.execute('''INSERT OR IGNORE INTO jobs(job_id,source_platform,source_id,job_type,queue,state,
                created_at,updated_at,policy_version,payload) VALUES(?,?,?,?,?,'pending',?,?,?,?)''',
                (job_id, platform, str(source_id), kind, queue, created, created, policy, encode(payload)))
            row = db.execute('SELECT job_id FROM jobs WHERE source_platform=? AND source_id=? AND job_type=? AND policy_version=?',
                             (platform, str(source_id), kind, policy)).fetchone()
        return row['job_id']

    def get(self, job_id):
        rows = self.query('SELECT * FROM jobs WHERE job_id=?', (job_id,))
        if not rows:
            return None
        row = rows[0]
        for field in ('payload', 'result'):
            row[field] = json.loads(row[field])
        return row

    def claim(self, queue, lease_seconds, max_attempts, *, job_types=None):
        scope, types = job_type_filter(job_types)
        now = time.time()
        owner = str(uuid.uuid4())
        with self.transaction() as db:
            if db.execute('SELECT paused FROM pipelines WHERE name=?', (queue,)).fetchone()['paused']:
                return None
            row = db.execute(f'''SELECT job_id FROM jobs WHERE queue=? AND {scope} AND state IN ('pending','retry_scheduled')
                AND next_run<=? AND attempts<? ORDER BY created_at,job_id LIMIT 1''', (queue, *types, now, max_attempts)).fetchone()
            if not row:
                return None
            db.execute('''UPDATE jobs SET state='processing',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=?
                WHERE job_id=?''', (owner, now + lease_seconds, now, row['job_id']))
        return self.get(row['job_id'])

    def heartbeat(self, job, lease_seconds):
        now = time.time()
        with self.transaction() as db:
            return db.execute('''UPDATE jobs SET lease_until=?,updated_at=? WHERE job_id=?
                 AND state='processing' AND lease_owner=? AND lease_until>? AND attempts=?''',
                (now + lease_seconds, now, job['job_id'], job['lease_owner'], now, job['attempts'])).rowcount == 1

    def finish(self, job, state, *, code=None, result=None, stage=None, delay=0, external_id=None):
        code = stable_error_code(code)
        with self.transaction() as db:
            changed = db.execute('''UPDATE jobs SET state=?,error_code=?,result=?,stage=?,next_run=?,
                external_publish_id=COALESCE(?,external_publish_id),updated_at=?,lease_owner=NULL,lease_until=NULL
                WHERE job_id=? AND state='processing' AND lease_owner=? AND lease_until>? AND attempts=?''',
                (state, code, encode(result if result is not None else job['result']), stage or job['stage'],
                 time.time() + delay, external_id, time.time(), job['job_id'], job['lease_owner'], time.time(), job['attempts'])).rowcount
            if changed != 1:
                raise LeaseLost()
            if state in ('failed', 'awaiting_review', 'publish_unknown', 'auth_expired'):
                event = {'failed': 'task.failed', 'auth_expired': 'auth.expired'}.get(state, 'review.required')
                self._event(db, event, {'job_id': job['job_id'], 'code': code, 'queue': job['queue']})

    def checkpoint(self, job, stage, result):
        with self.transaction() as db:
            if db.execute('UPDATE jobs SET stage=?,result=?,updated_at=? WHERE job_id=? AND state=? AND lease_owner=? AND lease_until>? AND attempts=?',
                          (stage, encode(result), time.time(), job['job_id'], 'processing', job['lease_owner'], time.time(), job['attempts'])).rowcount != 1:
                raise LeaseLost()
        job['stage'], job['result'] = stage, result

    def recover(self, queue, max_attempts, *, all_processing=False, job_types=None):
        # all_processing may ONLY be used after acquiring the corresponding OS queue lock.
        scope, types = job_type_filter(job_types)
        if all_processing:
            from .locks import require_queue_lock
            require_queue_lock(self, queue)
        now = time.time()
        with self.transaction() as db:
            rows = db.execute(f'SELECT * FROM jobs WHERE queue=? AND {scope} AND state=? AND (? OR lease_until<?)',
                              (queue, *types, 'processing', all_processing, now)).fetchall()
            for row in rows:
                publication = db.execute('SELECT * FROM publications WHERE job_id=?', (row['job_id'],)).fetchone()
                if publication and publication['state']!='rejected':
                    state, code = 'publish_unknown', 'RECONCILE_BEFORE_RETRY'
                elif row['attempts'] >= max_attempts:
                    state, code = 'failed', 'RETRY_EXHAUSTED_AFTER_RESTART'
                else:
                    state, code = 'retry_scheduled', 'PROCESS_INTERRUPTED'
                db.execute('''UPDATE jobs SET state=?,error_code=?,lease_owner=NULL,lease_until=NULL,
                    next_run=?,updated_at=? WHERE job_id=?''', (state, code, now, now, row['job_id']))
                if state == 'failed':
                    self._event(db, 'task.failed', {'job_id': row['job_id'], 'code': code, 'queue': queue})
        return len(rows)

    def _event(self, db, name, data):
        db.execute('INSERT INTO events(event_id,name,occurred_at,data) VALUES(?,?,?,?)',
                   (str(uuid.uuid4()), name, time.time(), encode(data)))

    def event(self, name, data, *, dedup_key=None, interval=3600):
        with self.transaction() as db:
            if dedup_key:
                row = db.execute('SELECT last_at FROM event_dedup WHERE key=?', (dedup_key,)).fetchone()
                if row and time.time() - row['last_at'] < interval:
                    return False
                db.execute('INSERT OR REPLACE INTO event_dedup VALUES(?,?)', (dedup_key, time.time()))
            self._event(db, name, data)
        return True

    def beat(self, name):
        with self.transaction() as db:
            db.execute('INSERT OR REPLACE INTO heartbeats VALUES(?,?)', (name, time.time()))

    def audit(self, principal, action, target, outcome):
        with self.transaction() as db:
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (time.time(), principal, action, target, outcome))
