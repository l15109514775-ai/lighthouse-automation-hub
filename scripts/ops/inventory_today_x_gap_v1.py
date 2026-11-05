"""Bounded Nov 2 official timeline inventory only; never imports or publishes.

No normal age/cursor/budget setting is changed. Raw API responses are saved by
the qualified source receipt/read-journal path with their actual capture times.
"""
from contextlib import contextmanager
from datetime import datetime,timezone
from pathlib import Path
import argparse,json,os,re,threading,time,uuid

from adapters.http import ProviderHTTP
from adapters.x import TWEET_FIELDS,MEDIA_FIELDS,USER_FIELDS,_author
from services.hub.budget import Budget
from services.hub.config import Settings
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.source_receipts import SourceReceipts
from services.hub.x_official_capture_queue_v1 import CapturedReadClient,_write,_read

VERSION='owner-today-x-gap-inventory-v1'
JOB_TYPE='x_today_gap_inventory_v1'
HANDLES=('example-channel-b','example_handle_a')
UID='1000000000000000'
START='2026-11-01T16:00:00Z'
START_AT=datetime.fromisoformat(START.replace('Z','+00:00')).timestamp()
END_AT=START_AT+86400
BLOCKED_AT=datetime.fromisoformat('2026-11-02T14:59:22+08:00').timestamp()
ALREADY_BACKFILLED={'2117142856553083135','2117143292328706377','2117149448958816687'}


def require(ok,code='TODAY_INVENTORY_SCOPE_INVALID'):
    if not ok:raise HubError(code,review=True)


def cursors(store):
    return store.query("SELECT key,value FROM source_cursors WHERE key LIKE 'x-api:%' OR key LIKE 'x:%' ORDER BY key")


def scope(settings):
    require(str(settings.config['target_bilibili_site_uid'])==UID and
        {s['x_handle'] for s in settings.config['sources']}==set(HANDLES))
    return {'version':VERSION,'handles':list(HANDLES),'target_site_uid':UID,'source_day':'2026-11-02 Asia/Shanghai',
        'start_time':START,'end_time_exclusive':END_AT,'max_pages_per_handle':2,'max_results':5,'max_detail_requests':0,
        'max_user_requests_per_handle':1,'publication_authorized':False,'human_review':False}


def historical_authors(settings,store):
    receipts=SourceReceipts(settings,store);result={}
    for handle in HANDLES:
        for row in store.query("SELECT call_id FROM source_http_receipts WHERE lower(path)=? ORDER BY captured_at DESC",('/2/users/by/username/'+handle,)):
            try:
                real=receipts._receipt(row['call_id']);body=receipts._body(real);author=_author(body.get('data'),handle)
                result[handle]=author['id'];break
            except HubError:continue
    return result


def saved_posts(settings,store,authors):
    receipts=SourceReceipts(settings,store);posts={};unreadable=[];safe={}
    for handle,author in authors.items():
        path='/2/users/'+author+'/tweets'
        for candidate in store.query('SELECT call_id FROM source_http_receipts WHERE path=? AND captured_at>=? ORDER BY captured_at',(path,START_AT-30)):
            try:
                row=receipts._receipt(candidate['call_id']);body=receipts._body(row)
                params=json.loads(row['params']);prior=params.get('since_id')
                if row['captured_at']<=BLOCKED_AT and isinstance(prior,str) and re.fullmatch('[0-9]{1,30}',prior):safe[handle]=prior
                for post in body.get('data',[]):
                    if post.get('author_id')!=author or not re.fullmatch('[0-9]{1,30}',post.get('id','')):continue
                    moment=datetime.fromisoformat(post['created_at'].replace('Z','+00:00'))
                    if moment.tzinfo is None:raise ValueError()
                    at=moment.timestamp()
                    if START_AT<=at<END_AT:
                        posts[post['id']]={'source_id':post['id'],'handle':handle,'original_post':post,'timeline_call_id':row['call_id'],
                            'captured_at':row['captured_at'],'transport_mode':row['transport_mode'],
                            'source_receipt':{'file':str(receipts._file(row)),'bytes':receipts._file(row).stat().st_size,'sha256':row['raw_sha256']}}
            except (HubError,ValueError,KeyError,TypeError) as exc:unreadable.append({'call_id':candidate['call_id'],'code':exc.code if isinstance(exc,HubError) else 'SAVED_RAW_SCHEMA_INVALID'})
    return posts,unreadable,safe


def classify(store,source_id):
    if store.query("SELECT 1 FROM source_tombstones WHERE source_platform='x' AND source_id=?",(source_id,)):return 'owner_cleared'
    publications=store.query("SELECT * FROM publications WHERE source_platform='x' AND source_id=? AND account_id=?",(source_id,'site_uid:'+UID))
    if any(p['state']=='completed' for p in publications):return 'published'
    if publications:return 'publication_exists_lookup_only'
    jobs=store.query("SELECT state FROM jobs WHERE source_platform='x' AND source_id=? AND job_type='tweet_process'",(source_id,))
    if jobs:return 'existing_job_reuse_required'
    if store.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='x_official_capture_queue_v1'"):
        if store.query('SELECT 1 FROM x_official_capture_queue_v1 WHERE source_id=? AND target_uid=?',(source_id,UID)):return 'capture_pending_or_held'
    return 'already_backfilled_hold' if source_id in ALREADY_BACKFILLED else 'missing'


