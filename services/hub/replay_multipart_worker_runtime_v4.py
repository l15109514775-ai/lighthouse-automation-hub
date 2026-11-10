"""Type-scoped whole-replay acquisition; native multipart publication is closed.

The producer continues to register official observations while an ordinary
Worker holds a long media lease. Neither stream claims or recovers old video
jobs. No browser, publisher, or automatic deletion route is registered here.
"""
import argparse
import json
import logging
import os
import threading
import time
import uuid

from .errors import HubError
from .job_scope import job_type_filter
from .locks import require_queue_lock
from .replay_fifo_v4 import (ReplayFIFOV4, VERSION as FIFO_VERSION,
    PLAN_JOB, DOWNLOAD_JOB, REFRESH_JOB, PUBLISH_JOB)
from .youtube_download_v4 import (YouTubeWholeReplayPrepare, worker_handlers,
    resource_callbacks, resource_validators)
from .youtube_readiness_v4 import WholeReadinessCoordinatorV4, JOB_TYPE as READINESS_JOB

VERSION = 'replay-multipart-worker-runtime-v4'
PRODUCER_INTERVAL = 60
PUBLICATION_ENABLED = False
JOB_TYPES = (PLAN_JOB, DOWNLOAD_JOB, REFRESH_JOB, READINESS_JOB)


class QueueView:
    """The existing video OS lock, with a positive V4 type/platform/policy set.

    In particular, a stale V2/V3 processing row is not ours to recover. A V4
    reservation would become unknown, never a free retry, even though this
    acquisition-only release does not itself create publications.
    """
    def __init__(self, store, *, acquisition_enabled=False, heartbeat_name=None):
        if type(acquisition_enabled) is not bool:
            raise ValueError('REPLAY_MULTIPART_QUEUE_MODE_INVALID')
        self.store, self.acquisition_enabled = store, acquisition_enabled
        self.heartbeat_name = heartbeat_name
        self.heartbeat_instance = None

    def __getattr__(self, name):
        return getattr(self.store, name)

    @staticmethod
    def _queue(queue):
        if queue != 'video':
            raise ValueError('REPLAY_MULTIPART_QUEUE_SCOPE')

    @staticmethod
    def _scope():
        return ("policy_version=? AND ((source_platform='youtube' AND job_type IN (?,?,?)) "
                "OR (source_platform='maintenance' AND job_type=?))",
                (FIFO_VERSION, PLAN_JOB, DOWNLOAD_JOB, READINESS_JOB, REFRESH_JOB))

    def beat(self, name):
        if name != 'worker:video':
            raise ValueError('REPLAY_MULTIPART_HEARTBEAT_SCOPE')
        self.store.beat(name)
        self.store.beat('worker:video-main-v4')
        if self.heartbeat_name:
            self.store.beat(self.heartbeat_name)
        if self.heartbeat_instance:
            self.store.beat(self.heartbeat_instance)

    def claim(self, queue, lease_seconds, max_attempts, *, job_types=None):
        self._queue(queue)
        if not self.acquisition_enabled:
            return None
        scope, args = self._scope()
        types_sql, types = job_type_filter(job_types)
        scope, args = f'({scope}) AND {types_sql}', (*args, *types)
        now, owner = time.time(), str(uuid.uuid4())
        with self.store.transaction() as db:
            if db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']:
                return None
            row = db.execute(f"""SELECT job_id FROM jobs WHERE queue='video' AND {scope}
                AND state IN ('pending','retry_scheduled') AND next_run<=? AND attempts<?
                ORDER BY created_at,job_id LIMIT 1""", (*args, now, max_attempts)).fetchone()
            if row is None:
                return None
            db.execute("""UPDATE jobs SET state='processing',attempts=attempts+1,
                lease_owner=?,lease_until=?,updated_at=? WHERE job_id=?""",
                (owner, now + lease_seconds, now, row['job_id']))
        return self.store.get(row['job_id'])

    def recover(self, queue, max_attempts, *, all_processing=False, job_types=None):
        # Worker calls this only while it owns the real video OS lock.
        self._queue(queue)
        if all_processing:
            require_queue_lock(self.store, queue)
        if not self.acquisition_enabled:
            return 0
        scope, args = self._scope()
        types_sql, types = job_type_filter(job_types)
        scope, args = f'({scope}) AND {types_sql}', (*args, *types)
        now = time.time()
        with self.store.transaction() as db:
            rows = db.execute(f"""SELECT * FROM jobs WHERE queue='video' AND {scope}
                AND state='processing' AND (? OR lease_until<?)""", (*args, all_processing, now)).fetchall()
            for row in rows:
                publication = db.execute('SELECT * FROM publications WHERE job_id=?', (row['job_id'],)).fetchone()
                if publication and publication['state'] != 'rejected':
                    state, code = 'publish_unknown', 'RECONCILE_BEFORE_RETRY'
                elif row['attempts'] >= max_attempts:
                    state, code = 'failed', 'RETRY_EXHAUSTED_AFTER_RESTART'
                else:
                    state, code = 'retry_scheduled', 'PROCESS_INTERRUPTED'
                db.execute("""UPDATE jobs SET state=?,error_code=?,lease_owner=NULL,lease_until=NULL,
                    next_run=?,updated_at=? WHERE job_id=?""", (state, code, now, now, row['job_id']))
                if state == 'failed':
                    self.store._event(db, 'task.failed', {'job_id': row['job_id'], 'code': code, 'queue': queue})
        return len(rows)


