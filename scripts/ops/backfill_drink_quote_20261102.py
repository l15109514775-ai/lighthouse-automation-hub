"""One-time exact November 2 drink-campaign quote backfill from existing API receipts.

Reuses the qualified machine renderer and native publication coordinator. It
never calls X, changes the ordinary age rule, reissues an unknown send, resets
attempts or asserts human review. The root grant binds the owner's actual order.
"""
import argparse
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import uuid

from adapters.x import build_official_record
from adapters.http import ProviderHTTP
from adapters.model import ModelAPI
from services.hub.budget import Budget
from services.hub.config import Settings
from services.hub.db import Store, encode
from services.hub.errors import HubError, PublishUnknown
from services.hub.source_receipts import SourceReceipts, digest

VERSION='owner-cached-drink-quote-v1'
OWNER_QUOTE='先补上今天漏发的推特，然后修改流程中重复计费的问题。最后将回放自动上传跑通'
TARGETS=('2117215807201788251',)
HANDLE='example_handle_a'
AUTHOR='1762798427501740032'
UID='1000000000000000'
CAPTURE='2605df1c-77ec-4441-b4e9-a314afc52817'
PAGE='ae8391b9-be8e-4ad8-a014-355b70ce37e0'
USER='805d32e6-474f-427a-b0b3-1eab987c487e'
DETAILS={'2117134613633999283':'ed1fee7a-c1f2-47f3-bec3-ba88879c4be8'}
ENGINE_SHA='b2b3db799afbdb6efb41ec55476c938c2d6395eee43b783a73d7ca4ab1787fee'
START=datetime.fromisoformat('2026-11-02T00:00:00+08:00').timestamp()
END=START+86400

def require(condition,code):
    if not condition: raise HubError(code)

def strict_json(raw):
    def pairs(items):
        result={}
        for k,v in items:
            require(k not in result,'BACKFILL_DUPLICATE_JSON');result[k]=v
        return result
    return json.loads(raw,object_pairs_hook=pairs)

