from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import time
import uuid
import os
from apscheduler.schedulers.blocking import BlockingScheduler
from services.hub.db import encode
from services.hub.locks import exclusive


class DurableScheduler:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def daily_due(self, local_time, now):
        hour, minute = map(int, local_time.split(':'))
        local = datetime.fromtimestamp(now, ZoneInfo(self.settings.config['timezone']))
        due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if due.timestamp() <= now:
            due += timedelta(days=1)
        return due.timestamp()

    def tick(self, now=None):
        now = now if now is not None else time.time()
        config = self.settings.config
        rules = [('youtube_discovery', 'video', None), ('maintenance', 'system', 60),
                 ('backup', 'system', 86400), ('cleanup', 'system', 86400)]
        if config.get('x_api_enabled', False) or config.get('x_web_enabled', False):
            rules.insert(0, ('x_discovery', 'tweet', config['tweet_poll_minutes'] * 60))
        if config.get('youtube_feed_enabled', False):
            rules.append(('youtube_feed', 'system', config['youtube_feed_poll_minutes'] * 60))
        if config.get('youtube_live_monitor_enabled', False) and os.getenv('YOUTUBE_API_KEY'):
            rules.append(('youtube_live_monitor', 'system', config['youtube_live_poll_minutes'] * 60))
        if config.get('bilibili_token_refresh_enabled', False):
            rules.append(('bilibili_token_check', 'system', 60))
        with self.store.transaction() as db:
            if config.get('machine_tweet_enabled',False) and db.execute('''SELECT 1 FROM publications p JOIN jobs j ON j.job_id=p.job_id
                WHERE p.kind IN ('dynamic_website_machine_v1','dynamic_website_machine_v2','dynamic_website_machine_v3_api','dynamic_website_machine_v4_api_relations') AND p.state IN ('dispatching','unknown')
                AND COALESCE(json_extract(j.result,'$.machine_reconcile.attempts'),0)<? LIMIT 1''',(config['max_attempts'],)).fetchone():
                rules.append(('tweet_machine_reconcile','tweet',config.get('bilibili_reconcile_minutes',5)*60))
            if config.get('web_reviewed_queue_enabled',False) and db.execute('''SELECT 1 FROM publications p JOIN jobs j ON j.job_id=p.job_id
                WHERE p.kind='dynamic_website_supervised' AND p.state IN ('dispatching','unknown') AND j.state='publish_unknown'
                AND COALESCE(json_extract(j.result,'$.web_reconcile.attempts'),0)<? LIMIT 1''',(config['max_attempts'],)).fetchone():
                rules.append(('tweet_web_reconcile','tweet',config.get('bilibili_reconcile_minutes',5)*60))
            if db.execute('''SELECT 1 FROM publications p JOIN jobs j ON j.job_id=p.job_id
                WHERE p.platform='bilibili' AND p.kind='video' AND (p.state IN ('dispatching','unknown','awaiting_moderation','upload_pending')
                    OR j.state='publish_unknown') LIMIT 1''').fetchone():
                rules.append(('bilibili_reconcile','system',config.get('bilibili_reconcile_minutes',5)*60))
            for name, queue, interval in rules:
                initial = self.daily_due(config['youtube_check_time'], now) if interval is None else now
                db.execute('INSERT OR IGNORE INTO schedules(name,next_due) VALUES(?,?)', (name, initial))
                due = db.execute('SELECT next_due FROM schedules WHERE name=?', (name,)).fetchone()['next_due']
                if due > now:
                    continue
                if name in ('x_discovery','tweet_web_reconcile','tweet_machine_reconcile','youtube_feed','youtube_live_monitor','bilibili_reconcile','bilibili_token_check') and db.execute('''SELECT 1 FROM jobs
                    WHERE job_type=? AND state IN ('pending','processing','retry_scheduled') LIMIT 1''', (name,)).fetchone():
                    db.execute('UPDATE schedules SET next_due=? WHERE name=?', (now + interval, name))
                    continue
                # One catch-up job per schedule. Its insertion and next_due advance are atomic.
                ident = str(uuid.uuid4())
                db.execute('''INSERT OR IGNORE INTO jobs(job_id,source_platform,source_id,job_type,queue,state,
                    created_at,updated_at,policy_version,payload) VALUES(?,?,?,?,?,'pending',?,?,?,?)''',
                    (ident, 'scheduler', f'{name}:{int(due)}', name, queue, now, now, config['policy_version'], encode({})))
                next_due = self.daily_due(config['youtube_check_time'], now) if interval is None else now + interval
                db.execute('UPDATE schedules SET next_due=? WHERE name=?', (next_due, name))
            db.execute('INSERT OR REPLACE INTO heartbeats VALUES(?,?)', ('scheduler', now))

    def run(self):
        with exclusive(self.settings.root / 'data/locks/scheduler'):
            scheduler = BlockingScheduler(timezone=self.settings.config['timezone'])
            scheduler.add_job(self.tick, 'interval', seconds=5, max_instances=1, coalesce=True)
            self.tick()
            scheduler.start()
