"""Machine-only policy and durable first-party publishing. Never human approval."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from adapters.assets import download
from adapters.machine_compositor import machine_input,render_machine
from .db import encode
from .errors import HubError
from .evidence import sha256_file,machine_evidence
from .locks import resource_guard
from .maintenance import Maintenance
from .model_receipts import ModelReceipts
from .source_receipts import digest
from .tweet_reviews import TweetReviews
from .web_source import WebSources
from .outcomes import JobOutcome

VERSION='machine-tweet-v1'
KIND='dynamic_website_machine_v1'


def accepted_job_policy(settings):
    """Version immutable capture attempts without changing publishing approval."""
    generation=settings.config.get('machine_source_generation',1)
    if type(generation) is not int or not 1<=generation<=1000:
        raise HubError('MACHINE_SOURCE_GENERATION_INVALID')
    return VERSION if generation==1 else f'{VERSION}:source-{generation}'


def source_policy_generation(value):
    if value==VERSION:return 1
    match=re.fullmatch(re.escape(VERSION)+r':source-([1-9][0-9]{0,3})',value) if isinstance(value,str) else None
    if not match or not 2<=int(match[1])<=1000:raise HubError('MACHINE_SOURCE_GENERATION_INVALID')
    return int(match[1])


def owner_backfill_authorization(settings, source_id):
    """Owner-authorized exception for re-publishing one specific source.

    The file is deployment configuration, not code: it grants no blanket
    bypass. Entries must carry an explicit reference, an authorisation time
    and a hard not-after bound; anything malformed is ignored.
    """
    if not isinstance(source_id, str) or not source_id:
        return None
    try:
        path = settings.root / 'config/owner-backfill-authorizations.json'
        if path.is_symlink():
            return None
        data = json.loads(path.read_text('utf-8'))
        if data.get('version') != 'owner-backfill-authorizations-v1':
            return None
        for item in data.get('authorizations', []):
            if item.get('source_id') != source_id:
                continue
            if (type(item.get('authorized_at')) not in (int, float) or
                    type(item.get('not_after')) not in (int, float) or
                    not item.get('reference')):
                return None
            return item
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return None


class MachineTweets:
    version=VERSION
    kind=KIND

    def __init__(self,settings,store):
        self.settings,self.store=settings,store
        self.files=TweetReviews(settings,store)

    def job_policy(self):return accepted_job_policy(self.settings)

    def validate_generation(self,value):return source_policy_generation(value)

    def source_check(self,job,*,require_fresh=True):
        return WebSources(self.settings,self.store).assess(job,require_fresh=require_fresh)

    def _require_current_generation(self,job):
        if not job or job.get('policy_version')!=self.job_policy():
            raise HubError('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')

    def require_source_not_cleared(self,job,*,db=None):
        """A source-level owner decision survives job retries and new captures.

        Lookup and recording deliberately do not call this guard: clearing a
        backlog must never erase or strand an already uncertain publication.
        An explicit, time-bounded owner backfill authorization is the only
        exception; it never applies to a different source.
        """
        auth=owner_backfill_authorization(self.settings,job['source_id'])
        if auth and time.time()<=auth['not_after']:
            return
        args=('x',job['source_id'])
        sql='SELECT 1 FROM source_tombstones WHERE source_platform=? AND source_id=?'
        blocked=db.execute(sql,args).fetchone() if db is not None else self.store.query(sql,args)
        if blocked:raise HubError('OWNER_BACKLOG_CLEARED')

    def _require_snapshot_generation(self,job):
        if job['policy_version']==self.job_policy():return
        self.validate_generation(job['policy_version'])
        # A generation upgrade cannot strand a previous uncertain send. Its
        # immutable evidence may still be used for lookup/recording, never a new
        # reservation; reserve() separately requires the currently active source.
        persisted=self.store.query("SELECT 1 FROM publications WHERE job_id=? AND kind=? AND state IN ('dispatching','unknown','completed')",
            (job['job_id'],self.kind))
        if not persisted:raise HubError('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')

    def policy(self):
        if not self.settings.config.get('machine_tweet_enabled',False):raise HubError('MACHINE_TWEET_POLICY_DISABLED')
        try:
            file=self.settings.root/'config/machine-tweet-policy.json'
            p=json.loads(file.read_text('utf-8'))
            hold=p.get('dispatch_not_before',0)
            if (file.is_symlink() or p['version']!=self.version or p['owner_confirmed_machine_mode'] is not True or
                p['human_review_required'] is not False or p['target_site_uid']!=self.settings.config['target_bilibili_site_uid'] or
                set(p['x_handles'])!={'example_handle_a','example_handle_b'} or p['rights']['owner_confirmed'] is not True or
                not {'publish_translation','publish_image'}<=set(p['rights']['actions']) or
                not p['rights']['reference'] or not p['owner_response'] or
                type(p['max_post_age_seconds']) is not int or not 60<=p['max_post_age_seconds']<=7200 or
                type(p['activation_at']) not in (int,float) or not math.isfinite(p['activation_at']) or
                not 0<=p['activation_at']<=time.time()+5 or
                type(hold) not in (int,float) or not math.isfinite(hold) or not 0<=hold<=p['activation_at']+3600 or
                p['supported_content']!=['visible_plain_text_without_media_or_context']):raise ValueError()
            return p
        except (OSError,KeyError,ValueError,TypeError):raise HubError('MACHINE_TWEET_POLICY_INVALID') from None

    def fresh(self,job,*,now=None):
        self._require_current_generation(job)
        self.require_source_not_cleared(job)
        p=self.policy();now=time.time() if now is None else now
        try:
            moment=datetime.fromisoformat(job['payload']['post']['created_at'].replace('Z','+00:00'))
            if moment.tzinfo is None:raise ValueError()
            posted=moment.timestamp()
        except (ValueError,KeyError,TypeError):raise HubError('MACHINE_SOURCE_TIMESTAMP_INVALID') from None
        auth=owner_backfill_authorization(self.settings,job['source_id'])
        if auth and auth['authorized_at']<=now<=auth['not_after']:
            return p
        if not 0<=now-posted<=p['max_post_age_seconds'] or posted<p['activation_at']-p['max_post_age_seconds']:
            raise HubError('OWNER_TWO_HOUR_WINDOW_EXPIRED')
        return p

    def prepare(self,job,translation,quality,checkpoint):
        p=self.fresh(job)
        source_proof=WebSources(self.settings,self.store).assess(job)
        quality_proof=machine_evidence(job['payload'],translation,quality,self.settings.config.get('glossary'))
        model_proof=ModelReceipts(self.store).assess(job,translation)
        if not (quality_proof['machine_checks_passed'] and model_proof['proof_integrity_passed'] and model_proof['live_transport_verified'] and
            model_proof['separate_requests_verified'] and model_proof['qa_recommends_pass']):
            raise HubError('MACHINE_TRANSLATION_CHECK_FAILED')
        folder=self.settings.root/'media'/job['job_id'];source=deepcopy(job['payload'])
        with resource_guard(self.settings.root/'data/locks/heavy'):
            configured=next(s for s in self.settings.config['sources'] if s['x_handle']==source['author']['username'])
            avatar=source['author'].get('profile_image_url') or configured.get('profile_image_url')
            if not avatar:raise HubError('MACHINE_SOURCE_AVATAR_MISSING')
            asset=download(avatar,folder/'machine-avatar.bin',allowed_hosts=self.settings.config['allowed_media_hosts'],
                max_bytes=5_000_000,check_disk=Maintenance(self.settings,self.store).check_disk)
            source['avatar_file']=asset['file'];source['source_screenshot_file']=str(folder/'browser-source.png')
            source['source_screenshot_captured_at']=source['capture']['captured_at']
            item=machine_input(source,translation)
            rendered=render_machine(self.settings,item,folder/'machine-v1')
        caption=f"{source['author']['name']}｜原帖中文翻译（非原图） 原帖：{source['url']}"
        manifest={'version':self.version,'job_id':job['job_id'],'job_policy_version':job['policy_version'],
            'post_id':job['source_id'],'source_url':source['url'],
            'source_sha256':checkpoint['source_sha256'],'target_site_uid':p['target_site_uid'],
            'target_name':self.settings.config['target_bilibili_account_name'],'policy_sha256':digest(p),
            'caption':caption,'caption_sha256':hashlib.sha256(caption.encode()).hexdigest(),
            'source_proof':source_proof,'model_proof':model_proof,'quality':quality_proof,'render':rendered,
            'human_review':False,'approval_basis':'owner_authorized_versioned_machine_policy'}
        file=folder/'machine-v1/manifest.json';file.write_text(encode(manifest),'utf-8')
        result={**checkpoint,'machine_web':{'manifest_file':str(file),'manifest_sha256':sha256_file(file),
            'artifact_sha256':rendered['sha256'],'state':'ready','policy':self.version,'human_review':False}}
        self.store.checkpoint(job,'machine_render_ready',result)
        return self.resume_prepared(job)

    def resume_prepared(self,job):
        """Resume a durable artifact without rerendering or changing child hashes.

        Tweet workers serialize this with the OS queue lock. If the process dies
        after checkpoint or child insertion, recovery reuses the same manifest.
        """
        self.fresh(job)
        current,manifest=self._snapshot(job['job_id'])
        result=current['result'];entry=result['machine_web']
        child=self.store.enqueue('machine_web',job['job_id'],'tweet_machine_publish','tweet',
            {'version':self.version,'parent_job_id':job['job_id'],'manifest_sha256':entry['manifest_sha256']},self.version)
        payload=self.store.get(child)['payload']
        if payload.get('manifest_sha256')!=entry['manifest_sha256']:
            raise HubError('MACHINE_PREPARED_QUEUE_HASH_CHANGED')
        hold=self.policy().get('dispatch_not_before',0)
        with self.store.transaction() as db:
            db.execute("UPDATE jobs SET next_run=MAX(next_run,?) WHERE job_id=? AND state IN ('pending','retry_scheduled')",(hold,child))
        result={**result,'machine_dispatch_job_id':child}
        self.store.audit('machine-policy','queue_machine_website',job['job_id'],'machine_evidence_bound_no_human_claim')
        return JobOutcome('upload_pending',result)

    def _snapshot(self,job_id):
        p=self.policy();job=self.store.get(job_id)
        try:
            if not job or job['job_type']!='tweet_process' or job['source_platform']!='x':raise ValueError()
            self._require_snapshot_generation(job)
            entry=job['result']['machine_web'];file=self.files._file(job_id,entry['manifest_file'])
            if sha256_file(file)!=entry['manifest_sha256']:raise ValueError()
            m=json.loads(file.read_text('utf-8'));render=m['render']
            source=self.files._file(job_id,self.settings.root/'media'/job_id/'source.json')
            image=self.files._file(job_id,render['png']);input_file=self.files._file(job_id,render['input_file'])
            report_file=self.files._file(job_id,render['report_file'])
            item=json.loads(input_file.read_text('utf-8'));post=item['post'];report=json.loads(report_file.read_text('utf-8'))
            if (m['version']!=self.version or m.get('job_policy_version',self.version)!=job['policy_version'] or
                m['job_id']!=job_id or m['post_id']!=job['source_id'] or m['source_url']!=job['payload']['url'] or
                m['policy_sha256']!=digest(p) or m['target_site_uid']!=p['target_site_uid'] or m['human_review'] is not False or
                sha256_file(source)!=m['source_sha256'] or json.loads(source.read_text('utf-8'))!=job['payload'] or
                sha256_file(image)!=render['sha256'] or sha256_file(input_file)!=render['input_sha256'] or
                sha256_file(report_file)!=render['report_sha256'] or report!=render['report'] or
                report['mode']!='machine-publish-v1' or report['machine_publishable'] is not True or report['human_review'] is not False or
                report['upstream_human_publishable'] is not False or report['input_sha256']!=render['input_sha256'] or
                report['png']['sha256']!=render['sha256'] or not all(v is True for v in report['checks'].values()) or
                post['id']!=job['source_id'] or post['url']!=m['source_url'] or post['originalText']!=job['payload']['post']['text'] or
                post['author']['handle']!=job['payload']['author']['username'] or post['author']['name']!=job['payload']['author']['name'] or
                post['review']!={'sourceConfirmed':False,'translationConfirmed':False} or post['sourceScreenshot']['reviewed'] is not False or
                post['media'] or post['quote'] or post['relationship']['type']!='original' or
                hashlib.sha256(m['caption'].encode()).hexdigest()!=m['caption_sha256']):raise ValueError()
            proof=ModelReceipts(self.store).assess(job,post['translation'])
            source_proof=WebSources(self.settings,self.store).assess(job,require_fresh=False)
            if (not proof['proof_integrity_passed'] or not proof['live_transport_verified'] or not proof['separate_requests_verified'] or not proof['qa_recommends_pass'] or
                source_proof!=m['source_proof'] or not machine_evidence(job['payload'],post['translation'],
                    m['quality']['independent_model_advice'],self.settings.config.get('glossary'))['machine_checks_passed']):raise ValueError()
            # Recheck original asset bytes as well as the immutable final PNG.
            for asset in report['upstream_report']['assets']:
                original=self.files._file(job_id,asset['inputFile'])
                if sha256_file(original)!=asset['sha256']:raise ValueError()
            return job,m
        except (KeyError,ValueError,TypeError,OSError):raise HubError('MACHINE_POLICY_OR_ARTIFACT_INVALID') from None

    def reserve(self,principal,job_id,*,site_uid,artifact_sha256,caption_sha256):
        if principal!='machine-policy':
            self.store.audit(principal,'reserve_machine_website',job_id,'denied')
            raise HubError('ACCESS_DENIED')
        with resource_guard(self.settings.root/'data/locks/tweet-review'):
            if self.settings.config.get('policy_version')!=self.version:raise HubError('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')
            job,m=self._snapshot(job_id);self.fresh(job);self.source_check(job)
            if (site_uid!=m['target_site_uid'] or artifact_sha256!=m['render']['sha256'] or caption_sha256!=m['caption_sha256']):
                raise HubError('MACHINE_WEBSITE_DRAFT_MISMATCH')
            key=digest(['bilibili','x',job['source_id'],'site_uid:'+site_uid,self.kind]);now=time.time()
            with self.store.transaction() as db:
                # Serialize the last permission check with the reservation. A
                # concurrent clear cannot race between this read and the send
                # reservation's commit.
                self.require_source_not_cleared(job,db=db)
                rows=db.execute("SELECT * FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=? AND account_id=?",
                    (job['source_id'],'site_uid:'+site_uid)).fetchall()
                if rows:return {'can_click_publish':False,'state':rows[0]['state'],'external_id':rows[0]['external_id']}
                current=db.execute('SELECT state,result FROM jobs WHERE job_id=?',(job_id,)).fetchone()
                if current['state']!='upload_pending' or json.loads(current['result']).get('machine_web')!=job['result']['machine_web']:
                    raise HubError('MACHINE_JOB_CHANGED')
                db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                    (key,job_id,'bilibili','x',job['source_id'],'site_uid:'+site_uid,self.kind,'dispatching',None,None,now))
                db.execute("UPDATE jobs SET state='publish_unknown',stage='machine_dispatch_reserved',error_code='MACHINE_RECONCILE_REQUIRED',updated_at=? WHERE job_id=?",(now,job_id))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (now,principal,'reserve_machine_website',job_id,'one_click_authorized_by_machine_policy'))
            return {'can_click_publish':True,'state':'dispatching','idempotency_key':key}

    def record_visible(self,principal,job_id,evidence_file):
        if principal!='machine-policy':
            self.store.audit(principal,'record_machine_website',job_id,'denied')
            raise HubError('ACCESS_DENIED')
        job,m=self._snapshot(job_id);file=self.files._file(job_id,evidence_file)
        try:
            e=json.loads(file.read_text('utf-8'));url=urlsplit(e['url'])
            ident=re.fullmatch(r'/([0-9]+)/*',url.path) if url.hostname=='t.bilibili.com' else (
                re.fullmatch(r'/opus/([0-9]+)/*',url.path) if url.hostname=='www.bilibili.com' else None)
            screenshot=self.files._file(job_id,e['screenshot_file'])
            if (e.get('version')!='operator-browser-publication-observation-v1' or
                url.scheme!='https' or url.username or url.password or url.query or url.fragment or not ident or
                e['site_uid']!=m['target_site_uid'] or e['caption']!=m['caption'] or
                e['artifact_sha256']!=m['render']['sha256'] or sha256_file(screenshot)!=e['screenshot_sha256'] or
                e.get('visible_in_owner_browser') is not True):raise ValueError()
        except (KeyError,ValueError,TypeError,OSError):raise HubError('MACHINE_VISIBLE_RESULT_INVALID') from None
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM publications WHERE job_id=? AND kind=?',(job_id,self.kind)).fetchone()
            if (not row or row['state'] not in ('dispatching','unknown','completed') or
                    (row['external_id'] and row['external_id']!=ident[1])):raise HubError('MACHINE_DISPATCH_RECORD_REQUIRED')
            now=time.time();result={**job['result'],'machine_web':{**job['result']['machine_web'],'state':'completed',
                'external_publish_id':ident[1],'external_url':e['url']},'publication':{'state':'completed',
                'external_id':ident[1],'url':e['url'],'transport':'website_ui','policy':self.version,'human_review':False},
                'website_publication_evidence':{'file':str(file),'sha256':sha256_file(file),'observer':'server_browser_ui'}}
            db.execute("UPDATE publications SET state='completed',external_id=?,external_url=?,updated_at=? WHERE idempotency_key=?",(ident[1],e['url'],now,row['idempotency_key']))
            db.execute("UPDATE jobs SET state='completed',stage='machine_publication_visible',error_code=NULL,external_publish_id=?,result=?,updated_at=? WHERE job_id=?",
                (ident[1],encode(result),now,job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',(now,principal,'record_machine_website',job_id,'visible_result_link_saved'))
            from .tweet_repost_check_v1 import enqueue_check
            check_job=enqueue_check(db,self.settings,job,m,row,ident[1],recorded_at=now)
        return {'state':'completed','external_id':ident[1],'url':e['url'],'human_review':False,'policy':self.version,'repost_check_job_id':check_job}

    def dispatch(self,job):
        from .web_publish import run_machine_tweet
        request=job['payload'];parent_id=request.get('parent_job_id')
        parent,m=self._snapshot(parent_id)
        if (job['source_platform']!='machine_web' or job['source_id']!=parent_id or request.get('version')!=self.version or
            request.get('manifest_sha256')!=parent['result']['machine_web']['manifest_sha256']):raise HubError('MACHINE_QUEUE_REQUEST_INVALID')
        rows=self.store.query('SELECT state FROM publications WHERE job_id=? AND kind=?',(parent_id,self.kind))
        if not rows:self.fresh(parent)
        value=run_machine_tweet(self.settings,self.store,parent_id)
        return JobOutcome('completed',{'parent_job_id':parent_id,**value},external_id=value.get('external_id'))

    def reconcile(self,job):
        from .web_publish import run_machine_tweet
        observations=[]
        for row in self.store.query("SELECT job_id FROM publications WHERE kind=? AND state IN ('dispatching','unknown')",(self.kind,)):
            parent=self.store.get(row['job_id']);attempts=parent['result'].get('machine_reconcile',{}).get('attempts',0)
            if attempts>=self.settings.config['max_attempts']:continue
            try:
                value=run_machine_tweet(self.settings,self.store,parent['job_id'])
                with self.store.transaction() as db:
                    children=db.execute("SELECT job_id,result,state,error_code FROM jobs WHERE source_platform='machine_web' AND source_id=? AND job_type='tweet_machine_publish' AND state<>'processing'",(parent['job_id'],)).fetchall()
                    for child in children:
                        result={**json.loads(child['result']),'parent_job_id':parent['job_id'],
                            'reconciled_result':value,'previous_dispatch_state':child['state'],
                            'previous_dispatch_error':child['error_code']}
                        db.execute("UPDATE jobs SET state='completed',error_code=NULL,result=?,external_publish_id=?,stage='machine_website_reconciled',updated_at=?,lease_owner=NULL,lease_until=NULL WHERE job_id=?",
                            (encode(result),value.get('external_id'),time.time(),child['job_id']))
                observations.append(value)
            except HubError as exc:
                if exc.code=='HEAVY_RESOURCE_BUSY':raise
                current=self.store.get(parent['job_id']);result={**current['result'],'machine_reconcile':{'attempts':attempts+1,'last_error':exc.code}}
                with self.store.transaction() as db:db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',(encode(result),time.time(),parent['job_id']))
                self.store.event('task.failed',{'job_id':parent['job_id'],'code':exc.code},dedup_key='machine-reconcile:'+parent['job_id'])
        return {'observations':observations,'publish_requests_sent':0}

    def clear_backlog(self,*,before=None,all_existing=False):
        """Owner-requested queue clearing; never erase history or unresolved sends."""
        p=self.policy();cutoff=time.time()-p['max_post_age_seconds'] if before is None else before
        changed=[]
        with self.store.transaction() as db:
            rows=db.execute("SELECT job_id,source_id,payload FROM jobs WHERE source_platform='x' AND job_type='tweet_process' AND state IN ('pending','retry_scheduled','awaiting_review','upload_pending','auth_expired') AND NOT EXISTS(SELECT 1 FROM publications p WHERE p.source_platform='x' AND p.source_id=jobs.source_id AND p.state<>'rejected')").fetchall()
            for row in rows:
                try:posted=datetime.fromisoformat(json.loads(row['payload'])['post']['created_at'].replace('Z','+00:00')).timestamp()
                except (KeyError,ValueError,TypeError):posted=0
                if all_existing or posted<cutoff:
                    now=time.time()
                    db.execute('''INSERT OR IGNORE INTO source_tombstones
                        (source_platform,source_id,reason,marked_at,evidence_job_id,origin)
                        VALUES('x',?,'OWNER_BACKLOG_CLEARED',?,?,?)''',
                        (row['source_id'],now,row['job_id'],'owner_clear_all' if all_existing else 'policy_window_expired'))
                    db.execute("UPDATE jobs SET state='failed',error_code='OWNER_BACKLOG_CLEARED',stage='backlog_cleared',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?",(now,row['job_id']))
                    changed.append(row['job_id'])
            for parent_id in changed:
                db.execute("UPDATE jobs SET state='failed',error_code='OWNER_BACKLOG_CLEARED',updated_at=? WHERE source_id=? AND source_platform IN ('machine_web','reviewed_web') AND state IN ('pending','retry_scheduled','awaiting_review','upload_pending','auth_expired')",(time.time(),parent_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (time.time(),'owner-policy','clear_tweet_backlog','tweet',encode({'cleared':len(changed),'cutoff':cutoff,
                    'mode':'all_existing' if all_existing else 'expired_policy_window','source_tombstones_persisted':True,
                    'history_retained':True})))
        return {'cleared':len(changed),'job_ids':changed,'history_retained':True,'unresolved_publications_retained':True}
