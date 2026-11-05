"""A delayed native-control observation, never another publication attempt.

Completed publication means the existing submission/link was recorded. This
check neither upgrades that to public visibility nor changes its reservation.
"""
from copy import deepcopy
from datetime import datetime
import argparse,hashlib,json,math,os,signal,stat,subprocess,time,uuid
from pathlib import Path
from urllib.parse import urlsplit

from .db import encode,Store
from .errors import HubError
from .locks import resource_guard

VERSION='tweet-native-repost-check-v1'
REINSPECTION='tweet-native-repost-reinspection-v2'
JOB_TYPE='tweet_repost_check_v1'
PRINCIPAL='worker:tweet-repost-check-v1'
UID='1000000000000000'
DELAY=300
EXISTING={'1266077623570661433':'2117189314744590552','1266077700886364168':'2117189497125486924'}


def fail(code='TWEET_REPOST_CHECK_BINDING_INVALID'):raise HubError(code)
def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()
def timestamp(value):
    try:
        value=datetime.fromisoformat(value.replace('Z','+00:00'))
        if value.tzinfo is None or not math.isfinite(value.timestamp()):raise ValueError()
        return value.timestamp()
    except (ValueError,TypeError,AttributeError):fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')


def _opened_editor(dialog,request):
    if dialog.get('kind')=='bilibili_share_editor_v1':
        editors=dialog.get('editor_controls',[]);controls=dialog.get('final_controls',[]);reference=dialog.get('reference') or {}
        return (dialog.get('title')=='分享到动态' and 'bili-dyn-share-publishing' in dialog.get('classes',[]) and
            type(dialog.get('editors')) is int and dialog['editors']==1 and len(editors)==1 and editors[0].get('tag')=='DIV' and
            editors[0].get('contenteditable')=='true' and 'bili-rich-textarea__inner' in editors[0].get('classes',[]) and
            reference.get('author_text')=='@'+request['target_name'] and
            ' '.join(request['caption'].split()) in ' '.join(reference.get('text','').split()) and request['source_url'] in reference.get('text','') and
            len(controls)==1 and controls[0].get('tag')=='DIV' and controls[0].get('text')=='发布' and
            all(k in controls[0].get('classes',[]) for k in ('bili-dyn-share-publishing__action','launcher')))
    return type(dialog.get('editors')) is int and dialog['editors']>0 and bool(dialog.get('final_controls')) and '转发' in dialog.get('text','')