def write_new(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    require(not path.exists() and not any(p.is_symlink() for p in (path,*path.parents)),'BACKFILL_OUTPUT_EXISTS_OR_SYMLINK')
    raw=(encode(value)+'\n').encode()
    with path.open('xb') as f:os.fchmod(f.fileno(),0o600);f.write(raw);f.flush();os.fsync(f.fileno())
    fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)
    return {'file':str(path),'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}

def load_engine(path):
    path=Path(path)
    require(path.is_absolute() and not any(p.is_symlink() for p in (path,*path.parents)),'BACKFILL_ENGINE_PATH')
    require(hashlib.sha256(path.read_bytes()).hexdigest()==ENGINE_SHA,'BACKFILL_ENGINE_CHANGED')
    spec=importlib.util.spec_from_file_location('cached_backfill_engine',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def capture_records(settings,store):
    receipts=SourceReceipts(settings,store);ids=[USER,PAGE,*DETAILS.values()]
    rows={i:receipts._receipt(i) for i in ids};bodies={i:receipts._body(r) for i,r in rows.items()}
    require(all(r['transport_mode']=='default_tls' for r in rows.values()),'BACKFILL_REAL_TLS_REQUIRED')
    require(all(rows[i]['discovery_job_id']==CAPTURE for i in [PAGE,*DETAILS.values()]),'BACKFILL_CAPTURE_OWNER_CHANGED')
    metadata={i:{'captured_at':r['captured_at'],'transport_mode':r['transport_mode']} for i,r in rows.items()}
    values={}
    for ident in TARGETS:
        posts=[p for p in bodies[PAGE]['data'] if p['id']==ident];require(len(posts)==1,'BACKFILL_POST_MISSING')
        p=posts[0];created=datetime.fromisoformat(p['created_at'].replace('Z','+00:00')).timestamp()
        require(p['author_id']==AUTHOR and START<=created<END,'BACKFILL_WRONG_AUTHOR_OR_DAY')
        refs=[DETAILS[r['id']] for r in p.get('referenced_tweets',[])]
        ref={'user_call_id':USER,'timeline_call_id':PAGE,'detail_call_ids':refs}
        values[ident]=build_official_record(HANDLE,ident,bodies[USER],bodies[PAGE],
            {i:bodies[i] for i in refs},ref,metadata,source_version='x-official-source-v2')
    descriptor={'capture_job_id':CAPTURE,'source_ids':list(TARGETS),
        'original_posts':{i:deepcopy(values[i]['payload']['post']) for i in TARGETS},
        'record_sha256':{i:digest(values[i]) for i in TARGETS},
        'receipts':{i:{'raw_sha256':r['raw_sha256'],'canonical_sha256':r['canonical_sha256'],'captured_at':r['captured_at']} for i,r in rows.items()}}
    return values,descriptor

def plan(settings,store):
    _,descriptor=capture_records(settings,store)
    config=Path(os.environ.get('HUB_CONFIG',settings.root/'config/hub.toml'))
    require(settings.config['policy_version']=='machine-tweet-v4-api-relations' and settings.config['target_bilibili_site_uid']==UID,'BACKFILL_POLICY_SCOPE')
    return {'version':VERSION,'capture':descriptor,'target_site_uid':UID,'config_file':str(config),
        'config_sha256':hashlib.sha256(config.read_bytes()).hexdigest(),
        'policy_sha256':hashlib.sha256((settings.root/'config/machine-tweet-policy-api-relations-v1.json').read_bytes()).hexdigest(),
        'engine_sha256':ENGINE_SHA,'source_ids':list(TARGETS),'owner_quote':OWNER_QUOTE,
        'scope':['cached_source_import','translate_independent_check_render','publish_once_per_source','today_only_age_exception'],
        'new_x_calls':0,'human_review':False,
        'existing_publications':store.query("SELECT source_id,job_id,state,external_url FROM publications WHERE source_platform='x' AND source_id IN (?)",TARGETS),
        'existing_jobs':store.query("SELECT source_id,job_id,state FROM jobs WHERE source_platform='x' AND job_type='tweet_process' AND source_id IN (?)",TARGETS),
        'tombstones':store.query("SELECT source_id,reason FROM source_tombstones WHERE source_platform='x' AND source_id IN (?)",TARGETS)}

def validate_grant(value,expected,*,now,require_live=True):
    require(set(value)=={'plan','grant_id','issued_at','expires_at','owner_response_reference'},'BACKFILL_GRANT_KEYS')
    require(value['plan']==expected,'BACKFILL_PLAN_CHANGED')
    require(str(uuid.UUID(value['grant_id']))==value['grant_id'],'BACKFILL_GRANT_ID')
    require(isinstance(value['owner_response_reference'],str) and 1<=len(value['owner_response_reference'])<=2000,'BACKFILL_OWNER_REFERENCE')
    require(type(value['issued_at']) in (int,float) and type(value['expires_at']) in (int,float) and
        0<value['expires_at']-value['issued_at']<=7200 and value['issued_at']<=now+5,'BACKFILL_GRANT_TIME')
    if require_live:require(value['issued_at']-5<=now<value['expires_at'],'BACKFILL_GRANT_EXPIRED')

class Scope:
    def __init__(self,settings,store,engine,grant_file,grant_sha):
        self.live,self.store,self.engine=settings,store,engine
        self.file=Path(grant_file);self.sha=grant_sha
        require(self.file==settings.root/'config/owner-cached-drink-quote-v1/grant.json','BACKFILL_GRANT_PATH')
        self.raw=self.file.read_bytes();require(hashlib.sha256(self.raw).hexdigest()==grant_sha,'BACKFILL_GRANT_HASH')
        self.value=strict_json(self.raw);self.expected=self.value['plan']
        self.grant=SimpleNamespace(ref={'file':str(self.file),'sha256':grant_sha,'bytes':len(self.raw)})
        self.descriptor_ref=self.grant.ref;self.capture_job_id=CAPTURE
        self.support=SimpleNamespace(TARGETS=TARGETS,SITE_UID=UID,write_new=write_new,strict_json=strict_json)
        self.settings=Settings(settings.root,deepcopy(settings.config),settings.dry_run)
        self.settings.config[engine.AVATAR_CONFIG_KEY]=deepcopy(engine.AVATAR_CONFIG)
        self.queue_depth=0;self.session_job=None;self.worker_context_verified=False
        self.records,descriptor=capture_records(settings,store)
        require(descriptor==self.expected['capture'],'BACKFILL_CAPTURE_CHANGED')
        original=plan(settings,store)
        for field in ('existing_jobs','existing_publications','tombstones'):original[field]=self.expected[field]
        require(original==self.expected,'BACKFILL_BASE_PLAN_CHANGED')
        self.check()

    def check(self,*,require_live=True):
        for p in (self.file,*self.file.parents):
            require(not p.is_symlink() and p.stat().st_uid==0 and p.stat().st_mode&0o022==0,'BACKFILL_GRANT_NOT_PROTECTED')
        require(self.file.read_bytes()==self.raw,'BACKFILL_GRANT_CHANGED')
        validate_grant(self.value,self.expected,now=time.time(),require_live=require_live)
        require(hashlib.sha256(Path(self.expected['config_file']).read_bytes()).hexdigest()==self.expected['config_sha256'],'BACKFILL_CONFIG_CHANGED')
        require(hashlib.sha256((self.settings.root/'config/machine-tweet-policy-api-relations-v1.json').read_bytes()).hexdigest()==self.expected['policy_sha256'],'BACKFILL_POLICY_CHANGED')
        require(not self.store.query("SELECT 1 FROM source_tombstones WHERE source_platform='x' AND source_id IN (?)",TARGETS),'BACKFILL_SOURCE_CLEARED')
        if self.session_job:
            current=self.store.get(self.session_job['job_id'])
            require(current['state']=='processing' and current['lease_owner']==self.session_job['lease_owner'] and current['lease_until']>time.time(),'BACKFILL_SESSION_LEASE_LOST')
        return self.value,self.grant.ref

    def payload(self,payload,*,require_live=True):
        self.check(require_live=require_live);ident=payload['post']['id']
        require(ident in TARGETS and payload['post']==self.expected['capture']['original_posts'][ident] and
            payload['author']['id']==AUTHOR and payload['author']['username']==HANDLE and
            payload['url']=='https://x.com/'+HANDLE+'/status/'+ident and payload['source_api']=='x-v2-official' and
            payload['official_source']['version']=='x-official-source-v2','BACKFILL_SOURCE_SCOPE_CHANGED')
        return ident

    def job_policy(self):
        generation=self.settings.config.get('machine_source_generation',1)
        require(type(generation) is int and 1<=generation<=1000,'BACKFILL_GENERATION')
        return self.engine.POLICY if generation==1 else self.engine.POLICY+':source-'+str(generation)

@contextmanager
def discovery_lease(scope):
    """Resume real completed capture only for cached import; restore its original result and state."""
    store=scope.store;old=store.get(CAPTURE);now=time.time();owner=str(uuid.uuid4());duration=scope.settings.config['lease_seconds']
    require(old['job_type']=='x_discovery' and old['state']=='completed' and old['error_code'] is None and
        old['lease_owner'] is None and old['lease_until'] is None and old['attempts']<scope.settings.config['max_attempts'],'BACKFILL_CAPTURE_NOT_IDLE')
    with store.transaction() as db:
        require(db.execute("UPDATE jobs SET state='processing',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE job_id=? AND state='completed' AND updated_at=? AND lease_owner IS NULL",(owner,now+duration,now,CAPTURE,old['updated_at'])).rowcount==1,'BACKFILL_CAPTURE_CAS')
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,VERSION,'resume_cached_source_import',CAPTURE,encode({'grant':scope.grant.ref,'old_job':old,'new_x_calls':0,'human_review':False})))
    job=store.get(CAPTURE);stop=threading.Event()
    def heartbeat():
        while not stop.wait(min(30,duration/3)):
            if not store.heartbeat(job,duration):return
    thread=threading.Thread(target=heartbeat,daemon=True);thread.start()
    try:yield job
    finally:
        current=store.get(CAPTURE)
        if current['state']=='processing' and current['lease_owner']==owner:
            store.finish(job,old['state'],code=old['error_code'],result=current['result'],stage=old['stage'])
        stop.set();thread.join(timeout=2)

