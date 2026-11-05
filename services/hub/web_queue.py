"""Durable dispatch of individually approved artifacts, never machine approval."""
import json
import re
import time
import uuid
from .db import encode
from .errors import HubError,PublishUnknown
from .supervised_web import SupervisedWeb,KIND
from .web_publish import run_reviewed_tweet
from .outcomes import JobOutcome


class ReviewedWebQueue:
    def __init__(self,settings,store):
        self.settings,self.store=settings,store
        self.review=SupervisedWeb(settings,store)

    def queue(self,principal,parent_id,*,manifest_sha256,target_site_uid,authorization_reference):
        if principal!='maintenance':
            self.store.audit(principal,'queue_reviewed_website',parent_id,'denied')
            raise HubError('ACCESS_DENIED')
        if not self.settings.config.get('web_reviewed_queue_enabled',False):
            raise HubError('WEB_REVIEWED_QUEUE_DISABLED',review=True)
        try:
            valid_parent=isinstance(parent_id,str) and str(uuid.UUID(parent_id))==parent_id
        except (ValueError,TypeError,AttributeError):valid_parent=False
        if (not valid_parent or not isinstance(authorization_reference,str) or not authorization_reference.strip() or
                len(authorization_reference)>512 or not isinstance(manifest_sha256,str) or
                not re.fullmatch('[0-9a-f]{64}',manifest_sha256) or not isinstance(target_site_uid,str)):
            raise HubError('WEB_QUEUE_REQUEST_INVALID',review=True)
        parent,manifest=self.review._snapshot(parent_id)
        if (parent['result']['supervised_web']['manifest_sha256']!=manifest_sha256 or
                manifest['target_site_uid']!=target_site_uid):
            raise HubError('WEB_QUEUE_REVIEW_CHANGED',review=True)
        if parent['state'] not in ('awaiting_review','publish_unknown','completed'):
            raise HubError('WEB_QUEUE_PARENT_NOT_REVIEWABLE',review=True)
        request={'version':'reviewed-web-dispatch-v1','parent_job_id':parent_id,
                 'manifest_sha256':manifest_sha256,'target_site_uid':target_site_uid,
                 'authorization_reference':authorization_reference,
                 'basis':'existing_scoped_owner_review_and_explicit_maintenance_queue_request',
                 'grants_future_content_approval':False}
        now=time.time();ident=str(uuid.uuid4())
        with self.store.transaction() as db:
            row=db.execute("SELECT result,updated_at FROM jobs WHERE job_id=?",(parent_id,)).fetchone()
            if row['updated_at']!=parent['updated_at']:raise HubError('WEB_QUEUE_REVIEW_CHANGED',review=True)
            db.execute('''INSERT OR IGNORE INTO jobs(job_id,source_platform,source_id,job_type,queue,state,
                created_at,updated_at,policy_version,payload) VALUES(?,?,?,'tweet_web_publish','tweet','pending',?,?,?,?)''',
                (ident,'reviewed_web',parent_id,now,now,self.settings.config['policy_version'],encode(request)))
            row=db.execute("SELECT job_id,payload FROM jobs WHERE source_platform='reviewed_web' AND source_id=? AND job_type='tweet_web_publish' AND policy_version=?",
                (parent_id,self.settings.config['policy_version'])).fetchone()
            existing=json.loads(row['payload'])
            if any(existing.get(k)!=request[k] for k in ('version','parent_job_id','manifest_sha256','target_site_uid')):
                raise HubError('WEB_QUEUE_REQUEST_CONFLICT',review=True)
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,principal,'queue_reviewed_website',parent_id,'completed_parent_verification_only' if parent['state']=='completed' else 'one_scoped_artifact_queued'))
        return {'job_id':row['job_id'],'parent_job_id':parent_id,'state':'queued',
                'completed_parent_no_send':parent['state']=='completed','automatic_content_approval':False}

    def dispatch(self,job):
        if not self.settings.config.get('web_reviewed_queue_enabled',False):
            raise HubError('WEB_REVIEWED_QUEUE_DISABLED',review=True)
        request=job['payload']
        try:
            parent_id=request['parent_job_id']
            if (str(uuid.UUID(parent_id))!=parent_id or request['version']!='reviewed-web-dispatch-v1' or
                    job['source_platform']!='reviewed_web' or job['source_id']!=parent_id or
                    not isinstance(request.get('authorization_reference'),str) or not request['authorization_reference'].strip() or
                    len(request['authorization_reference'])>512 or
                    request.get('grants_future_content_approval') is not False or
                    request.get('basis')!='existing_scoped_owner_review_and_explicit_maintenance_queue_request'):
                raise ValueError()
            parent,manifest=self.review._snapshot(parent_id)
            if (request['manifest_sha256']!=parent['result']['supervised_web']['manifest_sha256'] or
                    request['target_site_uid']!=manifest['target_site_uid']):raise ValueError()
        except (KeyError,ValueError,TypeError,AttributeError):
            raise HubError('WEB_QUEUE_REQUEST_INVALID',review=True) from None
        result=run_reviewed_tweet(self.settings,self.store,parent_id)
        return JobOutcome('completed',{'parent_job_id':parent_id,**result},external_id=result.get('external_id'))

    def reconcile(self,job):
        if not self.settings.config.get('web_reviewed_queue_enabled',False):
            return {'disposition':'web_queue_disabled','publish_requests_sent':0}
        rows=self.store.query('''SELECT p.job_id,j.result FROM publications p JOIN jobs j ON j.job_id=p.job_id
            WHERE p.kind=? AND p.state IN ('dispatching','unknown') AND j.state='publish_unknown' ''',(KIND,))
        observations=[]
        for row in rows:
            parent=self.store.get(row['job_id']);history=parent['result'].get('web_reconcile',{})
            attempts=history.get('attempts',0)
            if type(attempts) is not int or attempts>=self.settings.config['max_attempts']:continue
            try:
                # An unresolved reservation makes this strictly lookup-only.
                value=run_reviewed_tweet(self.settings,self.store,row['job_id'])
                with self.store.transaction() as db:
                    children=db.execute("SELECT job_id,result,state,error_code FROM jobs WHERE source_platform='reviewed_web' AND source_id=? AND job_type='tweet_web_publish' AND state<>'processing'",
                        (row['job_id'],)).fetchall()
                    for child in children:
                        current=json.loads(child['result'])
                        current.update(parent_job_id=row['job_id'],reconciled_result=value,
                            previous_dispatch_state=child['state'],previous_dispatch_error=child['error_code'])
                        db.execute("UPDATE jobs SET state='completed',error_code=NULL,result=?,external_publish_id=?,stage='website_reconciled',updated_at=?,lease_owner=NULL,lease_until=NULL WHERE job_id=?",
                            (encode(current),value.get('external_id'),time.time(),child['job_id']))
                observations.append({'parent_job_id':row['job_id'],**value})
            except HubError as exc:
                if exc.code=='HEAVY_RESOURCE_BUSY':raise
                current=self.store.get(row['job_id']);result={**current['result'],
                    'web_reconcile':{'attempts':attempts+1,'last_error':exc.code,'checked_at':time.time(),
                                     'resend_authorized':False}}
                with self.store.transaction() as db:
                    db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=? AND state=\'publish_unknown\'',
                        (encode(result),time.time(),row['job_id']))
                if attempts+1>=self.settings.config['max_attempts']:
                    self.store.event('review.required',{'job_id':row['job_id'],'code':'WEB_RECONCILE_LIMIT_REACHED'},
                        dedup_key='web-reconcile-limit:'+row['job_id'])
                observations.append({'parent_job_id':row['job_id'],'state':'publish_unknown','error_code':exc.code,
                    'lookup_attempts':attempts+1,'resend_authorized':False})
        return {'observations':observations,'publish_requests_sent':0}
