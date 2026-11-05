import json
import os
import time
from adapters.bilibili import BilibiliOfficial
from .errors import HubError, PublishUnknown
from .outcomes import JobOutcome
from .publishing import Publishing
from .db import encode


class BilibiliService:
    def __init__(self,settings,store,http):
        self.settings,self.store=settings,store
        self.adapter=BilibiliOfficial(settings,store,http)
        self.publisher=Publishing(settings,store,self.adapter)

    def preflight(self):
        try:
            identity=self.adapter.account_identity()
            return {'official_video_implementation':True,'account_verified':True,'open_id':identity,
                    'publish_requests_sent':0,'dry_run':self.settings.dry_run}
        except HubError as exc:
            return {'official_video_implementation':True,'account_verified':False,'error_code':exc.code,
                    'publish_requests_sent':0,'dry_run':self.settings.dry_run}

    def publish_reviewed_video(self,job,grant):
        if self.settings.dry_run or self.settings.config.get('bilibili_publish_mode')!='official_video':
            raise HubError('VIDEO_REVIEW_AND_BILIBILI_AUTHORIZATION_REQUIRED',review=True)
        if ('publish' not in grant.get('actions',[]) or not grant.get('evidence') or
                not grant.get('authorized_by')):
            raise HubError('VIDEO_PUBLICATION_RIGHTS_REQUIRED',review=True)
        if (job['result'].get('source_sha256')!=grant.get('source_sha256') or
                job['result'].get('rights_evidence')!=grant.get('evidence')):
            raise HubError('MEDIA_RIGHTS_OR_SOURCE_CHANGED',review=True)
        metadata=grant.get('bilibili')
        if not isinstance(metadata,dict):
            raise HubError('BILIBILI_SUBMISSION_METADATA_REQUIRED',review=True)
        artifact={**job['result']['processed'],'bilibili':metadata}
        result=self.publisher.submit(job,artifact,os.getenv('BILIBILI_ACCOUNT_ID',''),'video')
        return JobOutcome(result['state'],{**job['result'],'publication':result},result.get('external_id'),
                          result.get('error_code'))

    def reconcile(self,job):
        pending=self.store.query('''SELECT p.* FROM publications p JOIN jobs j ON j.job_id=p.job_id
            WHERE p.platform='bilibili' AND p.kind='video' AND (p.state IN ('dispatching','unknown','awaiting_moderation','upload_pending')
                OR j.state='publish_unknown') ORDER BY p.updated_at LIMIT 20''')
        counts={}
        for row in pending:
            parent=self.store.get(row['job_id'])
            if parent['state']=='processing' and (parent.get('lease_until') or 0)>time.time():
                continue
            try:
                result=self.publisher.reconcile(row['idempotency_key'])
                state=result['state']
                code=result.get('error_code')
            except PublishUnknown:
                state,code='publish_unknown','RECONCILE_BEFORE_RETRY'
                result={'state':state}
            except HubError as exc:
                state='auth_expired' if exc.code=='AUTH_EXPIRED' else 'awaiting_review'
                code=exc.code
                result={'state':state,'error_code':code}
            with self.store.transaction() as db:
                changed=db.execute('''UPDATE jobs SET state=?,error_code=?,result=?,external_publish_id=COALESCE(?,external_publish_id),
                    updated_at=? WHERE job_id=? AND (state!='processing' OR lease_until<?)''',
                    (state,code,encode({**parent['result'],'publication':result}),result.get('external_id'),
                     time.time(),parent['job_id'],time.time())).rowcount
                if changed:
                    db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                        (time.time(),'scheduler','reconcile_publication',row['idempotency_key'],state))
            if state in ('auth_expired','awaiting_review','publish_unknown','failed'):
                event='auth.expired' if state=='auth_expired' else 'task.failed' if state=='failed' else 'review.required'
                self.store.event(event,{'job_id':parent['job_id'],'code':code},
                    dedup_key='bilibili-reconcile:'+parent['job_id'])
            counts[state]=counts.get(state,0)+1
        return {'publications_checked':sum(counts.values()),'states':counts,'publish_requests_sent':0}