def ingest(scope):
    store=scope.store;engine=scope.engine
    ids=dict(store.get(CAPTURE)['result'].get('oneoff_imported_parent_ids',{}))
    with discovery_lease(scope) as job:
        receipts=engine.AgeScopedReceipts(scope)
        for ident in TARGETS:
            scope.payload(scope.records[ident]['payload'])
            result=engine.bounded_local_resource_wait(scope,lambda:receipts.ingest_official(job,scope.records[ident],scope.job_policy()))
            parent=store.get(result['job_id']);scope.payload(parent['payload'])
            require(not result.get('publication_state') and parent['state']=='upload_pending','BACKFILL_EXISTING_SOURCE_REQUIRES_RECONCILIATION')
            marker={'version':VERSION,'grant':scope.grant.ref,'descriptor':scope.descriptor_ref,'capture_job_id':CAPTURE,'original_created_at':parent['payload']['post']['created_at'],'age_exception_only':True,'human_review':False}
            with store.transaction() as db:
                row=db.execute('SELECT result,state,lease_owner FROM jobs WHERE job_id=?',(parent['job_id'],)).fetchone()
                require(row['state']=='upload_pending' and row['lease_owner'] is None,'BACKFILL_PARENT_BUSY')
                previous=strict_json(row['result']);prior=previous.get('owner_reference_backfill')
                require(prior is None or prior==marker,'BACKFILL_FOREIGN_GRANT')
                db.execute("UPDATE jobs SET result=?,updated_at=?,stage='owner_cached_today_imported' WHERE job_id=?",(encode({**previous,'owner_reference_backfill':marker}),time.time(),parent['job_id']))
            ids[ident]=parent['job_id'];store.checkpoint(job,'owner_cached_today_imported',{**job['result'],'oneoff_imported_parent_ids':ids})
    return ids

