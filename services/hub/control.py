from datetime import datetime
import hmac
import os
import shutil
import time
from zoneinfo import ZoneInfo
import psutil
from .budget import Budget
from .errors import HubError
from .monitoring import host_snapshot

QUEUES = {'tweet', 'video', 'system'}


class Control:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def authorize(self, token, *, write=False):
        read = os.getenv('HUB_READ_TOKEN', '')
        maintain = os.getenv('HUB_MAINTAIN_TOKEN', '')
        if read and maintain and hmac.compare_digest(read, maintain):
            raise HubError('TOKENS_MUST_BE_DISTINCT')
        if token and maintain and hmac.compare_digest(token, maintain):
            return 'maintenance'
        if not write and token and read and hmac.compare_digest(token, read):
            return 'reader'
        self.store.audit('unauthenticated', 'authorize_write' if write else 'authorize_read', 'hub', 'denied')
        raise HubError('ACCESS_DENIED')

    def status(self):
        from .youtube_monitor import YouTubeMonitor
        from .readiness import automation_readiness
        from .video_reviews import VideoReviews
        from .bilibili_tokens import BilibiliTokens
        from .source_receipts import SourceReceipts
        from .tweet_reviews import TweetReviews
        from .model_receipts import ModelReceipts
        disk = shutil.disk_usage(self.settings.root)
        memory = psutil.virtual_memory()
        return {'dry_run': self.settings.dry_run, 'policy_version': self.settings.config['policy_version'],
                'jobs': self.store.query('SELECT state,COUNT(*) count FROM jobs GROUP BY state'),
                'errors': self.store.query('SELECT error_code,COUNT(*) count FROM jobs WHERE error_code IS NOT NULL GROUP BY error_code'),
                'pipelines': self.store.query('SELECT * FROM pipelines'),
                'heartbeats': self.store.query('SELECT * FROM heartbeats'),
                'cpu_percent': psutil.cpu_percent(), 'memory_total_bytes': memory.total,
                'memory_available_bytes': memory.available, 'disk_free_bytes': disk.free,
                'disk_floor_bytes': int(self.settings.config['disk_floor_gb'] * 1_000_000_000),
                'container_status':host_snapshot(self.settings),
                'youtube':YouTubeMonitor(self.settings,self.store,None).status(),
                'automation':automation_readiness(self.settings,self.store),
                'human_video_review':VideoReviews(self.settings,self.store).readiness(),
                'bilibili_oauth':BilibiliTokens(self.settings,self.store).status(),
                'source_receipts':SourceReceipts(self.settings,self.store).status(),
                'human_tweet_review':TweetReviews(self.settings,self.store).readiness(),
                'model_receipts':ModelReceipts(self.store).status(),
                'external_backup_configured': False,
                'external_backup_destination_provided': bool(os.getenv('BACKUP_EXTERNAL_DESTINATION')),
                'api_usage': Budget(self.store, self.settings.config['budgets']).report()}

    def list_jobs(self, state=None, limit=50):
        if not 1 <= limit <= 200:
            raise HubError('INVALID_LIMIT')
        if state is not None and state not in {'pending','processing','awaiting_review','retry_scheduled','publish_unknown',
                'awaiting_moderation','auth_expired','upload_pending','completed','failed'}:
            raise HubError('INVALID_STATE')
        return self.store.query('''SELECT job_id,source_platform,source_id,job_type,queue,state,attempts,error_code,
            created_at,updated_at,external_publish_id,policy_version FROM jobs WHERE (? IS NULL OR state=?)
            ORDER BY created_at DESC LIMIT ?''', (state, state, limit))

    def get_job(self, job_id):
        import uuid
        try:
            uuid.UUID(job_id)
        except (ValueError, TypeError):
            raise HubError('INVALID_JOB_ID') from None
        job = self.store.get(job_id)
        if not job:
            raise HubError('JOB_NOT_FOUND')
        job.pop('lease_owner', None)
        return job

    def retry(self, principal, job_id):
        if principal != 'maintenance':
            self.store.audit(principal, 'retry_job', job_id, 'denied')
            raise HubError('ACCESS_DENIED')
        self.get_job(job_id)
        with self.store.transaction() as db:
            row = db.execute('SELECT state,error_code FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            publication = db.execute('SELECT state,external_id FROM publications WHERE job_id=?', (job_id,)).fetchone()
            # Review/authorization and unknown publication require separate acceptance/reconciliation.
            allowed_codes = {'NETWORK_ERROR','API_RATE_LIMITED','API_SERVER_ERROR','API_INVALID_JSON',
                             'PROCESS_INTERRUPTED','RETRY_EXHAUSTED_AFTER_RESTART','UNEXPECTED_RUNTIME_ERROR',
                             'MODEL_CREDENTIAL_OR_SELECTION_REQUIRED','X_CREDENTIAL_REQUIRED','YOUTUBE_CREDENTIAL_REQUIRED',
                             'API_BUDGET_EXCEEDED','AUTH_EXPIRED'}
            configuration_errors = {'MODEL_CREDENTIAL_OR_SELECTION_REQUIRED','X_CREDENTIAL_REQUIRED','YOUTUBE_CREDENTIAL_REQUIRED',
                                    'API_BUDGET_EXCEEDED','AUTH_EXPIRED'}
            eligible_state = row['state'] == 'failed' or (row['state'] in ('auth_expired','awaiting_review') and row['error_code'] in configuration_errors)
            unresolved_publication=publication and (publication['state']!='rejected' or publication['external_id'])
            if not eligible_state or row['error_code'] not in allowed_codes or unresolved_publication:
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                           (time.time(), principal, 'retry_job', job_id, 'blocked'))
                blocked = True
            else:
                blocked = False
                db.execute("UPDATE jobs SET state='pending',attempts=0,next_run=0,error_code=NULL,updated_at=? WHERE job_id=?",
                           (time.time(), job_id))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                           (time.time(), principal, 'retry_job', job_id, 'ok'))
        if blocked:
            raise HubError('RETRY_REQUIRES_REVIEW_OR_RECONCILIATION')
        return {'job_id': job_id, 'state': 'pending'}

    def pause(self, principal, queue, paused):
        if principal != 'maintenance':
            self.store.audit(principal, 'pause_pipeline', queue, 'denied')
            raise HubError('ACCESS_DENIED')
        if queue not in QUEUES or not isinstance(paused, bool):
            raise HubError('INVALID_PIPELINE')
        with self.store.transaction() as db:
            db.execute('UPDATE pipelines SET paused=? WHERE name=?', (paused, queue))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (time.time(), principal, 'pause_pipeline' if paused else 'resume_pipeline', queue, 'ok'))
        return {'pipeline': queue, 'paused': paused, 'in_flight_jobs': 'allowed_to_checkpoint'}

    def daily_report(self):
        local = datetime.now(ZoneInfo('Asia/Shanghai'))
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        return {'date': local.date().isoformat(), 'timezone': 'Asia/Shanghai',
                'states': self.store.query('SELECT state,COUNT(*) count FROM jobs WHERE updated_at>=? GROUP BY state', (start,)),
                'events': self.store.query('SELECT name,COUNT(*) count FROM events WHERE occurred_at>=? GROUP BY name', (start,)),
                'api_usage': Budget(self.store, self.settings.config['budgets']).report()}
