"""Bounded official API discovery; the retained browser route is never a fallback."""
import time
from adapters.x import XOfficial,OFFICIAL_VERSION
from .db import encode
from .errors import HubError
from .machine_tweets_api import MachineTweetsAPI
from .source_receipts import SourceReceipts
from .x_official_capture_queue_v1 import OfficialCaptureQueue,CapturedReadClient
from .x_official_reparse_v1 import OfficialSourceReparse

# These are deterministic failures of one public asset, never authorization to
# alter it, retry a send, or skip a receipt/lease/system failure. Everything not
# explicitly listed still aborts this source without advancing its cursor.
PERMANENT_ASSET_ERRORS=frozenset({
    'X_MEDIA_DECLARED_DIMENSIONS_MISMATCH','X_MEDIA_ANIMATED_UNSUPPORTED',
    'X_MEDIA_ORIENTATION_UNSUPPORTED','X_MEDIA_SIZE_INVALID','X_MEDIA_DECODE_FAILED',
    'ASSET_HTTP_REJECTED','ASSET_SIZE_LIMIT',
})
SKIPPABLE_SOURCE_ERRORS=frozenset({'OWNER_TWO_HOUR_WINDOW_EXPIRED','OWNER_BACKLOG_CLEARED'})


class XAPIDiscovery:
    def __init__(self,settings,store,http):
        self.settings,self.store=settings,store
        self.x=XOfficial(http)
        self.http=http
        self.receipts=SourceReceipts(settings,store)
        self.capture_queue=OfficialCaptureQueue(settings,store)
        self.reparse=OfficialSourceReparse(settings,store)

    def run(self,job):
        if not self.settings.config.get('x_api_enabled') or self.settings.config.get('x_web_enabled'):
            raise HubError('X_OFFICIAL_TRANSPORT_REQUIRED')
        machine=MachineTweetsAPI(self.settings,self.store)
        if self.settings.config.get('policy_version')=='machine-tweet-v4-api-relations':
            from .machine_tweets_api import MachineTweetsAPIReference
            machine=MachineTweetsAPIReference(self.settings,self.store)
        policy=machine.policy();job_policy=machine.job_policy()
        self.capture_queue.initialize()
        self.x.client=CapturedReadClient(self.settings,self.store,self.http,job,self.receipts)
        progress=job['result'].get('x_official_capture_progress_v1',{})
        if progress and progress.get('job_policy')!=job_policy:raise HubError('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')
        progress={**progress,'job_policy':job_policy,'handles':dict(progress.get('handles',{}))}
        summaries=[];failures=[];created=0
        for configured in self.settings.config['sources']:
            handle=configured['x_handle']
            summary={'handle':handle,'source_authority':'x-v2-official','new_jobs':0,
                     'human_review':False,'browser_fallback':False,'catchup_complete':False}
            try:
                previous=progress['handles'].get(handle,{})
                if previous.get('complete') is True:
                    summaries.append({**previous['summary'],'capture_reused':True})
                    continue
                # A completed earlier policy/publication and a cleared source are
                # global exclusions. Partial responses never advance the cursor.
                known=self.store.query("""SELECT source_id FROM jobs WHERE source_platform='x'
                    AND job_type='tweet_process' AND (external_publish_id IS NOT NULL OR
                    ((policy_version=? OR ?=1) AND EXISTS(SELECT 1 FROM tweet_source_bindings b WHERE b.job_id=jobs.job_id)))
                    UNION SELECT source_id FROM source_tombstones WHERE source_platform='x'
                    UNION SELECT source_id FROM publications WHERE platform='bilibili' AND source_platform='x'
                    AND state<>'rejected' LIMIT 10000""",(job_policy,int(machine.version=='machine-tweet-v4-api-relations')))
                key='x-api:'+handle
                cursor=self.store.query('SELECT value FROM source_cursors WHERE key=?',(key,))
                version=getattr(machine,'source_record_version',OFFICIAL_VERSION)
                source_options={'source_version':version} if version!=OFFICIAL_VERSION else {}
                self.x.client.last_captured_at=None;self.x.client.fresh_calls=0
                admitted={};admission_skips=[];rejected_jobs={}
                def reject(rejection):
                    value=self.reparse.register(job,rejection,job_policy)
                    rejected_jobs[rejection['post_id']]=value['job_id'];return value
                def admit(record):
                    nonlocal created
                    post_id=record['payload']['post']['id']
                    if post_id in admitted:return admitted[post_id]
                    try:value=self.capture_queue.register(job,record,job_policy)
                    except HubError as exc:
                        if exc.code not in SKIPPABLE_SOURCE_ERRORS:raise
                        admission_skips.append({'post_id':post_id,'reason':exc.code});value={'created':False,'skipped':True}
                    admitted[post_id]=value
                    if value['created']:created+=1;summary['new_jobs']+=1
                    return value
                response=self.x.latest_official(handle,job_id=job['job_id'],
                    recorder=lambda item:self.receipts.record(job,item),
                    not_before=max(time.time()-policy['max_post_age_seconds'],policy['activation_at']-policy['max_post_age_seconds']),
                    since_id=cursor[0]['value'] if cursor else None,
                    known_ids=list(set([row['source_id'] for row in known]+self.capture_queue.known_ids())),author_cache=self.receipts.cached_author(handle),record_consumer=admit,
                    rejection_consumer=reject,
                    **({'initial_pagination_token':previous['continuation_token']} if previous.get('continuation_token') else {}),**source_options)
                summary.update(posts_observed=len(response['observed_post_ids']),
                    skipped_posts=list(response['skipped_posts'])+admission_skips,rejected_posts=list(response['rejected_posts']),
                    catchup_complete=response['catchup_complete'],parse_blocked_count=len(response['rejected_posts']),
                    parse_blocked_job_ids=list(dict.fromkeys(rejected_jobs.values())))
                if self.x.client.last_captured_at is not None:
                    summary.update(source_captured_at=self.x.client.last_captured_at,capture_reused=self.x.client.fresh_calls==0)
                for record in response['records']:
                    try:
                        admit(record)
                    except HubError as exc:
                        post_id=record['payload']['post']['id']
                        if exc.code in SKIPPABLE_SOURCE_ERRORS:
                            summary['skipped_posts'].append({'post_id':post_id,'reason':exc.code})
                            continue
                        if exc.code not in PERMANENT_ASSET_ERRORS:raise
                        summary['rejected_posts'].append({'post_id':post_id,'error_code':exc.code})
                        self.store.event('task.failed',{'job_id':job['job_id'],'queue':'tweet','source_platform':'x','source_id':post_id,
                            'code':exc.code,'policy_version':machine.version},dedup_key='x-api-rejected:'+post_id)
                        continue
                summary['skipped_posts']=list(response['skipped_posts'])+admission_skips
                # Unsupported content is explicitly recorded and not retried as
                # a blind publication. Each source still gets its own poll.
                for rejected in response['rejected_posts']:
                    if rejected['post_id'] in rejected_jobs:continue  # its real failed child already emitted the event
                    self.store.event('task.failed',{'job_id':job['job_id'],'queue':'tweet','source_platform':'x','source_id':rejected['post_id'],
                        'code':rejected['error_code'],'policy_version':machine.version},
                        dedup_key='x-api-rejected:'+rejected['post_id'])
                observed=list(dict.fromkeys(previous.get('observed_post_ids',[])+response['observed_post_ids']))
                progress['handles'][handle]={'complete':False,'observed_post_ids':observed,'continuation_token':response.get('continuation_token')}
                self.store.checkpoint(job,'x_official_captured_before_media',{**job['result'],'x_official_capture_progress_v1':progress})
                if not response['catchup_complete']:
                    raise HubError('X_CATCHUP_BOUND_REACHED',retryable=True,retry_after=60)
                if observed:
                    last=str(max(int(value) for value in observed))
                    with self.store.transaction() as db:
                        db.execute('INSERT OR REPLACE INTO source_cursors VALUES(?,?)',(key,last))
                summary['state']='observed'
                progress['handles'][handle].update(complete=True,summary=summary)
                self.store.checkpoint(job,'x_official_captured_before_media',{**job['result'],'x_official_capture_progress_v1':progress})
            except HubError as exc:
                summary.update(state='blocked',error_code=exc.code);failures.append(exc)
            summaries.append(summary)
            now=time.time()
            with self.store.transaction() as db:
                db.execute('''INSERT INTO source_polls VALUES(?,?,?,?,?,?) ON CONFLICT(source_key) DO UPDATE SET
                    checked_at=excluded.checked_at,last_success_at=COALESCE(excluded.last_success_at,source_polls.last_success_at),
                    error_code=excluded.error_code,metadata=excluded.metadata,next_check_at=excluded.next_check_at''',
                    ('x-api:'+handle,now,None if summary.get('error_code') else summary.get('source_captured_at',now),summary.get('error_code'),encode(summary),
                     now+self.settings.config['tweet_poll_minutes']*60))
        result={'source_mode':'x-v2-official','sources':summaries,'new_jobs':created,
                'human_review':False,'browser_fallback':False,'expired_backlog_cleanup':machine.clear_backlog(),
                'x_official_capture_progress_v1':progress,'media_ingest_deferred':True}
        self.store.checkpoint(job,'x_official_observation',result)
        if failures:
            selected=next((e for e in failures if e.code in ('AUTH_EXPIRED','INSUFFICIENT_BALANCE','PERMISSION_DENIED')),failures[0])
            # Machine authority does not ask an operator to fake a source check.
            raise HubError(selected.code,retryable=selected.retryable,retry_after=selected.retry_after)
        return result
