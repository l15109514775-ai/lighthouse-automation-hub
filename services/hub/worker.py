import random
import json
import sqlite3
import threading
import time
import errno
import logging
from .errors import HubError, PublishUnknown, ResourceDeferred, YouTubeReadinessDeferred, LeaseLost
from .locks import exclusive
from .outcomes import JobOutcome

# Unexpected exceptions in these acquisition types are resumable: their handlers
# never publish and their downloads continue from retained partials. Publishing
# types stay terminal, because an unexpected error after a real submission must
# never be replayed automatically.
RETRYABLE_UNEXPECTED = frozenset(('youtube_replay_plan_v4', 'youtube_replay_download_v4',
    'youtube_replay_readiness_v4', 'youtube_replay_refresh_v4', 'youtube_download',
    'youtube_download_v2', 'youtube_download_v3', 'youtube_highest_prepare_v3'))


class Worker:
    def __init__(self, settings, store, handlers, *, resource_deferrals=None,readiness_deferrals=None):
        self.settings, self.store, self.handlers = settings, store, handlers
        self.resource_deferrals = dict(resource_deferrals or {})
        self.readiness_deferrals = dict(readiness_deferrals or {})

    def _resource_deferred(self,job,error):
        try:self.resource_deferrals[job['job_type']](job,error)
        except PublishUnknown as exc:self._resource_failure(job,'publish_unknown',exc.code)
        except HubError as exc:self._resource_failure(job,'failed',exc.code)
        except Exception:self._resource_failure(job,'failed','RESOURCE_WAIT_PERSIST_FAILED')

    def _resource_failure(self,job,state,code):
        current=self.store.get(job['job_id'])
        if current and current['state']=='processing' and current['lease_owner']==job.get('lease_owner'):
            self.store.finish(job,state,code=code,result=current['result'],stage=current['stage'])

    def _heartbeat(self, job, stop, lost=None):
        try:
            while not stop.wait(min(30, self.settings.config['lease_seconds'] / 3)):
                if not self.store.heartbeat(job, self.settings.config['lease_seconds']):
                    raise LeaseLost()
                self.store.beat('worker:' + job['queue'])
        except Exception:
            # Fail closed on failed renewal, including SQLite errors. Do not
            # print exception values or allow a returned handler to commit.
            if lost is not None:
                lost.set()
            logging.getLogger(__name__).error('WORKER_LEASE_RENEWAL_FAILED')

    def _defer_resource_busy(self, job):
        """Waiting for an unacquired OS resource is not an external attempt.

        The transaction fences cancellation/recovery and preserves checkpoints.
        Existing dispatch intent always remains lookup-only instead of entering
        this free retry path, including a publishing child linked to its parent.
        """
        now = time.time()
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
            if (not row or row['state'] != 'processing' or row['lease_owner'] != job.get('lease_owner') or
                    (row['lease_until'] or 0) <= now or row['attempts'] != job['attempts'] or row['attempts'] < 1):
                return False
            linked = [job['job_id']]
            parent = json.loads(row['payload']).get('parent_job_id')
            if isinstance(parent, str):
                linked.append(parent)
            marks = ','.join('?' for _ in linked)
            dispatched = db.execute(f"SELECT 1 FROM publications WHERE job_id IN ({marks}) AND (state<>'rejected' OR external_id IS NOT NULL) LIMIT 1",
                                    linked).fetchone()
            paid_capture=row['job_type']=='x_discovery' and db.execute("SELECT 1 FROM api_calls WHERE job_id=? AND provider='x' LIMIT 1",(job['job_id'],)).fetchone()
            if paid_capture and not dispatched:
                # An unexpected busy error after a real X attempt is not a
                # free claim. Keep its true attempt and evidence for scoped
                # reconciliation, rather than replaying paid reads forever.
                db.execute("UPDATE jobs SET state='awaiting_review',error_code='X_PAID_CAPTURE_RECONCILIATION_REQUIRED',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?",
                    (now,job['job_id']))
                self.store._event(db,'review.required',{'job_id':job['job_id'],'code':'X_PAID_CAPTURE_RECONCILIATION_REQUIRED','queue':row['queue']})
                return True
            state = 'publish_unknown' if dispatched else 'retry_scheduled'
            code = 'RECONCILE_BEFORE_RETRY' if dispatched else 'HEAVY_RESOURCE_BUSY'
            attempts = row['attempts'] if dispatched else row['attempts'] - 1
            due = now if dispatched else now + 15 + random.random() * 15
            db.execute('''UPDATE jobs SET state=?,error_code=?,attempts=?,next_run=?,updated_at=?,
                lease_owner=NULL,lease_until=NULL WHERE job_id=? AND state='processing' AND lease_owner=?''',
                (state, code, attempts, due, now, job['job_id'], job['lease_owner']))
            if dispatched:
                self.store._event(db, 'review.required', {'job_id': job['job_id'], 'code': code, 'queue': row['queue']})
        job.update(state=state, error_code=code, attempts=attempts, next_run=due, updated_at=now,
                   result=json.loads(row['result']), stage=row['stage'], lease_owner=None, lease_until=None)
        return True

    def once(self, queue):
        cfg = self.settings.config
        try:
            with exclusive(self.settings.root / 'data/locks' / queue):
                self.store.recover(queue, cfg['max_attempts'], all_processing=True, job_types=tuple(self.handlers))
                self.store.beat('worker:' + queue)
                job = self.store.claim(queue, cfg['lease_seconds'], cfg['max_attempts'], job_types=tuple(self.handlers))
                if not job:
                    return False
                stop = threading.Event()
                lost = threading.Event()
                heartbeat = threading.Thread(target=self._heartbeat, args=(job, stop, lost), daemon=True)
                heartbeat.start()
                try:
                    if job['job_type'] not in self.handlers:
                        raise HubError('JOB_TYPE_UNSUPPORTED', review=True)
                    from .source_controls import guard_job
                    guard_job(self.settings, self.store, job)
                    result = self.handlers[job['job_type']](job)
                    if lost.is_set():
                        raise LeaseLost()
                    if isinstance(result,JobOutcome):
                        if result.state not in ('completed','awaiting_moderation','upload_pending','failed'):
                            raise HubError('INVALID_HANDLER_OUTCOME',review=True)
                        self.store.finish(job,result.state,result=result.result,external_id=result.external_id,
                                          code=result.error_code,stage='done' if result.state=='completed' else 'published_pending')
                    else:
                        self.store.finish(job, 'completed', result=result, stage='done')
                except LeaseLost:
                    raise
                except sqlite3.Error:
                    raise
                except PublishUnknown as exc:
                    if lost.is_set():
                        raise LeaseLost()
                    self.store.finish(job, 'publish_unknown', code=exc.code)
                except HubError as exc:
                    if lost.is_set():
                        raise LeaseLost()
                    if isinstance(exc,ResourceDeferred) and job['job_type'] in self.resource_deferrals:
                        self._resource_deferred(job,exc)
                    elif isinstance(exc,YouTubeReadinessDeferred) and job['job_type'] in self.readiness_deferrals:
                        try:self.readiness_deferrals[job['job_type']](job,exc)
                        except PublishUnknown as failed:self._resource_failure(job,'publish_unknown',failed.code)
                        except HubError as failed:self._resource_failure(job,'failed',failed.code)
                        except Exception:self._resource_failure(job,'failed','YOUTUBE_READINESS_PERSIST_FAILED')
                    elif exc.code == 'HEAVY_RESOURCE_BUSY' and exc.retryable and not exc.review:
                        self._defer_resource_busy(job)
                    else:
                        if exc.code in ('AUTH_EXPIRED', 'YOUTUBE_SESSION_AUTH_EXPIRED', 'YOUTUBE_SESSION_UNAVAILABLE'):
                            state = 'auth_expired'
                        elif exc.review:
                            state = 'awaiting_review'
                        elif exc.retryable and job['attempts'] < cfg['max_attempts']:
                            state = 'retry_scheduled'
                        else:
                            state = 'failed'
                        delay = max(exc.retry_after, min(3600, 15 * 2 ** (job['attempts'] - 1)) + random.random() * 5)
                        self.store.finish(job, state, code=exc.code, delay=delay)
                except Exception as exc:
                    # Exception strings can contain keys, provider bodies or signed URLs.
                    if lost.is_set():
                        raise LeaseLost()
                    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EACCES, errno.EPERM):
                        code = 'DISK_LOW' if exc.errno in (errno.ENOSPC, errno.EDQUOT) else 'FILESYSTEM_NOT_WRITABLE'
                        self.store.finish(job, 'awaiting_review', code=code)
                        return True
                    retry = job['attempts'] < cfg['max_attempts'] and job['job_type'] in RETRYABLE_UNEXPECTED
                    if retry:
                        delay = min(3600, 15 * 2 ** (job['attempts'] - 1)) + random.random() * 5
                        self.store.finish(job, 'retry_scheduled', code='UNEXPECTED_RUNTIME_ERROR', delay=delay)
                    else:
                        self.store.finish(job, 'failed', code='UNEXPECTED_RUNTIME_ERROR')
                finally:
                    stop.set()
                    heartbeat.join(timeout=2)
                return True
        except LeaseLost:
            logging.getLogger(__name__).error('WORKER_LEASE_LOST')
            return False
        except OSError as exc:
            # Another worker owns this queue; never recover or run its in-flight work.
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                logging.getLogger(__name__).error('WORKER_LOCK_IO_ERROR')
            return False
        except sqlite3.Error as exc:
            code = getattr(exc, 'sqlite_errorcode', 0)
            label = 'SQLITE_BUSY' if code & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else 'SQLITE_UNAVAILABLE'
            logging.getLogger(__name__).error(label)
            return False

    def run(self, queue):
        while True:
            if not self.once(queue):
                time.sleep(1)