def prepare(scope,ident):
    engine=scope.engine;store=scope.store;service=engine.machine(scope);job=engine.parent_job(scope,ident)
    if job['result'].get('machine_web'):
        _,manifest=service._snapshot(job['job_id']);return manifest
    with engine.held_claim(scope,job['job_id'],expected_type='tweet_process',allowed_states=('upload_pending','failed','processing')) as claimed:
        model=ModelAPI(ProviderHTTP(Budget(store,scope.settings.config['budgets'])),store,settings=scope.settings)
        outcome=engine.bounded_local_resource_wait(scope,lambda:service.process(claimed,model))
        require(outcome.state=='upload_pending','BACKFILL_RENDER_NOT_HELD')
        store.finish(claimed,'upload_pending',result=outcome.result,stage='owner_cached_today_prepared')
    _,manifest=service._snapshot(job['job_id']);return manifest

def run(scope,cid,hostname):
    engine=scope.engine;store=scope.store;engine.validate_worker_context(scope,cid,hostname)
    with engine.tweet_session(scope):
        value,_=scope.check();payload={'grant':scope.grant.ref,'descriptor':scope.descriptor_ref,'scope_version':VERSION,'source_ids':list(TARGETS),'human_review':False}
        ident=store.enqueue('owner_backfill','cached-today:'+value['grant_id'],'owner_reference_backfill_session','tweet',payload,engine.VERSION)
        existing=store.get(ident);require(existing['payload']==payload,'BACKFILL_SESSION_CHANGED')
        if existing['state']=='completed':return existing['result']
        with engine.held_claim(scope,ident,expected_type='owner_reference_backfill_session',allowed_states=('pending','failed','processing','publish_unknown')) as session:
            scope.session_job=session
            try:
                ids=store.get(CAPTURE)['result'].get('oneoff_imported_parent_ids',{})
                if not all(i in ids for i in TARGETS):ingest(scope)
                prepared={};published={}
                for source in TARGETS:
                    prepared[source]=prepare(scope,source)
                    print(encode({'state':'prepared','source_id':source,'render':prepared[source]['render'],'caption':prepared[source]['caption']}),flush=True)
                store.checkpoint(session,'cached_today_prepared',{**session['result'],'prepared':{s:{'job_id':m['job_id'],'png':m['render']['png'],'sha256':m['render']['sha256']} for s,m in prepared.items()}})
                for source in TARGETS:
                    try:published[source]=engine.dispatch(scope,source)
                    except PublishUnknown:published[source]={'state':'publish_unknown','next_action':'lookup_only'}
                    print(encode({'state':'source_result','source_id':source,'result':published[source]}),flush=True)
                result={'version':VERSION,'grant':scope.grant.ref,'publications':published,'new_x_calls':0,'human_review':False}
                unknown=any(v.get('state')=='publish_unknown' for v in published.values())
                store.finish(session,'publish_unknown' if unknown else 'completed',result=result,code='PUBLISH_UNKNOWN' if unknown else None,stage='cached_today_backfill_result')
                return result
            finally:scope.session_job=None

def main():
    p=argparse.ArgumentParser();p.add_argument('--mode',choices=('plan','run'),default='plan');p.add_argument('--engine-file',required=True)
    p.add_argument('--grant-file');p.add_argument('--grant-sha256');p.add_argument('--container-id');p.add_argument('--hostname');a=p.parse_args()
    settings=Settings.load();store=Store(settings.db_path);engine=load_engine(a.engine_file)
    if a.mode=='plan':print(encode(plan(settings,store)));return
    scope=Scope(settings,store,engine,a.grant_file,a.grant_sha256)
    print(encode(run(scope,a.container_id,a.hostname)),flush=True)

if __name__=='__main__':
    try:main()
    except HubError as exc:print(encode({'version':VERSION,'error_code':exc.code,'automatic_resubmit':False}),flush=True);raise SystemExit(1)