def enqueue_check(db,settings,job,manifest,publication,external_id,*,recorded_at):
    """Called in record_visible's transaction; crash cannot lose the followup."""
    if (str(settings.config['target_bilibili_site_uid'])!=UID or manifest['target_site_uid']!=UID or
            job['source_platform']!='x' or job['job_type']!='tweet_process' or
            publication['job_id']!=job['job_id'] or publication['source_id']!=job['source_id'] or
            publication['account_id']!='site_uid:'+UID or not external_id.isdigit() or not 1<=len(external_id)<=30 or
            type(recorded_at) not in (int,float) or not math.isfinite(recorded_at) or recorded_at<0):fail()
    actual=db.execute('SELECT * FROM publications WHERE idempotency_key=?',(publication['idempotency_key'],)).fetchone()
    if (not actual or actual['state']!='completed' or actual['external_id']!=external_id or actual['job_id']!=job['job_id'] or
            actual['kind']!=publication['kind'] or actual['source_platform']!='x' or actual['platform']!='bilibili'):fail()
    request={'version':VERSION,'submission_job_id':job['job_id'],'source_id':job['source_id'],
        'external_id':external_id,'url':'https://t.bilibili.com/'+external_id,'target_site_uid':UID,
        'target_name':manifest['target_name'],'caption':manifest['caption'],'source_url':manifest['source_url'],
        'caption_sha256':manifest['caption_sha256'],'manifest_sha256':job['result']['machine_web']['manifest_sha256'],
        'publication_key':publication['idempotency_key'],'publication_kind':publication['kind']}
    old=db.execute('SELECT * FROM jobs WHERE source_platform=? AND source_id=? AND job_type=? AND policy_version=?',
        ('bilibili',external_id,JOB_TYPE,VERSION)).fetchone()
    if old:
        previous=json.loads(old['payload'])
        if {k:previous.get(k) for k in request}!=request:fail('TWEET_REPOST_CHECK_CONFLICT')
        return old['job_id']
    ident=str(uuid.uuid4());now=time.time();request.update(submission_recorded_at=recorded_at,not_before=recorded_at+DELAY)
    db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload,next_run,stage)
        VALUES(?,?,?,?,'tweet','pending',?,?,?,?,?,'repost_check_scheduled')""",
        (ident,'bilibili',external_id,JOB_TYPE,now,now,VERSION,encode(request),request['not_before']))
    db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,PRINCIPAL,'queue_tweet_repost_check',job['job_id'],
        encode({'check_job_id':ident,'external_id':external_id,'not_before':request['not_before'],'human_review':False,'send_authorized':False})))
    return ident


def _read(file,root,maximum):
    file=Path(file)
    if not file.is_relative_to(root) or file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)):fail('TWEET_REPOST_CHECK_EVIDENCE_INVALID')
    fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        a=os.fstat(stream.fileno())
        if (not stat.S_ISREG(a.st_mode) or a.st_nlink!=1 or not 0<a.st_size<=maximum or
                os.name=='posix' and (a.st_uid!=10001 or a.st_mode&0o022)):fail('TWEET_REPOST_CHECK_EVIDENCE_INVALID')
        raw=stream.read(maximum+1);b=os.fstat(stream.fileno());p=file.stat()
    fields=('st_dev','st_ino','st_size','st_mtime_ns')+(('st_ctime_ns',) if os.name=='posix' else ())
    if len(raw)!=a.st_size or any(getattr(a,k)!=getattr(b,k) or getattr(a,k)!=getattr(p,k) for k in fields):fail('TWEET_REPOST_CHECK_EVIDENCE_INVALID')
    return raw


def validate_observation(settings,request,attempt,ref,*,now=None):
    now=time.time() if now is None else now
    area=settings.root/'media'/request['check_job_id']/'tweet-repost-check-v1'/attempt
    if set(ref)!={'file','bytes','sha256'} or ref['file']!=str(area/'observation.json'):fail('TWEET_REPOST_CHECK_EVIDENCE_INVALID')
    raw=_read(ref['file'],area,2_000_000)
    if len(raw)!=ref['bytes'] or hashlib.sha256(raw).hexdigest()!=ref['sha256']:fail('TWEET_REPOST_CHECK_EVIDENCE_INVALID')
    body=json.loads(raw);observed=timestamp(body['observed_at'])
    version=body['version'];actions=body['actions']
    keys={'clicks','file_selections','fills','submits'}
    if version=='tweet-native-repost-observation-v2':keys|={'author_navigation_clicks','repost_dialog_clicks'}
    if (version not in ('tweet-native-repost-observation-v1','tweet-native-repost-observation-v2') or
            set(actions)!=keys or any(type(actions[k]) is not int for k in keys) or
            any(actions[k]!=0 for k in ('file_selections','fills','submits'))):
        fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
    author_clicks=actions.get('author_navigation_clicks',0)
    repost_clicks=actions.get('repost_dialog_clicks',actions['clicks'])
    if (author_clicks not in (0,1) or repost_clicks not in (0,1) or
            actions['clicks']!=author_clicks+repost_clicks):fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
    if (body['request_sha256']!=digest(request) or
            body['check_job_id']!=request['check_job_id'] or body['attempt_id']!=attempt or
            body['external_id']!=request['external_id'] or body['target_site_uid']!=UID or
            not request['not_before']<=observed<=now+2 or now-observed>120 or
            body['human_review'] is not False or body['platform_cause_verified'] is not False):fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
    image=body['screenshot']
    if set(image)!={'file','bytes','sha256'} or image['file']!=str(area/'observed.png'):fail('TWEET_REPOST_CHECK_EVIDENCE_INVALID')
    png=_read(image['file'],area,20_000_000)
    if len(png)!=image['bytes'] or hashlib.sha256(png).hexdigest()!=image['sha256'] or png[:8]!=b'\x89PNG\r\n\x1a\n':fail('TWEET_REPOST_CHECK_EVIDENCE_INVALID')
    before=body.get('before_probe_screenshot')
    if before is not None:
        if set(before)!={'file','bytes','sha256'} or before['file']!=str(area/'before-probe.png'):fail('TWEET_REPOST_CHECK_EVIDENCE_INVALID')
        before_bytes=_read(before['file'],area,20_000_000)
        if len(before_bytes)!=before['bytes'] or hashlib.sha256(before_bytes).hexdigest()!=before['sha256'] or before_bytes[:8]!=b'\x89PNG\r\n\x1a\n':fail('TWEET_REPOST_CHECK_EVIDENCE_INVALID')
    status=body['status'];binding=body['content_binding'];controls=body['controls'];probe=body.get('functional_probe',{})
    if repost_clicks:
        if before is None or probe.get('control_click_attempted') is not True or probe.get('final_send_clicks')!=0:fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
    if version=='tweet-native-repost-observation-v2' and (repost_clicks or status in ('enabled_unverified','disabled','not_found','dialog_opened','native_denied','probe_unconfirmed')):
        identity=body.get('author_identity')
        if not isinstance(identity,dict):fail('TWEET_REPOST_CHECK_AUTHOR_IDENTITY_REQUIRED')
        try:
            profile=urlsplit(identity['profile_url'])
            expected_paths={'/'+UID,'/'+UID+'/','/'+UID+'/dynamic','/'+UID+'/dynamic/'}
            valid=(identity['version']=='tweet-native-author-identity-v2' and identity['site_uid']==UID and
                profile.scheme=='https' and profile.netloc=='space.bilibili.com' and profile.path in expected_paths and
                not profile.query and not profile.fragment and identity['source_dynamic_url'] in
                (request['url'],'https://www.bilibili.com/opus/'+request['external_id']) and
                request['not_before']<=timestamp(identity['observed_at'])<=observed+2 and observed-timestamp(identity['observed_at'])<=120)
            if identity['method']=='visible_named_author_anchor':
                valid=valid and author_clicks==0
            elif identity['method']=='native_author_profile_navigation':
                target=identity['clicked_element'];guard=identity['profile_protection']
                valid=(valid and author_clicks==1 and target['tag']=='SPAN' and type(target['classes']) is list and
                    all(type(c) is str for c in target['classes']) and 'bili-dyn-title__text' in target['classes'] and target['text']==request['target_name'] and
                    identity['return_binding_verified'] is True and identity['returned_dynamic_url']==body['final_url'] and
                    guard=={'visible_controls':[],'challenge_frames':[],'blocked':False})
                picture=identity['profile_screenshot']
                if set(picture)!={'file','bytes','sha256'} or picture['file']!=str(area/'author-profile.png'):valid=False
                else:
                    data=_read(picture['file'],area,20_000_000)
                    valid=valid and len(data)==picture['bytes'] and hashlib.sha256(data).hexdigest()==picture['sha256'] and data[:8]==b'\x89PNG\r\n\x1a\n'
            else:valid=False
        except (KeyError,TypeError,ValueError,AttributeError):valid=False
        if not valid:fail('TWEET_REPOST_CHECK_AUTHOR_IDENTITY_REQUIRED')
    if status in ('enabled_unverified','disabled','not_found','dialog_opened','native_denied','probe_unconfirmed'):
        if body['final_url'] not in (request['url'],'https://www.bilibili.com/opus/'+request['external_id']) or body['account_uid']!=UID or not all(binding.get(k) is True for k in
                ('exact_dynamic','caption','source_url','author_uid','author_name')):fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
        if status=='not_found':
            if controls!=[]:fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
        else:
            if len(controls)!=1 or controls[0]['visible'] is not True or type(controls[0]['disabled']) is not bool or controls[0]['disabled'] is not (status=='disabled'):fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
            if status in ('dialog_opened','native_denied') and controls[0].get('in_viewport') is not True:fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
        if status=='dialog_opened' and (repost_clicks!=1 or probe.get('outcome')!='native_editor_opened_no_send' or
                not any(_opened_editor(d,request) for d in probe.get('dialogs',[]))):fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
        if status=='native_denied' and (repost_clicks!=1 or probe.get('outcome')!='native_denial' or
                not any(t not in probe.get('before_ui',{}).get('messages',[]) and any(v in t for v in ('审核','无法转发','不能转发','不支持','转发受限','不可转发','仅自己可见','动态已失效')) for t in probe.get('messages',[]))):fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
    elif status not in ('auth_required','protection_blocked','wrong_account','dynamic_not_found','http_denied','ui_unrecognized') and not (
            version=='tweet-native-repost-observation-v2' and status in ('author_identity_mismatch','author_navigation_unconfirmed')):fail('TWEET_REPOST_CHECK_OBSERVATION_INVALID')
    labels={'enabled_unverified':'repost_control_enabled_unverified','disabled':'repost_unavailable','not_found':'repost_control_not_found',
        'dialog_opened':'repost_dialog_opened_no_send','native_denied':'repost_unavailable'}
    return {'version':VERSION,'status':labels.get(status,status),'suspected_review':status in ('disabled','native_denied'),
        'platform_cause_verified':False,'public_visibility_verified':False,'observed_at':body['observed_at'],
        'external_id':request['external_id'],'url':request['url'],'raw_observation':ref,'screenshot':image,
        'provenance':'native_owner_browser','human_review':False,'send_requests':0,'functional_probe':probe,
        'before_probe_screenshot':before,'native_control_click_attempts':repost_clicks,
        'author_navigation_click_attempts':author_clicks,'total_native_click_attempts':actions['clicks'],
        'native_observation_version':version}


def previous_unrecognized_check(settings,store,external_id,*,expected_job_id=None,expected_result_sha256=None,db=None):
    """Historical integrity only; never turn an old observation into a new one."""
    if external_id not in EXISTING:fail('TWEET_REPOST_REINSPECTION_SCOPE_INVALID')
    sql="SELECT * FROM jobs WHERE source_platform='bilibili' AND source_id=? AND job_type=? AND policy_version=?"
    rows=list(db.execute(sql,(external_id,JOB_TYPE,VERSION))) if db is not None else store.query(sql,(external_id,JOB_TYPE,VERSION))
    if len(rows)!=1:fail('TWEET_REPOST_REINSPECTION_PREVIOUS_REQUIRED')
    old=dict(rows[0])
    for key in ('payload','result'):
        if isinstance(old[key],str):old[key]=json.loads(old[key])
    observations=old['result'].get('observations',[]);final=old['result'].get('final_observation')
    if (old['state']!='completed' or old['attempts']!=4 or old['lease_owner'] is not None or old['lease_until'] is not None or
            old['payload'].get('source_id')!=EXISTING[external_id] or not isinstance(observations,list) or len(observations)!=4 or
            final!=observations[-1] or not isinstance(final,dict) or final.get('status')!='ui_unrecognized' or
            expected_job_id is not None and old['job_id']!=expected_job_id or
            expected_result_sha256 is not None and digest(old['result'])!=expected_result_sha256):
        fail('TWEET_REPOST_REINSPECTION_PREVIOUS_REQUIRED')
    query='SELECT at,principal,outcome FROM audit WHERE action=? AND target=? ORDER BY at'
    audits=list(db.execute(query,(VERSION,old['job_id']))) if db is not None else store.query(query,(VERSION,old['job_id']))
    for outcome in observations:
        accepted=[]
        for audit in audits:
            try:matches=audit['principal']==PRINCIPAL and json.loads(audit['outcome'])==outcome
            except (ValueError,TypeError):matches=False
            if matches and type(audit['at']) in (int,float) and math.isfinite(audit['at']) and 0<=audit['at']<=time.time():accepted.append(audit['at'])
        if not accepted:fail('TWEET_REPOST_REINSPECTION_PREVIOUS_AUDIT_REQUIRED')
        ref=outcome['raw_observation'];area=settings.root/'media'/old['job_id']/'tweet-repost-check-v1'
        body=json.loads(_read(ref['file'],area,2_000_000))
        spec={**old['payload'],'check_job_id':old['job_id'],'attempt_id':body['attempt_id']}
        actual=validate_observation(settings,spec,spec['attempt_id'],ref,now=min(accepted))
        if (actual['status']!='ui_unrecognized' or actual['native_control_click_attempts']!=0 or
                actual['author_navigation_click_attempts']!=0 or actual['total_native_click_attempts']!=0 or
                any(actual.get(k)!=v for k,v in outcome.items())):
            fail('TWEET_REPOST_REINSPECTION_PREVIOUS_ZERO_ACTION_REQUIRED')
    return old


def _pid(pid):
    try:
        raw=Path('/proc',str(pid),'stat').read_text();tail=raw[raw.rfind(')')+2:].split()
        return {'pid':int(pid),'ppid':int(tail[1]),'start':int(tail[19]),'state':tail[0]}
    except (OSError,ValueError,IndexError):return None


def native_observe(settings,spec,assert_lease):
    """Bounded Node/Chrome cleanup before the profile/heavy locks are released."""
    if os.name!='posix':fail('TWEET_REPOST_NATIVE_LINUX_REQUIRED')
    env={k:os.environ[k] for k in ('PATH','PLAYWRIGHT_BROWSERS_PATH','TEMP','TMP','TMPDIR') if k in os.environ}
    env.update(HUB_ROOT=str(settings.root),HUB_BROWSER_CHANNEL=settings.config.get('web_browser_channel','chromium'),
        XDG_CONFIG_HOME='/tmp/lighthouse-browser-config',XDG_CACHE_HOME='/tmp/lighthouse-browser-cache')
    proc=subprocess.Popen(['node',str(settings.root/'adapters/browser_tweet_repost_check_v1.mjs')],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,start_new_session=True)
    owned={};initial=_pid(proc.pid)
    if initial:owned[proc.pid]=initial['start']
    def capture():
        rows=[v for p in Path('/proc').iterdir() if p.name.isdigit() and (v:=_pid(p.name))]
        for _ in range(8):
            added=False
            for row in rows:
                parent=_pid(row['ppid'])
                if row['pid'] not in owned and parent and owned.get(parent['pid'])==parent['start']:
                    owned[row['pid']]=row['start'];added=True
            if not added:break
    def live():return [pid for pid,start in owned.items() if (v:=_pid(pid)) and v['start']==start and v['state']!='Z']
    def signal_owned(sig):
        capture()
        for pid in reversed(live()):
            try:os.kill(pid,sig)
            except ProcessLookupError:pass
    try:
        proc.stdin.write(encode({'version':'tweet-repost-native-request-v1','request_json':encode(spec),'request_sha256':digest(spec)}).encode())
        proc.stdin.close();proc.stdin=None;end=time.monotonic()+180
        while proc.poll() is None:
            assert_lease();capture()
            if time.monotonic()>=end:raise HubError('TWEET_REPOST_NATIVE_TIMEOUT',retryable=True,retry_after=60)
            time.sleep(.1)
        capture();output=proc.stdout.read(100000)
        if proc.returncode or len(output)>64000:raise HubError('TWEET_REPOST_NATIVE_RESULT_MISSING',retryable=True,retry_after=60)
        return json.loads(output)
    finally:
        capture()
        if proc.poll() is None:proc.terminate()
        end=time.monotonic()+7
        while live() and time.monotonic()<end:capture();time.sleep(.1)
        if live():signal_owned(signal.SIGTERM)
        end=time.monotonic()+2
        while live() and time.monotonic()<end:time.sleep(.1)
        if live():signal_owned(signal.SIGKILL)
        proc.wait(timeout=5)
        if live():fail('TWEET_REPOST_NATIVE_CLEANUP_REQUIRED')


class TweetRepostCheck:
    def __init__(self,settings,store,*,observer=None):self.settings,self.store,self.observer=settings,store,observer or native_observe

    def context(self,job,*,db=None):
        if (job['job_type']!=JOB_TYPE or job['source_platform']!='bilibili' or job['policy_version'] not in (VERSION,REINSPECTION) or job['queue']!='tweet' or
                job['payload'].get('version')!=VERSION or job['source_id']!=job['payload'].get('external_id') or
                job['payload'].get('target_site_uid')!=UID or str(self.settings.config['target_bilibili_site_uid'])!=UID):fail()
        request=job['payload'];parent=self.store.get(request['submission_job_id'])
        if any(type(request[k]) not in (int,float) or not math.isfinite(request[k]) for k in ('not_before','submission_recorded_at')):fail()
        rows=(db.execute('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',(request['submission_job_id'],request['publication_key'])).fetchall()
            if db is not None else self.store.query('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',(request['submission_job_id'],request['publication_key'])))
        if (not parent or parent['source_platform']!='x' or parent['source_id']!=request['source_id'] or parent['job_type']!='tweet_process' or
                parent['state']!='completed' or parent['external_publish_id']!=request['external_id'] or len(rows)!=1 or
                rows[0]['state']!='completed' or rows[0]['kind']!=request['publication_kind'] or rows[0]['source_id']!=request['source_id'] or
                rows[0]['account_id']!='site_uid:'+UID or rows[0]['external_id']!=request['external_id'] or
                parent['result'].get('machine_web',{}).get('manifest_sha256')!=request['manifest_sha256'] or
                request['url']!='https://t.bilibili.com/'+request['external_id'] or
                hashlib.sha256(request['caption'].encode()).hexdigest()!=request['caption_sha256'] or
                request['not_before']!=request['submission_recorded_at']+DELAY):fail()
        entry=parent['result']['machine_web'];raw=_read(entry['manifest_file'],self.settings.root/'media'/parent['job_id'],2_000_000)
        manifest=json.loads(raw)
        if (hashlib.sha256(raw).hexdigest()!=request['manifest_sha256'] or
                any(manifest.get(k)!=request[k] for k in ('caption','caption_sha256','source_url','target_name')) or
                manifest.get('target_site_uid')!=UID or manifest.get('job_id')!=parent['job_id'] or manifest.get('post_id')!=parent['source_id']):fail()
        if job['policy_version']==REINSPECTION:
            recheck=request.get('reinspection')
            if (not isinstance(recheck,dict) or set(recheck)!={'version','previous_check_job_id','previous_result_sha256','owner_report_reference'} or
                    recheck['version']!=REINSPECTION or not isinstance(recheck['owner_report_reference'],str) or
                    not recheck['owner_report_reference'].strip() or len(recheck['owner_report_reference'])>2000):fail('TWEET_REPOST_REINSPECTION_SCOPE_INVALID')
            previous=previous_unrecognized_check(self.settings,self.store,request['external_id'],expected_job_id=recheck['previous_check_job_id'],
                expected_result_sha256=recheck['previous_result_sha256'],db=db)
            if previous['payload']!={k:v for k,v in request.items() if k!='reinspection'}:fail('TWEET_REPOST_REINSPECTION_BINDING_CHANGED')
        elif 'reinspection' in request:fail('TWEET_REPOST_REINSPECTION_SCOPE_INVALID')
        return parent

    def assert_lease(self,job,*,db=None):
        row=(db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone() if db is not None else self.store.get(job['job_id']))
        if (not row or row['state']!='processing' or row['lease_owner']!=job.get('lease_owner') or
                (row['lease_until'] or 0)<=time.time() or row['attempts']!=job['attempts'] or
                (json.loads(row['payload']) if isinstance(row['payload'],str) else row['payload'])!=job['payload']):fail('TWEET_REPOST_CHECK_LEASE_LOST')

    def process(self,job):
        self.assert_lease(job);self.context(job)
        reinspection=job['policy_version']==REINSPECTION
        action=REINSPECTION if reinspection else VERSION
        if time.time()<job['payload']['not_before']:raise HubError('TWEET_REPOST_CHECK_NOT_DUE',retryable=True,retry_after=DELAY)
        if job['result'].get('final_observation'):
            audits=self.store.query('SELECT outcome FROM audit WHERE action=? AND target=?',(action,job['job_id']))
            if not any(json.loads(a['outcome'])==job['result']['final_observation'] for a in audits):fail('TWEET_REPOST_CHECK_AUDIT_REQUIRED')
            return job['result']  # Crash after durable observation: no second browser check.
        with resource_guard(self.settings.root/'data/locks/tweet-repost-check',self.settings.root/'data/locks/heavy'):
            self.assert_lease(job);self.context(job);attempt=str(uuid.uuid4())
            spec={**job['payload'],'check_job_id':job['job_id'],'attempt_id':attempt}
            ref=self.observer(self.settings,spec,lambda:self.assert_lease(job))
            outcome=validate_observation(self.settings,spec,attempt,ref)
            if reinspection and outcome['native_observation_version']!='tweet-native-repost-observation-v2':fail('TWEET_REPOST_REINSPECTION_V2_EVIDENCE_REQUIRED')
            retry=outcome['status'] in ('repost_control_not_found','dynamic_not_found','ui_unrecognized','http_denied') and job['attempts']<min(4,self.settings.config['max_attempts'])
            result={**job['result'],'observations':[*job['result'].get('observations',[]),outcome]}
            if not retry:result['final_observation']=outcome
            with self.store.transaction() as db:
                self.assert_lease(job,db=db);parent=self.context(job,db=db)
                marker='post_submission_repost_reinspection_v2' if reinspection else 'post_submission_repost_check'
                parent_result={**parent['result'],marker:outcome}
                db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(parent_result),parent['job_id']))
                db.execute('UPDATE jobs SET result=?,stage=?,updated_at=? WHERE job_id=?',(encode(result),'repost_control_observed',time.time(),job['job_id']))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(time.time(),PRINCIPAL,action,job['job_id'],encode(outcome)))
            job['result']=result
            if retry:raise HubError('TWEET_REPOST_CHECK_NOT_CONFIRMED',retryable=True,retry_after=60)
            return result


def existing_plan(settings,store,external_id,*,apply=False,owner_report_reference=None):
    """Explicit two already-submitted Chise records only; never resend them."""
    if external_id not in EXISTING or apply and not isinstance(owner_report_reference,str):fail('TWEET_REPOST_EXISTING_SCOPE_INVALID')
    rows=store.query("SELECT * FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=? AND external_id=? AND account_id=? AND state='completed'",
        (EXISTING[external_id],external_id,'site_uid:'+UID))
    if len(rows)!=1:fail()
    publication=rows[0];parent=store.get(publication['job_id'])
    from .machine_routes import machine_service
    service=machine_service(settings,store,parent['policy_version']);job,manifest=service._snapshot(parent['job_id'])
    audits=store.query("SELECT at FROM audit WHERE action='record_machine_website' AND target=? AND outcome='visible_result_link_saved' ORDER BY at",(job['job_id'],))
    if not audits or job['state']!='completed' or job['external_publish_id']!=external_id:fail()
    result={'version':VERSION,'external_id':external_id,'submission_job_id':job['job_id'],'source_id':job['source_id'],
        'not_before':audits[0]['at']+DELAY,'state':'readonly_plan','publication_preserved':True,'send_requests':0}
    if apply:
        if not owner_report_reference.strip() or len(owner_report_reference)>2000:fail()
        with store.transaction() as db:
            current=db.execute('SELECT * FROM publications WHERE idempotency_key=?',(publication['idempotency_key'],)).fetchone()
            if dict(current)!=publication:fail()
            ident=enqueue_check(db,settings,job,manifest,current,external_id,recorded_at=audits[0]['at'])
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(time.time(),'owner-policy','owner_reported_repost_check_requested',job['job_id'],
                encode({'provenance':'owner_reported','owner_report_reference':owner_report_reference,'external_id':external_id,
                    'statement':'用户报告本人账号可见、其他账号不可见，并表示处于B站审核。','platform_cause_verified':False,'native_observation_claimed':False,'human_review':False})))
        result.update(state='queued',check_job_id=ident)
    return result


def reinspection_plan(settings,store,external_id,*,apply=False,owner_report_reference=None):
    """One separately versioned check for the exact two old zero-click failures."""
    plan=existing_plan(settings,store,external_id)
    old=previous_unrecognized_check(settings,store,external_id)
    result={**plan,'version':REINSPECTION,'previous_check_job_id':old['job_id'],
        'previous_attempts':old['attempts'],'previous_result_sha256':digest(old['result']),
        'old_records_preserved':True,'state':'readonly_plan'}
    if apply:
        if not isinstance(owner_report_reference,str) or not owner_report_reference.strip() or len(owner_report_reference)>2000:fail('TWEET_REPOST_REINSPECTION_SCOPE_INVALID')
        request={**old['payload'],'reinspection':{'version':REINSPECTION,'previous_check_job_id':old['job_id'],
            'previous_result_sha256':digest(old['result']),'owner_report_reference':owner_report_reference}}
        created=False
        with store.transaction() as db:
            previous=previous_unrecognized_check(settings,store,external_id,expected_job_id=old['job_id'],expected_result_sha256=digest(old['result']),db=db)
            TweetRepostCheck(settings,store).context(previous,db=db)
            rows=db.execute("SELECT * FROM jobs WHERE source_platform='bilibili' AND source_id=? AND job_type=? AND policy_version=?",
                (external_id,JOB_TYPE,REINSPECTION)).fetchall()
            if rows:
                if len(rows)!=1:fail('TWEET_REPOST_REINSPECTION_CONFLICT')
                saved=json.loads(rows[0]['payload']);expected=deepcopy(request)
                expected['reinspection']['owner_report_reference']=saved.get('reinspection',{}).get('owner_report_reference')
                if saved!=expected:fail('TWEET_REPOST_REINSPECTION_CONFLICT')
                ident=rows[0]['job_id']
            else:
                ident=str(uuid.uuid4());now=time.time()
                created=True
                db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload,next_run,stage)
                    VALUES(?,'bilibili',?,?,'tweet','pending',?,?,?,?,?,'repost_reinspection_scheduled')""",
                    (ident,external_id,JOB_TYPE,now,now,REINSPECTION,encode(request),max(now,request['not_before'])))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,'owner-policy',
                    'queue_tweet_repost_reinspection_v2',plan['submission_job_id'],encode({'check_job_id':ident,'previous_check_job_id':old['job_id'],
                    'previous_result_sha256':digest(old['result']),'external_id':external_id,'owner_report_reference':owner_report_reference,
                    'human_review':False,'send_authorized':False,'old_records_preserved':True})))
        result.update(state='queued' if created else 'existing_check_preserved',check_job_id=ident,new_job_created=created)
    return result


if __name__=='__main__':
    from .config import Settings
    p=argparse.ArgumentParser();group=p.add_mutually_exclusive_group(required=True)
    group.add_argument('--existing',choices=tuple(EXISTING));group.add_argument('--reinspect-existing',choices=tuple(EXISTING))
    p.add_argument('--apply',action='store_true');p.add_argument('--owner-report-reference');args=p.parse_args()
    s=Settings.load();function=reinspection_plan if args.reinspect_existing else existing_plan
    print(encode(function(s,Store(s.db_path),args.reinspect_existing or args.existing,apply=args.apply,owner_report_reference=args.owner_report_reference)))
