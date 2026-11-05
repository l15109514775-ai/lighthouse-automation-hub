"""Durable parser failures and explicitly requested, zero-X cached recovery.

No transport exists here. The ordinary source age, receipt, policy, UID,
tombstone and publication gates remain in force. Original capture owners are
never resumed or impersonated; an ordinary Worker supplies the child's lease.
"""
from copy import deepcopy
import argparse,json,os,re,time,uuid
from adapters.x import (build_official_record,CONTENT_REJECTIONS,OFFICIAL_VERSION,
    OFFICIAL_RELATIONS_VERSION,parser_revision,_author)
from .db import encode
from .errors import HubError
from .source_receipts import SourceReceipts,digest
from .x_official_capture_queue_v1 import CapturedMediaReceipts

VERSION='x-official-cached-reparse-v1'
JOB_TYPE='x_official_cached_reparse_v1'

def fail(code):raise HubError(code)
def decoded(row):
    value=dict(row)
    for key in ('payload','result'):value[key]=json.loads(value[key])
    return value

class OfficialSourceReparse:
    def __init__(self,settings,store,*,downloader=None):self.settings,self.store,self.downloader=settings,store,downloader

    def _active(self,job_policy,source_version,target):
        from .machine_tweets_api import MachineTweetsAPI,MachineTweetsAPIReference
        cfg=self.settings.config;base='machine-tweet-v4-api-relations' if source_version==OFFICIAL_RELATIONS_VERSION else 'machine-tweet-v3-api'
        if (source_version not in (OFFICIAL_VERSION,OFFICIAL_RELATIONS_VERSION) or cfg.get('policy_version')!=base or
                str(cfg['target_bilibili_site_uid'])!=target or cfg.get('x_api_enabled') is not True or
                cfg.get('x_web_enabled') is not False or cfg.get('machine_tweet_enabled') is not True):fail('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')
        service=(MachineTweetsAPIReference if base.endswith('relations') else MachineTweetsAPI)(self.settings,self.store)
        policy=service.policy()
        if service.job_policy()!=job_policy:fail('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')
        return max(time.time()-policy['max_post_age_seconds'],policy['activation_at']-policy['max_post_age_seconds'])

    def _source(self,payload,*,fresh=False):
        r=payload['rejection'];refs=r['receipt_refs'];ids=[refs['user_call_id'],refs['timeline_call_id'],*refs['detail_call_ids']]
        if (set(refs)!={'user_call_id','timeline_call_id','detail_call_ids'} or len(refs['detail_call_ids'])>2 or
                len(ids)!=len(set(ids)) or any(str(uuid.UUID(i))!=i for i in ids)):fail('X_REPARSE_RECEIPTS_INVALID')
        receipts=SourceReceipts(self.settings,self.store);rows={i:receipts._receipt(i) for i in ids};bodies={i:receipts._body(row) for i,row in rows.items()}
        if any(row['method']!='GET' or row['host']!='api.x.com' for row in rows.values()):fail('X_REPARSE_RECEIPTS_INVALID')
        user=rows[refs['user_call_id']];page=rows[refs['timeline_call_id']];author=_author(bodies[user['call_id']].get('data'),r['handle'])
        if (r['handle'].lower() not in {s['x_handle'].lower() for s in self.settings.config['sources']} or
                user['path'].lower()!='/2/users/by/username/'+r['handle'].lower() or page['path']!='/2/users/'+author['id']+'/tweets' or
                page['discovery_job_id']!=r['discovery_job_id'] or any(rows[i]['discovery_job_id']!=r['discovery_job_id'] or
                    rows[i]['path']!='/2/tweets/'+str(bodies[i].get('data',{}).get('id')) for i in refs['detail_call_ids'])):fail('X_REPARSE_RECEIPTS_INVALID')
        posts=[p for p in bodies[page['call_id']].get('data',[]) if p.get('id')==r['post_id']]
        if len(posts)!=1 or digest(posts[0])!=r['original_post_sha256']:fail('X_REPARSE_POST_CHANGED')
        actual={i:{'raw_sha256':row['raw_sha256'],'canonical_sha256':row['canonical_sha256'],'captured_at':row['captured_at'],
            'file':str(receipts._file(row))} for i,row in rows.items()}
        if 'receipts' in payload and payload['receipts']!=actual:fail('X_REPARSE_RECEIPTS_CHANGED')
        if fresh:
            now=time.time();maximum=self.settings.config.get('source_receipt_max_age_seconds',86400)
            if any(not 0<=now-row['captured_at']<=maximum for row in rows.values()):fail('SOURCE_RECEIPT_EXPIRED_OR_FUTURE')
            not_before=self._active(payload['policy_version'],r['source_version'],payload['target_uid'])
            receipts._official_publication_window(None,{'post':posts[0]},not_before)
        return rows,bodies,posts[0],actual

    def register(self,discovery,rejection,policy):
        receipts=SourceReceipts(self.settings,self.store);receipts._parent(discovery)
        if (set(rejection)!={'version','discovery_job_id','handle','post_id','source_version','error_code','receipt_refs','original_post_sha256','parser_sha256','human_review'} or
                rejection['version']!='x-official-rejected-source-v1' or rejection['discovery_job_id']!=discovery['job_id'] or
                rejection['error_code'] not in CONTENT_REJECTIONS or rejection['parser_sha256']!=parser_revision() or
                rejection['human_review'] is not False or not re.fullmatch('[0-9]{1,30}',rejection['post_id'])):fail('X_REPARSE_REJECTION_INVALID')
        payload={'version':VERSION,'rejection':deepcopy(rejection),'target_uid':str(self.settings.config['target_bilibili_site_uid']),'policy_version':policy}
        self._active(policy,rejection['source_version'],payload['target_uid']);_,_,_,payload['receipts']=self._source(payload)
        with self.store.transaction() as db:
            receipts._official_parent_row(db,discovery)
            for row in db.execute("SELECT job_id,payload FROM jobs WHERE source_platform='x' AND source_id=? AND job_type=?",(rejection['post_id'],JOB_TYPE)):
                if json.loads(row['payload'])['target_uid']==payload['target_uid']:return {'job_id':row['job_id'],'created':False}
            ident=str(uuid.uuid4());now=time.time();result={'version':VERSION,'last_parser_sha256':rejection['parser_sha256'],
                'history':[{'parser_sha256':rejection['parser_sha256'],'code':rejection['error_code'],'at':now}],'human_review':False,'paid_x_api_calls':0}
            db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload,result,error_code,stage) VALUES(?,'x',?,?,'tweet','failed',?,?,?,?,?,?,'source_parse_blocked')",
                (ident,rejection['post_id'],JOB_TYPE,now,now,policy,encode(payload),encode(result),rejection['error_code']))
            self.store._event(db,'task.failed',{'job_id':ident,'queue':'tweet','source_platform':'x','source_id':rejection['post_id'],
                'code':rejection['error_code'],'stage':'source_parse_blocked','parser_sha256':rejection['parser_sha256']})
        return {'job_id':ident,'created':True}

    def context(self,job,*,db=None):
        current=(db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone() if db is not None else None)
        current=decoded(current) if current is not None else self.store.get(job['job_id'])
        if (not current or current['job_type']!=JOB_TYPE or current['source_platform']!='x' or current['queue']!='tweet' or current['state']!='processing' or
                not job.get('lease_owner') or current['lease_owner']!=job['lease_owner'] or (current['lease_until'] or 0)<=time.time() or
                current['attempts']!=job['attempts'] or current['payload']!=job['payload']):fail('X_REPARSE_CHILD_LEASE_REQUIRED')
        payload=current['payload'];r=payload['rejection']
        if payload['version']!=VERSION or current['source_id']!=r['post_id'] or current['policy_version']!=payload['policy_version']:fail('X_REPARSE_SCOPE_CHANGED')
        self._active(current['policy_version'],r['source_version'],payload['target_uid'])
        parent=self.store.get(r['discovery_job_id'])
        if not parent or parent['job_type']!='x_discovery' or parent['queue']!='tweet':fail('X_REPARSE_CAPTURE_OWNER_CHANGED')
        return {**payload,'discovery_job_id':r['discovery_job_id']}

    def retry_job(self,job_id,*,expected_parser_sha256):
        revision=parser_revision()
        if expected_parser_sha256!=revision:fail('X_REPARSE_EXPECTED_PARSER_CHANGED')
        job=self.store.get(job_id)
        if not job or job['job_type']!=JOB_TYPE or job['state']!='failed' or job['lease_owner'] or job['lease_until']:fail('X_REPARSE_FAILED_JOB_REQUIRED')
        if job['result']['last_parser_sha256']==revision:fail('X_REPARSE_PARSER_UNCHANGED')
        if job['attempts']>=self.settings.config['max_attempts']:fail('X_REPARSE_ATTEMPT_LIMIT')
        self._source(job['payload'],fresh=True)
        source=job['source_id']
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
            if decoded(row)!=job:fail('X_REPARSE_JOB_CHANGED')
            if db.execute("SELECT 1 FROM source_tombstones WHERE source_platform='x' AND source_id=?",(source,)).fetchone():fail('OWNER_BACKLOG_CLEARED')
            if db.execute("SELECT 1 FROM publications WHERE source_platform='x' AND source_id=?",(source,)).fetchone():fail('X_REPARSE_PUBLICATION_EXISTS_LOOKUP_ONLY')
            if db.execute("SELECT 1 FROM jobs WHERE source_platform='x' AND source_id=? AND job_type='tweet_process'",(source,)).fetchone():fail('X_REPARSE_SOURCE_ALREADY_BOUND')
            selected={**job['result'],'retry_parser_sha256':revision,'retry_requested_at':time.time()}
            db.execute("UPDATE jobs SET state='pending',next_run=0,error_code=NULL,result=?,stage='source_cached_reparse_pending',updated_at=? WHERE job_id=?",(encode(selected),time.time(),job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(time.time(),'maintenance','retry_cached_x_source',job_id,
                encode({'previous_parser_sha256':job['result']['last_parser_sha256'],'parser_sha256':revision,'attempts_preserved':job['attempts'],'paid_x_api_calls':0})))
        return {'job_id':job_id,'state':'pending','attempts':job['attempts'],'paid_x_api_calls':0}

    def process(self,job):
        descriptor=self.context(job);payload=job['payload'];r=payload['rejection'];revision=parser_revision()
        if job['result'].get('retry_parser_sha256')!=revision:fail('X_REPARSE_PARSER_UNCHANGED')
        rows,bodies,_,_=self._source(payload,fresh=True)
        history=[*job['result']['history'],{'parser_sha256':revision,'at':time.time(),'code':'cached_reparse_started'}]
        self.store.checkpoint(job,'source_cached_reparse',{**job['result'],'last_parser_sha256':revision,'history':history})
        refs=r['receipt_refs'];metadata={i:{'captured_at':row['captured_at'],'transport_mode':row['transport_mode']} for i,row in rows.items()}
        try:
            record=build_official_record(r['handle'],r['post_id'],bodies[refs['user_call_id']],bodies[refs['timeline_call_id']],
                {i:bodies[i] for i in refs['detail_call_ids']},refs,metadata,source_version=r['source_version'])
        except HubError as exc:
            if exc.code not in CONTENT_REJECTIONS:raise
            self.store.checkpoint(job,'source_parse_blocked',{**job['result'],'history':[*history,{'parser_sha256':revision,'at':time.time(),'code':exc.code}]})
            raise HubError(exc.code) from None
        receipts=CapturedMediaReceipts(self.settings,self.store,self,job)
        result=receipts.ingest_official(job,record,job['policy_version'],downloader=self.downloader,
            not_before=self._active(job['policy_version'],r['source_version'],payload['target_uid']))
        return {**job['result'],'ingestion':result,'original_discovery_job_id':descriptor['discovery_job_id'],'paid_x_api_calls':0,'human_review':False}

def main():
    from .config import Settings
    from .db import Store
    assert os.name=='posix' and os.getuid()==os.getgid()==10001 and not __import__('sys').flags.optimize
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--retry-job',required=True);p.add_argument('--expected-parser-sha256',required=True);a=p.parse_args()
    settings=Settings.load();print(encode(OfficialSourceReparse(settings,Store(settings.db_path)).retry_job(a.retry_job,expected_parser_sha256=a.expected_parser_sha256)))
if __name__=='__main__':main()