class Coordinator:
    def __init__(self, settings, store, *, mode='register_only'):
        if mode not in ('register_only', 'acquisition_only'):
            raise ValueError('REPLAY_MULTIPART_MODE_INVALID')
        self.mode = mode
        self.settings, self.store = settings, store
        self.producer_heartbeat = None
        self.fifo = ReplayFIFOV4(settings, store)
        self.prepare = YouTubeWholeReplayPrepare(settings, store)
        self.readiness = WholeReadinessCoordinatorV4(settings, store)
        self.validators = resource_validators(settings, store)
        self._policy_binding = None
        self.require_policy()
        handlers = worker_handlers(settings, store)
        if set(handlers) != {PLAN_JOB, DOWNLOAD_JOB, READINESS_JOB} or not all(map(callable, handlers.values())):
            raise HubError('REPLAY_MULTIPART_HANDLER_SET_CHANGED', review=True)
        handlers[REFRESH_JOB] = self.fifo.process_refresh
        from .worker import Worker
        self.worker = Worker(settings, QueueView(store, acquisition_enabled=mode == 'acquisition_only',
            heartbeat_name='worker:replay-acquisition'), handlers,
            resource_deferrals=resource_callbacks(settings, store),
            readiness_deferrals=self.readiness.callbacks())

    def require_policy(self):
        fifo = self.fifo.policy()
        if self.mode == 'register_only':
            binding = (fifo['_sha256'],)
            if self._policy_binding is not None and binding != self._policy_binding:
                raise HubError('REPLAY_MULTIPART_RUNTIME_POLICY_CHANGED', review=True)
            self._policy_binding = binding
            return None
        acquisition = self.prepare.policy()
        planning = acquisition['_planning']
        from .replay_multipart_v4 import digest
        if fifo['planning_policy_sha256'] != digest(planning):
            raise HubError('REPLAY_MULTIPART_POLICY_PAIR_CHANGED', review=True)
        binding = (fifo['_sha256'], acquisition['_sha256'], digest(planning))
        if self._policy_binding is not None and binding != self._policy_binding:
            raise HubError('REPLAY_MULTIPART_RUNTIME_POLICY_CHANGED', review=True)
        self._policy_binding = binding
        return planning

    def tick(self):
        """Local producers only; official HTTP belongs to leased handlers."""
        from .locks import resource_guard
        planning = self.require_policy()
        with resource_guard(self.settings.root / 'data/locks/replay-multipart-producer-v4'):
            result = {'version': VERSION, 'mode': self.mode, 'publisher_registered': False,
                'publications': 0, 'external_requests': 0, 'steps': {}}
            operations = (
                ('sync', self.sync_states),
                ('register', self.fifo.register_all_observed),
            )
            for name, operation in operations:
                result['steps'][name] = operation()
            paused = self.store.query("SELECT paused FROM pipelines WHERE name='video'")[0]['paused']
            if self.mode == 'register_only':
                result['dispatch_state'] = 'register_only'
            elif paused:
                result['dispatch_state'] = 'paused'
            else:
                result['steps']['readiness'] = self.readiness.tick()
                result['steps']['resources'] = self.fifo.resource_tick(self.validators)
                result['steps']['plan'] = self.fifo.enqueue_plan_tick(self.worker.handlers)
                result['steps']['download'] = self.fifo.enqueue_download_tick(self.worker.handlers, planning)
                result['dispatch_state'] = 'acquisition_only'
            self.store.beat('worker:video-producer-v4')
            self.store.beat('worker:replay-acquisition-producer')
            if self.producer_heartbeat:
                self.store.beat(self.producer_heartbeat)
            if self.mode == 'register_only':
                # Like an idle ordinary Worker, this is the real live service,
                # not a fabricated processing-job lease or download heartbeat.
                self.worker.store.beat('worker:video')
            return result

    def sync_states(self):
        # Existing V5 groups represent several ordinary publications. Applying
        # V4's single-publication projection would incorrectly mark them unknown.
        # Reuse only the read-only projection; do not register any publisher.
        if self.store.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='replay_part_groups_v5'"):
            from .replay_parts_v5 import ReplayPartGroupsV5
            projection = ReplayPartGroupsV5(self.settings, self.store).fifo_projection
            return self.fifo.sync_states(publication_projection=projection)
        return self.fifo.sync_states()

    def bind_health(self, instance):
        from .acquisition_health import instance_names
        if self.producer_heartbeat is not None:
            raise ValueError('ACQUISITION_HEALTH_ALREADY_BOUND')
        main, producer = instance_names(instance)
        self.worker.store.heartbeat_instance = main
        self.producer_heartbeat = producer

    def idle_heartbeat(self):
        # Waiting behind another heavy worker is healthy; it grants no task lease.
        self.store.beat('worker:replay-acquisition')
        if self.worker.store.heartbeat_instance:
            self.store.beat(self.worker.store.heartbeat_instance)


