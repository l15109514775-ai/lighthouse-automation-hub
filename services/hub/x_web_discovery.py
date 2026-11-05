"""Queue ingestion of bounded rendered-page observations; no paid X API calls."""
import shutil
import time
from pathlib import Path
from adapters.x_web import XWeb
from .db import encode
from .errors import HubError
from .maintenance import Maintenance
from .web_source import WebSources


class XWebDiscovery:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.browser = XWeb(settings)

    def run(self, job):
        summaries, failures, created = [], [], 0
        for source in self.settings.config['sources']:
            handle = source['x_handle']
            summary = {'handle':handle, 'posts_observed':0, 'new_jobs':0, 'paid_api_calls':0,
                       'timeline_complete':False, 'human_review':False,'cleared_sources_skipped':0}
            try:
                Maintenance(self.settings,self.store).check_disk(reserve_bytes=2_000_000)
                # Establish a fixed monitoring start once; it is never advanced
                # after a partial web snapshot. Old pinned posts are not new work.
                with self.store.transaction() as db:
                    db.execute('INSERT OR IGNORE INTO source_cursors VALUES(?,?)',
                        ('x-web:start:'+handle,str(time.time()-86400)))
                    baseline=float(db.execute('SELECT value FROM source_cursors WHERE key=?',('x-web:start:'+handle,)).fetchone()['value'])
                if self.settings.config.get('machine_tweet_enabled',False):
                    from .machine_routes import machine_service
                    machine=machine_service(self.settings,self.store)
                    policy=machine.policy()
                    job_policy=machine.job_policy()
                    baseline=max(baseline,policy['activation_at']-policy['max_post_age_seconds'],time.time()-policy['max_post_age_seconds'])
                    known=self.store.query("SELECT source_id FROM jobs WHERE source_platform='x' AND job_type='tweet_process' AND ((policy_version=? AND (EXISTS(SELECT 1 FROM web_source_bindings b WHERE b.job_id=jobs.job_id) OR EXISTS(SELECT 1 FROM extended_web_source_bindings e WHERE e.job_id=jobs.job_id))) OR external_publish_id IS NOT NULL) ORDER BY created_at DESC LIMIT 1000",(job_policy,))
                    # Tombstones are source-level, not tied to a policy or
                    # capture generation. Prioritize them within the browser's
                    # bounded seen list; recheck every returned post below too.
                    cleared=self.store.query("SELECT source_id FROM source_tombstones WHERE source_platform='x' ORDER BY marked_at DESC,source_id")
                    known=[{'source_id':ident} for ident in dict.fromkeys(
                        [r['source_id'] for r in cleared]+[r['source_id'] for r in known])][:1000]
                else:
                    job_policy=self.settings.config['policy_version']
                    known = self.store.query("SELECT source_id FROM jobs WHERE source_platform='x' AND job_type='tweet_process' ORDER BY created_at DESC LIMIT 1000")
                response = self.browser.latest(handle, job_id=job['job_id'], seen_ids=[r['source_id'] for r in known],not_before=baseline,
                    trigger='durable_scheduler' if job['source_platform']=='scheduler' else 'durable_job')
                summary.update(result_file=response['result_file'], state=response['state'])
                for record in response['posts']:
                    payload=record['payload'];post=payload['post'];summary['posts_observed']+=1
                    if self.settings.config.get('machine_tweet_enabled',False) and self.store.query(
                            "SELECT 1 FROM source_tombstones WHERE source_platform='x' AND source_id=?",(post['id'],)):
                        summary['cleared_sources_skipped']+=1
                        continue
                    protocol=payload['capture'].get('protocol')
                    if protocol in ('x-web-dom-observation-v2','x-web-extended-observation-v1'):
                        if protocol=='x-web-extended-observation-v1':
                            from .extended_web_source import ExtendedWebSources
                            ingested=ExtendedWebSources(self.settings,self.store).ingest(job,record,job_policy)
                        else:ingested=WebSources(self.settings,self.store).ingest(job,record,job_policy)
                        ident=ingested['job_id'];is_new=ingested['created']
                        if not ingested['binding_present']:
                            self.store.event('task.failed',{'job_id':ident,'code':'WEB_EXISTING_UNBOUND_SOURCE_PRESERVED'},
                                dedup_key='web-unbound:'+ident)
                            raise HubError('WEB_EXISTING_UNBOUND_SOURCE_PRESERVED')
                    else:
                        prior=self.store.query("SELECT job_id FROM jobs WHERE source_platform='x' AND source_id=? AND job_type='tweet_process' AND policy_version=?",
                            (post['id'],job_policy))
                        ident=self.store.enqueue('x',post['id'],'tweet_process','tweet',payload,job_policy)
                        is_new=not prior
                    if is_new and protocol not in ('x-web-dom-observation-v2','x-web-extended-observation-v1'):
                        folder=self.settings.root/'media'/ident;folder.mkdir(exist_ok=True,mode=0o700)
                        shutil.copyfile(record['screenshot_file'],folder/'browser-source.png')
                    if is_new:
                        summary['new_jobs']+=1;created+=1
                    # An earlier observation, manual import or reviewed job is immutable.
                    elif self.store.get(ident)['payload']['post']['text']!=post['text']:
                        self.store.event('review.required',{'job_id':ident,'code':'WEB_SOURCE_TEXT_CHANGED'},dedup_key='web-edit:'+ident)
                codes=[r['error_code'] for r in response.get('errors',[])]
                if response.get('error_code'):codes.insert(0,response['error_code'])
                if codes:raise HubError(codes[0],retryable=codes[0] in {'X_WEB_NETWORK_TIMEOUT','X_WEB_RATE_LIMITED','X_WEB_UPSTREAM_UNAVAILABLE'},
                    review=not self.settings.config.get('machine_tweet_enabled',False) and codes[0] not in {'X_WEB_NETWORK_TIMEOUT','X_WEB_RATE_LIMITED','X_WEB_UPSTREAM_UNAVAILABLE'},retry_after=60)
            except HubError as exc:
                summary.update(state='blocked',error_code=exc.code)
                failures.append(exc)
            summaries.append(summary)
            now=time.time()
            with self.store.transaction() as db:
                db.execute('''INSERT INTO source_polls VALUES(?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET
                    checked_at=excluded.checked_at,last_success_at=COALESCE(excluded.last_success_at,source_polls.last_success_at),
                    error_code=excluded.error_code,metadata=excluded.metadata,next_check_at=excluded.next_check_at''',
                    ('x-web:'+handle,now,None if summary.get('error_code') else now,summary.get('error_code'),encode(summary),
                     now+self.settings.config['tweet_poll_minutes']*60))
        result={'source_mode':'x-web-dom-v1','sources':summaries,'new_jobs':created,'paid_x_api_calls':0,
                'human_review':False,'timeline_complete':False,'cursor_advanced':False}
        self.store.checkpoint(job,'x_web_observation',result)
        if self.settings.config.get('machine_tweet_enabled',False):
            from .machine_routes import machine_service
            result['expired_backlog_cleanup']=machine_service(self.settings,self.store).clear_backlog()
        if failures:
            # Both sources have been checked; one failure never suppresses the other.
            selected=next((e for e in failures if e.review or e.code=='AUTH_EXPIRED'),failures[0])
            raise selected
        return result