def plan(settings,store):
    value=scope(settings);authors=historical_authors(settings,store);posts,unreadable,safe=saved_posts(settings,store,authors)
    statuses={ident:classify(store,ident) for ident in posts}
    return {**value,'mode':'plan_only','paid_requests':0,'job_rows_created':0,'normal_cursors':cursors(store),'historical_author_ids':authors,
        'safe_since_from_saved_receipts':safe,'saved_posts':list(posts.values()),'statuses':statuses,
        'missing_full_raw_ids':sorted(i for i,state in statuses.items() if state=='missing'),
        'unreadable_saved_receipts':unreadable,'complete':False,'coverage_note':'Saved receipts alone do not prove today gap coverage.',
        'proposed_bound_usd':{'timeline_four_requests':.100,'user_lookup_two_if_cache_missing':.020,'total':.120},
        'normal_age_policy_modified':False,'normal_age_exception_granted':False,'detail_requests':0,'public_submits':0}


class InventoryReceipts(SourceReceipts):
    def _parent(self,job):
        current=self.store.get(job['job_id'])
        require(current and current['job_type']==JOB_TYPE and current['queue']=='tweet' and current['state']=='processing' and
            current['source_platform']=='maintenance' and current['policy_version']==VERSION and current['payload']['version']==VERSION and
            current['payload']['run_id']==current['source_id'] and
            current['payload']==job['payload'] and current['payload']['scope']==scope(self.settings) and
            job.get('lease_owner') and current['lease_owner']==job['lease_owner'] and (current['lease_until'] or 0)>time.time(),
            'TODAY_INVENTORY_LEASE_REQUIRED')


@contextmanager
def lease(settings,store,job):
    stop=threading.Event();lost=threading.Event();receipts=InventoryReceipts(settings,store)
    def check():
        require(not lost.is_set(),'TODAY_INVENTORY_LEASE_LOST');receipts._parent(job)
    def pulse():
        while not stop.wait(min(30,settings.config['lease_seconds']/3)):
            if not store.heartbeat(job,settings.config['lease_seconds']):lost.set();return
            store.beat('worker:tweet')  # actual lease renewed while this operator owns tweet OS lock
    thread=threading.Thread(target=pulse,daemon=True);thread.start()
    try:check();yield check
    finally:stop.set();thread.join(timeout=2)