def build_worker(settings, store, *, mode='register_only'):
    coordinator = Coordinator(settings, store, mode=mode)
    return coordinator.worker, coordinator


def producer_loop(coordinator, stop, *, fatal=os._exit):
    try:
        while not stop.wait(PRODUCER_INTERVAL):
            try:
                coordinator.tick()
            except HubError as exc:
                if exc.code != 'HEAVY_RESOURCE_BUSY':
                    raise
    except BaseException:
        # Do not let a healthy long-media heartbeat hide a dead producer.
        logging.getLogger(__name__).critical('MULTIPART_V4_PRODUCER_STOPPED')
        stop.set()
        fatal(70)


def run_loop(worker, coordinator, *, once=False, stop=None):
    stop = stop or threading.Event()
    coordinator.tick()
    if coordinator.mode == 'register_only':
        if once:
            return False
        while not stop.wait(PRODUCER_INTERVAL):
            coordinator.tick()
        return False
    if once:
        return worker.once('video')
    producer = threading.Thread(target=producer_loop, args=(coordinator, stop),
                                name='replay-multipart-producer-v4', daemon=True)
    producer.start()
    try:
        while not stop.is_set():
            idle_heartbeat = getattr(coordinator, 'idle_heartbeat', None)
            if idle_heartbeat:
                idle_heartbeat()
            if not worker.once('video'):
                stop.wait(1)
    finally:
        stop.set()
        producer.join(timeout=5)


def main():
    from .config import Settings
    from .db import Store
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--check', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--register-only', action='store_true')
    mode.add_argument('--acquisition-only', action='store_true')
    args = parser.parse_args()
    if os.name != 'posix' or os.getuid() != 10001:
        raise HubError('REPLAY_MULTIPART_APP_UID_REQUIRED', review=True)
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s')
    for name in ('httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    settings = Settings.load()
    settings.prepare()
    store = Store(settings.db_path)
    # Migrations remain the existing release's responsibility. This entry only
    # creates its explicit V4 tables when tick runs, never during --check.
    selected_mode = 'acquisition_only' if args.acquisition_only else 'register_only'
    worker, coordinator = build_worker(settings, store, mode=selected_mode)
    if args.check:
        print(json.dumps({'version': VERSION, 'mode': selected_mode, 'handlers': sorted(worker.handlers),
            'publisher_registered': False, 'publications': 0, 'external_requests': 0}))
        return
    if selected_mode == 'acquisition_only':
        from .youtube_credentials import load_youtube_key
        if os.environ.get('YOUTUBE_API_KEY_FILE') != '/run/secrets/youtube-api-key':
            raise HubError('REPLAY_MULTIPART_KEY_FILE_REQUIRED', review=True)
        load_youtube_key('/run/secrets/youtube-api-key')
        from .acquisition_health import start_epoch
        coordinator.bind_health(start_epoch())
    run_loop(worker, coordinator, once=args.once)


if __name__ == '__main__':
    main()
