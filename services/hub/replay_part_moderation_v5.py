"""Finite independent V5 per-part lookup jobs. No upload, reserve, submit or root grant.

Pending moderation and an unresolved send both retain the original parent
attempt and publication key. A missing submit intent is never permission to
send again. Actual platform-attempt counting starts after profile/heavy locks.
"""
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit,urljoin,parse_qsl
import hashlib,json,re,time,uuid

from .db import encode
from .errors import HubError
from .locks import resource_guard
from .replay_parts_v5 import ReplayPartVideoWeb,ReplayPartGroupsV5,POLICY,PART_JOB_TYPES,STATE,KIND,index_from_job,document,reference_body
from .replay_moderation_common import assert_lookup_lease,day_bounds
from .youtube_download import digest
from .worker import Worker

LOOKUP_JOB_TYPE='replay_part_lookup_v5'
LOOKUP_PRINCIPAL='worker:replay-part-lookup-v5'
VERSION='replay-part-moderation-v5'
PAYLOAD_VERSION='replay-part-read-only-lookup-v5'
TABLE='replay_part_lookup_schedule_v5'
ATTEMPTS='replay_part_lookup_attempts_v5'

def fail(code):raise HubError(code,review=True)
def limits(settings):
    result={k:settings.config.get('part_lookup_'+k,v) for k,v in
        {'fast_seconds':300,'slow_seconds':1800,'max_hours':48,'daily_polls':96,'daily_attempts':384,'total_polls':192}.items()}
    ranges={'fast_seconds':(60,600),'slow_seconds':(300,3600),'max_hours':(1,48),'daily_polls':(1,1000),
        'daily_attempts':(1,4000),'total_polls':(1,1000)}
    if any(type(result[k]) is not int or not low<=result[k]<=high for k,(low,high) in ranges.items()):fail('REPLAY_PART_LOOKUP_POLICY_INVALID')
    return result