def apply(settings,store,*,run_id,http=None):
    require(str(uuid.UUID(run_id))==run_id)
    value=scope(settings);token=os.environ.get('X_BEARER_TOKEN');require(bool(token),'X_CREDENTIAL_REQUIRED')
    with resource_guard(settings.root/'data/locks/tweet'):
        old_cursors=cursors(store);owner=str(uuid.uuid4());now=time.time();payload={'version':VERSION,'run_id':run_id,'scope':value}
        with store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE source_platform=? AND source_id=? AND job_type=? AND policy_version=?',('maintenance',run_id,JOB_TYPE,VERSION)).fetchone()
            if row:
                require(json.loads(row['payload'])==payload and not row['external_publish_id'],'TODAY_INVENTORY_BINDING_CHANGED')
                if row['state']=='completed':
                    result=json.loads(row['result']);saved=_read(settings.root,result['descriptor'])
                    require(saved['capture_job_id']==row['job_id'] and saved['run_id']==run_id,'TODAY_INVENTORY_RESULT_CHANGED')
                    return result
                require(row['state']!='processing' or (row['lease_until'] or 0)<=now,'TODAY_INVENTORY_ACTIVE_LEASE')
                require(row['attempts']<4,'TODAY_INVENTORY_ATTEMPTS_EXHAUSTED');ident=row['job_id']
                db.execute("UPDATE jobs SET state='processing',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE job_id=?",
                    (owner,now+settings.config['lease_seconds'],now,ident))
            else:
                ident=str(uuid.uuid4())
                db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,attempts,lease_owner,lease_until,created_at,updated_at,policy_version,payload) VALUES(?,'maintenance',?,?,'tweet','processing',1,?,?,?,?,?,?)",
                    (ident,run_id,JOB_TYPE,owner,now+settings.config['lease_seconds'],now,now,VERSION,encode(payload)))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,'operator:today-x-gap-inventory','today_x_inventory_read_only',ident,encode(value)))
        job=store.get(ident);receipts=InventoryReceipts(settings,store)
        client=CapturedReadClient(settings,store,http or ProviderHTTP(Budget(store,settings.config['budgets'])),job,receipts)
        progress=job['result'].get('inventory_progress',{'handles':{},'normal_cursors_before':old_cursors})
        headers={'Authorization':'Bearer '+token};folder=settings.root/'media'/ident/VERSION
        def save():store.checkpoint(job,'today_inventory_read_only',{'inventory_progress':progress})
        try:
            with lease(settings,store,job) as check:
                blocked=False
                for handle in HANDLES:
                    cp=progress['handles'].setdefault(handle,{'page_call_ids':[],'next_token':None,'complete':False,'user_call_id':None})
                    if cp['complete']:continue
                    try:
                        check()
                        if cp['user_call_id']:
                            author=_author(receipts._body(receipts._receipt(cp['user_call_id']))['data'],handle)
                        else:
                            cache=receipts.cached_author(handle)
                            if cache:author=cache['author'];cp['user_call_id']=cache['user_call_id']
                            else:
                                body,snapshot=client.request_recorded('x','GET','https://api.x.com/2/users/by/username/'+handle,
                                    job_id=ident,headers=headers,params={'user.fields':USER_FIELDS},reserved_cost_usd=.010)
                                cp['user_call_id']=receipts.record(job,snapshot);author=_author(body['data'],handle)
                            cp['author_id']=author['id'];save()
                        while len(cp['page_call_ids'])<2:
                            check();params={'max_results':5,'tweet.fields':TWEET_FIELDS,'expansions':'attachments.media_keys',
                                'media.fields':MEDIA_FIELDS,'user.fields':USER_FIELDS,'start_time':START}
                            if cp['next_token']:params['pagination_token']=cp['next_token']
                            body,snapshot=client.request_recorded('x','GET','https://api.x.com/2/users/'+author['id']+'/tweets',
                                job_id=ident,headers=headers,params=params,reserved_cost_usd=.025)
                            call=receipts.record(job,snapshot)
                            require(call not in cp['page_call_ids'],'TODAY_INVENTORY_PAGINATION_LOOP')
                            cp['page_call_ids'].append(call);cp['next_token']=body.get('meta',{}).get('next_token')
                            require(cp['next_token'] is None or isinstance(cp['next_token'],str) and 0<len(cp['next_token'])<=2048,'X_PAGINATION_TOKEN_INVALID')
                            cp['complete']=cp['next_token'] is None;save()
                            if cp['complete']:break
                    except HubError as exc:
                        cp['stop_reason']=exc.code;save()
                        if exc.code in ('API_BUDGET_EXCEEDED','API_CALL_BUDGET_EXCEEDED','PAID_SERVICE_NOT_AUTHORIZED','AUTH_EXPIRED','INSUFFICIENT_BALANCE'):
                            blocked=True;break
                check();require(cursors(store)==old_cursors,'TODAY_INVENTORY_NORMAL_CURSOR_CHANGED')
                authors=historical_authors(settings,store);posts,unreadable,safe=saved_posts(settings,store,authors)
                current_calls={c for cp in progress['handles'].values() for c in cp['page_call_ids']}
                parsed=not any(v['call_id'] in current_calls for v in unreadable)
                complete=parsed and all(progress['handles'].get(h,{}).get('complete') for h in HANDLES)
                result={**value,'run_id':run_id,'capture_job_id':ident,'state':'complete' if complete else 'partial',
                    'complete':complete,'progress':progress,'budget_or_auth_stopped':blocked,
                    'normal_cursors_before':old_cursors,'normal_cursors_after':cursors(store),'normal_cursors_changed':False,
                    'posts':list(posts.values()),'statuses':{i:classify(store,i) for i in posts},
                    'missing_full_raw_ids':sorted(i for i in posts if classify(store,i)=='missing'),'unreadable_saved_receipts':unreadable,
                    'observed_at':datetime.now(timezone.utc).isoformat(),'detail_requests':0,'public_submits':0,'normal_age_policy_modified':False,'normal_age_exception_granted':False,
                    'new_receipt_transport_modes':sorted({r['transport_mode'] for r in store.query('SELECT transport_mode FROM source_http_receipts WHERE discovery_job_id=?',(ident,))})}
                reference=_write(settings.root,folder/'inventory.json',result)
                final={'version':VERSION,'state':result['state'],'complete':result['complete'],'capture_job_id':ident,'descriptor':reference,
                    'missing_full_raw_ids':result['missing_full_raw_ids'],'progress':progress,'detail_requests':0,'public_submits':0,'normal_cursors_changed':False,'human_review':False}
                store.finish(job,'completed',result=final,stage='today_inventory_complete' if result['complete'] else 'today_inventory_partial')
                return final
        except BaseException:
            current=store.get(ident)
            if current['state']=='processing' and current['lease_owner']==owner:store.finish(job,'upload_pending',code='TODAY_INVENTORY_RECONCILIATION_REQUIRED',result={'inventory_progress':progress},stage='today_inventory_hold')
            raise


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--run-id')
    args=parser.parse_args();settings=Settings.load();store=Store(settings.db_path)
    if args.apply:
        require(os.name=='posix' and os.getuid()==10001,'TODAY_INVENTORY_APP_UID_REQUIRED')
        require(args.run_id is not None,'TODAY_INVENTORY_RUN_ID_REQUIRED')
        value=apply(settings,store,run_id=args.run_id)
    else:
        value=plan(settings,store);value={k:v for k,v in value.items() if k!='saved_posts'}
    print(json.dumps(value,ensure_ascii=False))


if __name__=='__main__':main()