def initialize(store):
    with store.transaction() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS replay_part_lookup_schedule_v5(parent_job_id TEXT PRIMARY KEY,
            binding_sha256 TEXT NOT NULL,intent_sha256 TEXT,first_seen REAL NOT NULL,deadline REAL NOT NULL,
            next_check REAL, polls_total INTEGER NOT NULL,active_child_id TEXT,status TEXT NOT NULL,record TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS replay_part_lookup_attempts_v5(id TEXT PRIMARY KEY,
            child_job_id TEXT NOT NULL,lease_owner TEXT NOT NULL,parent_job_id TEXT NOT NULL,started_at REAL NOT NULL,
            state TEXT NOT NULL,code TEXT,UNIQUE(child_job_id,lease_owner))''')

def parent_context(settings,store,service,parent_id):
    parent,binding,state=service.snapshot(parent_id,write=False)
    try:valid_attempt=str(uuid.UUID(state.get('attempt_id','')))==state.get('attempt_id')
    except (ValueError,TypeError,AttributeError):valid_attempt=False
    if (parent['job_type']!=service.JOB_TYPE or index_from_job(parent)!=service.part_index or
            type(binding.get('part_index')) is not int or binding['part_index']!=service.part_index or
            type(binding.get('part_count')) is not int or not service.part_index<binding['part_count']<=32 or
            parent['policy_version']!=POLICY or parent['source_platform']!='youtube' or parent['queue']!='video' or
            parent['state'] not in ('awaiting_moderation','publish_unknown') or parent['lease_owner'] is not None or parent['lease_until'] is not None or
            state.get('state') not in ('awaiting_moderation','submit_unknown','submitting') or state.get('public_submission_approved') is not False or
            binding.get('human_review') is not False or binding['site_uid']!=str(settings.config['target_bilibili_site_uid']) or
            binding['source_id']!=parent['source_id'] or binding['source_url']!='https://www.youtube.com/watch?v='+parent['source_id'] or
            digest(binding['metadata'])!=binding['metadata_sha256'] or not valid_attempt):fail('REPLAY_PART_LOOKUP_PARENT_SCOPE_REQUIRED')
    rows=store.query("SELECT * FROM publications WHERE job_id=? AND idempotency_key=?",(parent_id,state['key']))
    if len(rows)!=1:fail('REPLAY_PART_LOOKUP_PUBLICATION_REQUIRED')
    pub=rows[0];known=parent['external_publish_id'];previous=state.get('result_observation')
    if (pub['job_id']!=parent_id or pub['kind']!=service.KIND or pub['idempotency_key']!=state['key'] or
            pub['source_platform']!='youtube' or pub['source_id']!=parent['source_id'] or
            pub['account_id']!='site_uid:'+binding['site_uid'] or pub['platform']!='bilibili' or
            pub['state'] not in ('dispatching','unknown','awaiting_moderation')):fail('REPLAY_PART_LOOKUP_PUBLICATION_REQUIRED')
    if known is not None:
        if (not re.fullmatch('BV[A-Za-z0-9]{10}',known) or pub['external_id']!=known or pub['external_url']!='https://www.bilibili.com/video/'+known or
                not previous or previous.get('external_id')!=known or previous.get('state')!='awaiting_moderation' or state['state']!='awaiting_moderation' or
                previous.get('version')!=service.RESULT_OBSERVATION_VERSION or previous.get('attempt_id')!=state['attempt_id'] or
                previous.get('site_uid')!=binding['site_uid'] or previous.get('source_url')!=binding['source_url'] or
                previous.get('metadata_sha256')!=binding['metadata_sha256']):
            fail('REPLAY_PART_LOOKUP_KNOWN_BV_CHANGED')
    elif (pub['external_id'] is not None or pub['external_url'] is not None or previous or parent['state']!='publish_unknown' or
            state['state'] not in ('submit_unknown','submitting') or pub['state'] not in ('dispatching','unknown')):fail('REPLAY_PART_LOOKUP_UNKNOWN_BINDING_REQUIRED')
    file=settings.root/'media'/parent_id/'replay-native-intents/public-submit.json'
    try:intent,ref=document(settings.root,file,maximum=1_000_000)
    except (HubError,OSError):fail('REPLAY_PART_LOOKUP_SUBMIT_INTENT_REQUIRED')
    base=service._native_base(parent,binding,state)
    if (intent.get('version')!='replay-part-one-use-intent-v5' or intent.get('kind')!='public-submit' or
            any(intent.get(k)!=v or type(intent.get(k)) is not type(v) for k,v in base.items()) or
            intent.get('reservation',{}).get('idempotency_key')!=state['key']):fail('REPLAY_PART_LOOKUP_SUBMIT_INTENT_REQUIRED')
    try:created=datetime.fromisoformat(intent['created_at'].replace('Z','+00:00'))
    except (ValueError,KeyError,AttributeError):fail('REPLAY_PART_LOOKUP_SUBMIT_INTENT_REQUIRED')
    if created.tzinfo is None or created.timestamp()>time.time()+30:fail('REPLAY_PART_LOOKUP_SUBMIT_INTENT_REQUIRED')
    return parent,binding,state,pub,ref,created.timestamp()

def child_payload(context,number):
    parent,binding,state,pub,ref,_=context
    return {'version':PAYLOAD_VERSION,'parent_publish_job_id':parent['job_id'],'part_index':binding['part_index'],
        'part_job_type':parent['job_type'],'group_parent_job_id':binding['parent_job_id'],'parent_binding_sha256':digest(binding),
        'attempt_id':state['attempt_id'],'site_uid':binding['site_uid'],'source_id':parent['source_id'],
        'source_url':binding['source_url'],'metadata_sha256':binding['metadata_sha256'],'publication_key':state['key'],
        'external_id':parent['external_publish_id'],'submit_intent':ref,'poll_number':number,
        'human_review':False,'publication_authorized':False}

def assert_child(store,job):return assert_lookup_lease(store,job,job_type=LOOKUP_JOB_TYPE,policy=POLICY)

def lookup_context(settings,store,service,child,*,now=None):
    assert_child(store,child);p=child['payload'];context=parent_context(settings,store,service,p.get('parent_publish_job_id'))
    rows=store.query('SELECT * FROM '+TABLE+' WHERE parent_job_id=?',(p.get('parent_publish_job_id'),))
    if (len(rows)!=1 or rows[0]['active_child_id']!=child['job_id'] or rows[0]['status']!='waiting' or
            (time.time() if now is None else now)>=rows[0]['deadline'] or p!=child_payload(context,rows[0]['polls_total'])):
        fail('REPLAY_PART_LOOKUP_CHILD_PARENT_CHANGED')
    return context,rows[0]

class LookupService(ReplayPartVideoWeb):
    def __init__(self,*a,**k):super().__init__(*a,**k);self._lookup_job=None
    def _operator(self,principal):
        if principal==LOOKUP_PRINCIPAL and self._lookup_job is not None:
            lookup_context(self.settings,self.store,self,self._lookup_job);return
        return super()._operator(principal)
    def assert_worker_lease(self):
        if self._lookup_job is not None:return assert_child(self.store,self._lookup_job)
        return super().assert_worker_lease()
    def _checkpoint_matches(self,current,job):
        if self._lookup_job is not None:
            context,_=lookup_context(self.settings,self.store,self,self._lookup_job)
            if context[0]['job_id']!=job['job_id']:return False
        return super()._checkpoint_matches(current,job)
    def record_lookup(self,child,observation,*,result_reference):
        context,_=lookup_context(self.settings,self.store,self,child)
        if self._lookup_job is not None:fail('REPLAY_PART_LOOKUP_CONTEXT_ALREADY_BOUND')
        self._lookup_job=child
        try:return self.record_result(LOOKUP_PRINCIPAL,context[0]['job_id'],observation,lookup=True,result_reference=result_reference)
        finally:self._lookup_job=None

    def record_publication_candidate(self,child,reason):
        """Persist a manager-published BV even when only player verification failed."""
        context,_=lookup_context(self.settings,self.store,self,child)
        parent,binding,state,pub,_,_=context
        folder=self.settings.root/'media'/parent['job_id']/'replay-browser-part-v5'/state['attempt_id']/'later-lookups-v2'
        files=sorted(folder.glob('*/publication-observed.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        if not files:
            return None
        path=files[0]
        if path.is_symlink() or path.resolve()!=path:fail('REPLAY_PART_PUBLICATION_EVIDENCE_PATH')
        try:body=json.loads(path.read_text('utf-8'))
        except (OSError,ValueError):fail('REPLAY_PART_PUBLICATION_EVIDENCE_INVALID')
        bv=body.get('external_id') if isinstance(body,dict) else None
        if (body.get('version')!='replay-part-publication-observed-v5' or not isinstance(bv,str) or
                not re.fullmatch('BV[A-Za-z0-9]{10}',bv)):
            fail('REPLAY_PART_PUBLICATION_EVIDENCE_INVALID')
        self._lookup_job=child
        try:
            now=time.time();child_job=self.store.get(parent['job_id'])
            with self.store.transaction() as db:
                current=db.execute('SELECT state,external_publish_id FROM jobs WHERE job_id=?',(parent['job_id'],)).fetchone()
                if not current or current['external_publish_id'] not in (None,bv):fail('REPLAY_PART_PUBLICATION_CANDIDATE_CHANGED')
                db.execute("UPDATE publications SET state='awaiting_moderation',external_id=?,external_url=?,updated_at=? "
                           "WHERE job_id=? AND idempotency_key=? AND state IN ('unknown','dispatching') AND external_id IS NULL",
                           (bv,'https://www.bilibili.com/video/'+bv,now,parent['job_id'],state['key']))
                db.execute("UPDATE replay_part_children_v5 SET state='awaiting_moderation',external_id=? "
                           "WHERE child_job_id=? AND idempotency_key=? AND state IN ('unknown','dispatching')",
                           (bv,parent['job_id'],state['key']))
                result=child_job['result'] or {}
                observation={'version':'replay-part-publication-observation-v5','external_id':bv,
                             'external_url':'https://www.bilibili.com/video/'+bv,'reason':reason,
                             'complete_part_playability_verified':False,
                             'evidence':{'file':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}}
                result['publication_observation']=observation
                db.execute('UPDATE jobs SET external_publish_id=?,stage=?,error_code=?,result=?,updated_at=? '
                           'WHERE job_id=? AND state=?',
                           (bv,'replay_part_platform_published_completeness_unverified',reason,
                            encode(result),now,parent['job_id'],parent['state']))
                group=self.store.get(binding['parent_job_id'])
                group_result=group['result'] or {}
                group_result['publication_observation']=observation
                db.execute('UPDATE jobs SET state=?,stage=?,error_code=?,result=?,updated_at=? WHERE job_id=? AND state=?',
                           ('awaiting_review','replay_parts_platform_published_completeness_pending',
                            'REPLAY_PART_PUBLIC_VERIFICATION_PENDING',encode(group_result),now,
                            binding['parent_job_id'],group['state']))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                           (now,LOOKUP_PRINCIPAL,'replay_part_publication_candidate',parent['job_id'],
                            encode({'external_id':bv,'reason':reason,'evidence':str(path)})))
            return observation
        finally:
            self._lookup_job=None

    def record_draft_lookup(self,child,observation,*,result_reference):
        """Save a source-bound native draft location; never complete or resend."""
        context,_=lookup_context(self.settings,self.store,self,child)
        parent,binding,state,pub,_,_=context
        if parent['external_publish_id'] is not None or self._lookup_job is not None:fail('REPLAY_PART_DRAFT_CONTEXT_REQUIRED')
        base=self._native_base(parent,binding,state)
        def check():return lookup_context(self.settings,self.store,self,child)
        def require(value):
            if not value:fail('REPLAY_PART_DRAFT_EVIDENCE_REQUIRED')
        def link(value):
            u=urlsplit(urljoin('https://member.bilibili.com',value));query=parse_qsl(u.query,keep_blank_values=True)
            require(u.scheme=='https' and u.netloc=='member.bilibili.com' and u.path=='/platform/upload/video/frame' and not u.fragment and
                sorted(k for k,v in query)==['draftId','type'] and dict(query)['type']=='draft' and re.fullmatch('[1-9][0-9]{0,17}',dict(query)['draftId']))
            return dict(query)['draftId'],u.geturl()
        folder=self.settings.root/'media'/parent['job_id']/'replay-browser-part-v5'/state['attempt_id']/'later-lookups-v2'
        require(isinstance(result_reference,dict) and Path(result_reference.get('file','')).name=='draft-observation.json')
        output=Path(result_reference['file']).parent
        require(output.parent==folder and re.fullmatch('[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',output.name))
        def raw(ref,name):
            value=reference_body(self.settings.root,ref,expected=output/name,check=check)
            require(all(value.get(k)==v and type(value.get(k)) is type(v) for k,v in base.items()))
            require(all(type(value.get(k)) is int and value[k]==0 for k in ('file_selections','save_clicks','submit_clicks','player_seeks')) and value.get('source_or_media_modified') is False)
            return value
        saved=raw(result_reference,'draft-observation.json');require(saved==observation)
        require(saved.get('version')=='replay-part-native-draft-observation-v5' and saved.get('state')=='draft_found' and saved.get('read_only') is True and
            all(saved.get(k) is False for k in ('public_accessible','server_file_hash_verified','human_review','publication_authorized')) and
            saved.get('navigation_actions')=={'draft_tab_clicks':1,'draft_editor_navigations':1})
        observed=datetime.fromisoformat(saved['observed_at'].replace('Z','+00:00'))
        require(observed.tzinfo is not None and -30<=time.time()-observed.timestamp()<=300)
        manager=raw(saved['manager_observation'],'manager-row.json')
        require(manager.get('selected_row') is None and manager.get('known_external_id') is None and manager['visible_dom']['truncated'] is False and
            type(manager['visible_dom'].get('declared_all_count')) is int and manager['visible_dom']['declared_all_count']==len(manager['visible_dom']['rows']) and
            not any(a['text']==binding['metadata']['title'] for row in manager['visible_dom']['rows'] for a in row['title_links']))
        drafts=raw(saved['draft_manager_observation'],'draft-manager.json');editor=raw(saved['draft_editor_observation'],'draft-editor.json')
        for value,version in ((drafts,'replay-part-native-draft-manager-v5'),(editor,'replay-part-native-draft-editor-v5')):
            require(value.get('version')==version and value.get('read_only') is True)
            identity=value['account_observation'];u=urlsplit(urljoin('https://t.bilibili.com/',identity['profile_href']))
            require(identity['site_uid']==binding['site_uid'] and identity['url']=='https://t.bilibili.com/' and
                u.scheme=='https' and u.netloc=='space.bilibili.com' and u.path in ('/'+binding['site_uid'],'/'+binding['site_uid']+'/') and not u.query and not u.fragment)
            reference_body(self.settings.root,value['screenshot'],expected=output/('draft-manager.png' if value is drafts else 'draft-editor.png'),maximum=5_000_000,check=check,json_body=False)
        dom=drafts['visible_dom'];count=drafts['declared_draft_count']
        require(type(count) is int and 1<=count<=60 and dom['section_count']==1 and dom['truncated'] is False and len(dom['rows'])==count and
            len(dom['pagination'])==1 and re.fullmatch(r'\s*共\s*1\s*页\s*/\s*'+str(count)+r'\s*个\s*[，,]?\s*',dom['pagination'][0]) and dom['selected_pages']==['1'])
        matches=[];ids=set()
        for row in dom['rows']:
            require(len(row['titles'])==len(row['covers'])==len(row['saved_times'])==1 and len(row['titles'][0]['links'])==len(row['covers'][0]['alts'])==1)
            title=row['titles'][0];a=title['links'][0];identity=link(a['href'])
            require(identity==link(row['covers'][0]['href']) and title['title']==a['text']==row['covers'][0]['alts'][0] and identity[0] not in ids)
            ids.add(identity[0])
            if a['text']==binding['metadata']['title']:matches.append((identity,row['saved_times'][0]))
        require(len(matches)==1 and matches[0]==(link(saved['draft_url']),saved['saved_at_text']) and matches[0][0][0]==saved['draft_id'])
        require(link(editor['visible_dom']['url'])==matches[0][0] and editor['visible_dom']['title']==binding['metadata']['title'] and editor['visible_dom']['source_url']==binding['source_url'])
        check();self._lookup_job=child
        try:
            return self._update(LOOKUP_PRINCIPAL,parent,{**state,'draft_observation':saved,'draft_observation_reference':result_reference},
                job_state=parent['state'],stage='replay_part_native_draft_observed',code=parent['error_code'])
        finally:self._lookup_job=None

def schedule_tick(settings,store,service=None,*,now=None):
    now=time.time() if now is None else now;cfg=limits(settings);initialize(store)
    start,end=day_bounds(now);out={'version':VERSION,'scheduled':[],'stalled':[],'blocked':[],'external_calls':0,'submissions':0,'parent_attempts_reset':False}
    with resource_guard(settings.root/'data/locks/part-lookup-schedule-v5'):
        placeholders=','.join('?' for _ in PART_JOB_TYPES)
        ids=store.query("SELECT job_id FROM jobs WHERE job_type IN ("+placeholders+") AND policy_version=? AND source_platform='youtube' AND state IN ('awaiting_moderation','publish_unknown') ORDER BY created_at,job_id",(*PART_JOB_TYPES,POLICY))
        for identity in ids:
            parent=store.get(identity['job_id']);rows=store.query('SELECT * FROM '+TABLE+' WHERE parent_job_id=?',(parent['job_id'],));old=rows[0] if rows else None
            try:
                selected=service or LookupService(settings,store,part_index=index_from_job(parent))
                context=parent_context(settings,store,selected,parent['job_id']);code=None
            except HubError as exc:context=None;code=exc.code
            with store.transaction() as db:
                current=db.execute('SELECT * FROM jobs WHERE job_id=?',(parent['job_id'],)).fetchone()
                if not current or current['payload']!=encode(parent['payload']) or current['state']!=parent['state'] or current['updated_at']!=parent['updated_at']:continue
                row=db.execute('SELECT * FROM '+TABLE+' WHERE parent_job_id=?',(parent['job_id'],)).fetchone()
                if old and (not row or dict(row)!=old):continue
                if row is None:
                    first=min(now,context[5] if context else parent['updated_at']);deadline=first+cfg['max_hours']*3600
                    db.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,0,NULL,?,?)',(parent['job_id'],digest(parent['payload']),context[4]['sha256'] if context else None,
                        first,deadline,now,'waiting' if context else 'blocked',encode({'last_code':code})))
                    row=db.execute('SELECT * FROM '+TABLE+' WHERE parent_job_id=?',(parent['job_id'],)).fetchone()
                row=dict(row)
                active=db.execute('SELECT state FROM jobs WHERE job_id=?',(row['active_child_id'],)).fetchone() if row['active_child_id'] else None
                running=bool(active and active['state'] in ('pending','processing','retry_scheduled'))
                if now>=row['deadline'] or row['polls_total']>=cfg['total_polls'] and not running:
                    if row['status']!='stalled':
                        db.execute('UPDATE '+TABLE+" SET status='stalled',next_check=NULL,record=? WHERE parent_job_id=?",(encode({'last_code':'REPLAY_PART_LOOKUP_TIME_OR_POLL_LIMIT'}),parent['job_id']))
                        store._event(db,'pipeline.stalled',{'job_id':parent['job_id'],'code':'REPLAY_PART_LOOKUP_TIME_OR_POLL_LIMIT'})
                        store._event(db,'task.failed',{'job_id':parent['job_id'],'code':'REPLAY_PART_LOOKUP_TIME_OR_POLL_LIMIT','lookup_only':True})
                    out['stalled'].append(parent['job_id']);continue
                if code:
                    if old is None or row['status']!='blocked' or json.loads(row['record']).get('last_code')!=code:
                        store._event(db,'review.required',{'job_id':parent['job_id'],'code':code,'lookup_only':True})
                    db.execute('UPDATE '+TABLE+" SET status='blocked',record=?,next_check=? WHERE parent_job_id=?",(encode({'last_code':code}),now+cfg['fast_seconds'],parent['job_id']))
                    out['blocked'].append({'job_id':parent['job_id'],'code':code});continue
                if row['binding_sha256']!=digest(context[1]) or row['intent_sha256'] not in (None,context[4]['sha256']):
                    db.execute('UPDATE '+TABLE+" SET status='blocked',record=? WHERE parent_job_id=?",(encode({'last_code':'REPLAY_PART_LOOKUP_BINDING_CHANGED'}),parent['job_id']));continue
                if row['active_child_id']:
                    child=db.execute('SELECT * FROM jobs WHERE job_id=?',(row['active_child_id'],)).fetchone()
                    if child and child['state'] in ('pending','processing','retry_scheduled'):continue
                    if child and child['state'] in ('auth_expired','awaiting_review'):
                        db.execute('UPDATE '+TABLE+" SET status='blocked',record=? WHERE parent_job_id=?",(encode({'last_code':child['error_code']}),parent['job_id']));continue
                if row['next_check'] is None or row['next_check']>now:continue
                if db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']:continue
                daily=db.execute("SELECT COUNT(*) FROM jobs WHERE job_type=? AND policy_version=? AND created_at>=? AND created_at<?",(LOOKUP_JOB_TYPE,POLICY,start,end)).fetchone()[0]
                spent=db.execute('SELECT COUNT(*) FROM '+ATTEMPTS+' WHERE started_at>=? AND started_at<?',(start,end)).fetchone()[0]
                if daily>=cfg['daily_polls'] or spent>=cfg['daily_attempts']:
                    db.execute('UPDATE '+TABLE+' SET next_check=? WHERE parent_job_id=?',(min(end,row['deadline']),parent['job_id']));continue
                ident=str(uuid.uuid4());number=row['polls_total']+1;payload=child_payload(context,number)
                db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload) VALUES(?,'bilibili',?,?,'video','pending',?,?,?,?)",
                    (ident,parent['job_id']+':lookup:'+str(number),LOOKUP_JOB_TYPE,now,now,POLICY,encode(payload)))
                interval=cfg['fast_seconds'] if now-row['first_seen']<3600 else cfg['slow_seconds']
                db.execute('UPDATE '+TABLE+" SET intent_sha256=?,polls_total=?,active_child_id=?,status='waiting',next_check=?,record=? WHERE parent_job_id=?",
                    (context[4]['sha256'],number,ident,now+interval,encode({'last_code':None}),parent['job_id']))
                out['scheduled'].append(ident);break
    return out

class QueueView:
    def __init__(self,store):self.store=store
    def __getattr__(self,name):return getattr(self.store,name)
    def beat(self,name):self.store.beat('worker:video-part-moderation-v5')
    def claim(self,queue,lease_seconds,max_attempts):
        if queue!='moderation':raise ValueError('REPLAY_PART_LOOKUP_QUEUE_SCOPE')
        now=time.time();owner=str(uuid.uuid4())
        with self.store.transaction() as db:
            if db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']:return None
            row=db.execute("SELECT job_id FROM jobs WHERE queue='video' AND source_platform='bilibili' AND job_type=? AND policy_version=? AND state IN ('pending','retry_scheduled') AND next_run<=? AND attempts<? ORDER BY created_at,job_id LIMIT 1",(LOOKUP_JOB_TYPE,POLICY,now,max_attempts)).fetchone()
            if not row:return None
            db.execute("UPDATE jobs SET state='processing',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE job_id=?",(owner,now+lease_seconds,now,row['job_id']))
        return self.store.get(row['job_id'])
    def recover(self,queue,max_attempts,*,all_processing=False):
        if queue!='moderation':raise ValueError('REPLAY_PART_LOOKUP_QUEUE_SCOPE')
        now=time.time()
        with self.store.transaction() as db:
            rows=db.execute("SELECT * FROM jobs WHERE queue='video' AND source_platform='bilibili' AND job_type=? AND policy_version=? AND state='processing' AND (? OR lease_until<?)",(LOOKUP_JOB_TYPE,POLICY,all_processing,now)).fetchall()
            for row in rows:
                state='failed' if row['attempts']>=max_attempts else 'retry_scheduled'
                db.execute('UPDATE jobs SET state=?,error_code=?,lease_owner=NULL,lease_until=NULL,next_run=?,updated_at=? WHERE job_id=?',(state,'REPLAY_PART_LOOKUP_INTERRUPTED',now,now,row['job_id']))
                if state=='failed':self.store._event(db,'task.failed',{'job_id':row['job_id'],'code':'REPLAY_PART_LOOKUP_RETRY_EXHAUSTED','lookup_only':True})
        return len(rows)

class LookupWorker(Worker):
    def _defer_resource_busy(self,job):
        assert_child(self.store,job);now=time.time()
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            launched=db.execute('SELECT 1 FROM '+ATTEMPTS+' WHERE child_job_id=? AND lease_owner=?',(job['job_id'],job['lease_owner'])).fetchone()
            if not row or row['lease_owner']!=job['lease_owner'] or row['state']!='processing':return False
            used=row['attempts'] if launched else row['attempts']-1
            state='failed' if used>=self.settings.config['max_attempts'] else 'retry_scheduled'
            db.execute('UPDATE jobs SET state=?,attempts=?,error_code=?,lease_owner=NULL,lease_until=NULL,next_run=?,updated_at=? WHERE job_id=?',(state,used,'REPLAY_PART_LOOKUP_RESOURCE_BUSY',now+30,now,job['job_id']))
            if state=='failed':self.store._event(db,'task.failed',{'job_id':job['job_id'],'code':'REPLAY_PART_LOOKUP_RETRY_EXHAUSTED','lookup_only':True})
        return True

class LookupHandler:
    def __init__(self,settings,store,*,service=None,browser=None,manifest_builder=None):
        self.settings,self.store=settings,store;self.service=service
        self.browser,self.manifest_builder=browser,manifest_builder
    def process(self,child):
        index=child['payload'].get('part_index')
        if type(index) is not int or not 0<=index<32:fail('REPLAY_PART_LOOKUP_ORDINAL_REQUIRED')
        service=self.service or LookupService(self.settings,self.store,part_index=index)
        context,row=lookup_context(self.settings,self.store,service,child);cfg=limits(self.settings)
        def check():return lookup_context(self.settings,self.store,service,child)
        def before_launch():
            check();now=time.time();start,end=day_bounds(now)
            with self.store.transaction() as db:
                current=db.execute('SELECT state,lease_owner,lease_until,payload FROM jobs WHERE job_id=?',(child['job_id'],)).fetchone()
                if (not current or current['state']!='processing' or current['lease_owner']!=child['lease_owner'] or
                        (current['lease_until'] or 0)<=now or current['payload']!=encode(child['payload'])):raise RuntimeError('LEASE_LOST')
                count=db.execute('SELECT COUNT(*) FROM '+ATTEMPTS+' WHERE started_at>=? AND started_at<?',(start,end)).fetchone()[0]
                if count>=cfg['daily_attempts']:raise HubError('REPLAY_PART_LOOKUP_DAILY_ATTEMPT_LIMIT',retryable=True,retry_after=end-now)
                db.execute('INSERT INTO '+ATTEMPTS+' VALUES(?,?,?,?,?,?,NULL)',(str(uuid.uuid4()),child['job_id'],child['lease_owner'],context[0]['job_id'],now,'reserved'))
        if self.browser is None or self.manifest_builder is None:
            from .replay_part_publish_worker_v5 import PartReplayBrowser,part_replay_manifest
            self.browser=self.browser or PartReplayBrowser(self.settings);self.manifest_builder=self.manifest_builder or part_replay_manifest
        parent,binding,state,_,_,_=context
        manifest=self.manifest_builder(self.settings,self.store,parent,binding,state,state['attempt_id'],require_cover=False)
        options={}
        if (self.settings.root/'config/bilibili-video-handoff-v5.json').exists():
            from .replay_part_owner_handoff_v5 import record_lookup_handoff
            options['owner_handoff']=lambda body,ref:record_lookup_handoff(service,child,body,ref)
        with self.browser.session(manifest,assert_lease=check,before_launch=before_launch,**options) as session:
            try:value=session.operation('lookup',**({'external_id':child['payload']['external_id']} if child['payload']['external_id'] else {}))
            except HubError as exc:
                if exc.code in ('REPLAY_PART_NEAR_END_SEEK_UNKNOWN','REPLAY_PART_NEAR_END_PLAY_UNKNOWN'):
                    # The platform may already have published the BV; a player
                    # verification timeout must move the group to review, not
                    # leave the parent in upload_pending.
                    try:self.service.record_publication_candidate(child,exc.code)
                    except HubError:pass
                    ReplayPartGroupsV5(self.settings,self.store).mark_parent_review(binding['parent_job_id'],exc.code)
                    raise
                if exc.code not in ('REPLAY_PART_OWNER_VERIFIED_RECONCILE_REQUIRED','REPLAY_PART_OWNER_RECONCILE_REQUIRED'):raise
                # The owner assistance/inspection ended, not the publication.
                # Schedule another read-only lookup; never re-run a submit.
                value={'state':'unknown','owner_handoff_ended':True}
            check()
        observation=value.get('result_observation');result_ref=value.get('result_reference');outcome='unknown';unverified=None
        draft=value.get('native_result_evidence',{})
        if draft.get('state')=='draft_found':
            service.record_draft_lookup(child,draft.get('draft_observation'),result_reference=draft.get('draft_reference'));outcome='draft_found'
        elif observation is not None:
            if not isinstance(observation,dict) or observation.get('state') not in ('awaiting_moderation','completed','failed'):fail('REPLAY_PART_LOOKUP_RESULT_REFERENCE_REQUIRED')
            if observation['state']=='completed':
                if not isinstance(result_ref,dict):fail('REPLAY_PART_LOOKUP_RESULT_REFERENCE_REQUIRED')
                service.record_lookup(child,observation,result_reference=result_ref);outcome='completed'
            else:
                # The publisher admits only full public owner/source/player
                # evidence. A candidate BV or pending/rejection label alone is
                # not that proof, nor permission to resend. Query again within
                # the original finite deadline without changing the parent.
                unverified=observation['state'];outcome='pending_unverified'
        assert_child(self.store,child);now=time.time()
        with self.store.transaction() as db:
            schedule=db.execute('SELECT * FROM '+TABLE+' WHERE parent_job_id=?',(parent['job_id'],)).fetchone()
            if not schedule or schedule['active_child_id']!=child['job_id']:fail('REPLAY_PART_LOOKUP_SCHEDULE_CHANGED')
            if outcome!='completed':lookup_context(self.settings,self.store,service,child)
            next_check=None if outcome in ('completed','failed','draft_found') else now+(cfg['fast_seconds'] if now-schedule['first_seen']<3600 else cfg['slow_seconds'])
            db.execute('UPDATE '+TABLE+' SET active_child_id=NULL,status=?,next_check=?,record=? WHERE parent_job_id=?',
                ('completed' if outcome=='completed' else 'draft_found' if outcome=='draft_found' else 'waiting',next_check,encode({'last_state':outcome,'unverified_browser_state':unverified,'last_child_id':child['job_id'],
                 **({'draft_id':draft['draft_observation']['draft_id'],'draft_url':draft['draft_observation']['draft_url'],'draft_reference':draft['draft_reference']} if outcome=='draft_found' else {})}),parent['job_id']))
            db.execute('UPDATE '+ATTEMPTS+" SET state='completed',code=? WHERE child_job_id=? AND lease_owner=?",(outcome,child['job_id'],child['lease_owner']))
        return {'version':VERSION,'parent_job_id':parent['job_id'],'observed_state':outcome,'lookup_only':True,'public_submits':0,'parent_attempts_changed':False}

def build_lookup_worker(settings,store,*,service=None,browser=None,manifest_builder=None):
    initialize(store);handler=LookupHandler(settings,store,service=service,browser=browser,manifest_builder=manifest_builder)
    return LookupWorker(settings,QueueView(store),{LOOKUP_JOB_TYPE:handler.process})
