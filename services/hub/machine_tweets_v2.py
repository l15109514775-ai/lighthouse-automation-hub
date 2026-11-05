"""Independent machine policy for evidenced static photos and bounded replies.

The renderer remains a render-only component. Publication authority is checked
here against durable source observations, real model receipts and owner policy.
Original compositor human-review fields and the v1 policy retain their meaning.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from adapters.assets import download
from adapters.compositor import compositor_input
from .db import encode
from .errors import HubError
from .evidence import machine_evidence,sha256_file
from .locks import resource_guard
from .machine_tweets import MachineTweets
from .maintenance import Maintenance
from .model_contract import VERSION as MODEL_CONTRACT
from .model_receipts import ModelReceipts
from .model_revision import build_revision_context,validated_revision_context
from .source_receipts import digest

VERSION='machine-tweet-v2'
KIND='dynamic_website_machine_v2'
SCOPE=['visible_plain_text','public_static_photos_full_dimensions','bounded_reply_parent_reread']


class MachineTweetsV2(MachineTweets):
    version=VERSION
    kind=KIND
    model_contract_version=MODEL_CONTRACT
    policy_filename='machine-tweet-policy-v2.json'
    content_scope=SCOPE
    render_folder='machine-v2'
    render_script='render_machine_extended.mjs'
    render_source_version='machine-render-source-evidence-v2'
    render_model_version='machine-render-model-evidence-v2'
    render_ticket_version='machine-render-authorization-v2'
    render_report_version='machine-render-v2'
    render_result_filename='machine-render-v2-result.json'
    node_roles=('post','parent')

    def job_policy(self):
        generation=self.settings.config.get('machine_source_generation',1)
        if type(generation) is not int or not 1<=generation<=1000:
            raise HubError('MACHINE_SOURCE_GENERATION_INVALID')
        return self.version if generation==1 else f'{self.version}:source-{generation}'

    def validate_generation(self,value):
        if value==self.version:return 1
        match=re.fullmatch(re.escape(self.version)+r':source-([1-9][0-9]{0,3})',value) if isinstance(value,str) else None
        if not match or not 2<=int(match[1])<=1000:raise HubError('MACHINE_SOURCE_GENERATION_INVALID')
        return int(match[1])

    def policy(self):
        if not self.settings.config.get('machine_tweet_enabled',False):raise HubError('MACHINE_TWEET_POLICY_DISABLED')
        try:
            file=self.settings.root/'config'/self.policy_filename
            p=json.loads(file.read_text('utf-8'));hold=p.get('dispatch_not_before',0)
            if (file.is_symlink() or p['version']!=self.version or p['owner_confirmed_machine_mode'] is not True or
                p['human_review_required'] is not False or p['target_site_uid']!=self.settings.config['target_bilibili_site_uid'] or
                set(p['x_handles'])!={'example_handle_a','example-channel-b'} or p['rights']['owner_confirmed'] is not True or
                not {'publish_translation','publish_image'}<=set(p['rights']['actions']) or not p['rights']['reference'] or
                not p['owner_response'] or p['authorization_basis']!='existing_owner_automation_and_content_grant' or
                type(p['max_post_age_seconds']) is not int or not 60<=p['max_post_age_seconds']<=7200 or
                type(p['activation_at']) not in (int,float) or not math.isfinite(p['activation_at']) or
                not 0<=p['activation_at']<=time.time()+5 or
                type(hold) not in (int,float) or not math.isfinite(hold) or not 0<=hold<=p['activation_at']+3600 or
                p['supported_content']!=self.content_scope):raise ValueError()
            return p
        except (OSError,KeyError,ValueError,TypeError):raise HubError('MACHINE_TWEET_POLICY_INVALID') from None

    def source(self,job,*,require_fresh=True):
        from .extended_web_source import ExtendedWebSources
        return ExtendedWebSources(self.settings,self.store).assess(job,require_fresh=require_fresh)

    def source_check(self,job,*,require_fresh=True):
        return self.source(job,require_fresh=require_fresh)['proof']

    def model_node(self,parent,node,revision_context=None):
        """Immutable non-runnable node; a single fixed revision slot per role."""
        role=node['role'];source=node['source']
        if role not in self.node_roles:raise HubError('MACHINE_V2_NODE_ROLE_INVALID')
        suffix=':revision-1' if revision_context is not None else ''
        source_id=parent['job_id']+':'+role+suffix;ident=str(uuid.uuid4());now=time.time()
        result={'translation_contract_version':self.model_contract_version,'parent_job_id':parent['job_id'],
            'role':role,'human_review':False}
        if revision_context is not None:
            result.update(translation_revision=revision_context)
        with self.store.transaction() as db:
            db.execute('''INSERT OR IGNORE INTO jobs(job_id,source_platform,source_id,job_type,queue,state,
                created_at,updated_at,policy_version,payload,result,stage)
                VALUES(?,'machine_model',?,'machine_translation_node','tweet','upload_pending',?,?,?,?,?,?)''',
                (ident,source_id,now,now,self.version,encode(source),encode(result),'model_input_bound'))
            row=db.execute("SELECT * FROM jobs WHERE source_platform='machine_model' AND source_id=? AND job_type='machine_translation_node' AND policy_version=?",(source_id,self.version)).fetchone()
            saved=json.loads(row['result'])
            if (json.loads(row['payload'])!=source or saved.get('parent_job_id')!=parent['job_id'] or
                saved.get('role')!=role or saved.get('translation_revision')!=revision_context):
                raise HubError('MACHINE_V2_MODEL_INPUT_CHANGED')
        child=self.store.get(row['job_id'])
        if revision_context is not None:validated_revision_context(self.store,child)
        return child

    def _node_job(self,parent,node,entry):
        index=entry.get('revision_index',0)
        if type(index) is not int or index not in (0,1):raise HubError('MODEL_REVISION_LIMIT_REACHED')
        child=self.store.get(entry['job_id']);suffix=':revision-1' if index else ''
        if (not child or child['source_platform']!='machine_model' or child['job_type']!='machine_translation_node' or
            child['source_id']!=parent['job_id']+':'+node['role']+suffix or child['payload']!=node['source'] or
            child['policy_version']!=self.version or child['result'].get('parent_job_id')!=parent['job_id'] or
            child['result'].get('role')!=node['role']):raise HubError('MACHINE_V2_MODEL_INPUT_CHANGED')
        if index:
            context=validated_revision_context(self.store,child)
            basis=child['result']['translation_revision']
            if entry.get('previous_model_job_id')!=basis['previous_model_job_id']:
                raise HubError('MACHINE_V2_MODEL_INPUT_CHANGED')
        return child

    def _model_proof(self,parent,node,entry):
        current=self._node_job(parent,node,entry)
        if entry.get('revision_index')==1:
            context=validated_revision_context(self.store,current)
            previous=self.store.get(entry['previous_model_job_id'])
            original=ModelReceipts(self.store).assess(previous,context['previous_translation'])
            if not (original['proof_integrity_passed'] and original['live_transport_verified'] and
                    original['separate_requests_verified'] and original['qa_recommends_pass'] is False):
                raise HubError('MACHINE_TRANSLATION_CHECK_FAILED')
        proof=ModelReceipts(self.store).assess(current,entry['translation'])
        quality=machine_evidence(node['source'],entry['translation'],entry['quality'],self.settings.config.get('glossary'))
        if not (proof['proof_integrity_passed'] and proof['live_transport_verified'] and proof['separate_requests_verified'] and
                proof['qa_recommends_pass'] and quality['machine_checks_passed']):
            raise HubError('MACHINE_TRANSLATION_CHECK_FAILED')
        return proof,quality

    def _parent_result(self,db,parent):
        current=db.execute('SELECT * FROM jobs WHERE job_id=?',(parent['job_id'],)).fetchone()
        if not current or json.loads(current['payload'])!=parent['payload']:raise HubError('MACHINE_V2_MODEL_INPUT_CHANGED')
        acceptance=current['source_platform']=='acceptance' and current['job_type']=='machine_render_acceptance' and current['state']=='upload_pending'
        leased=(current['source_platform']=='x' and current['job_type']=='tweet_process' and current['state']=='processing' and
            bool(current['lease_owner']) and current['lease_owner']==parent.get('lease_owner') and
            type(current['lease_until']) in (int,float) and current['lease_until']>time.time())
        if not acceptance and not leased:raise HubError('MACHINE_V2_PARENT_LEASE_LOST')
        return json.loads(current['result'])

    def record_model_rejection(self,parent,node,entry,code):
        """Keep each rejected actual pair; resolving it never rewrites the raw QA."""
        child=self._node_job(parent,node,entry)
        proof=ModelReceipts(self.store).assess(child,entry['translation'])
        quality=machine_evidence(node['source'],entry['translation'],entry['quality'],self.settings.config.get('glossary'))
        failure={'role':node['role'],'model_job_id':child['job_id'],'error_code':code,
            'translation':entry['translation'],'quality':quality,'model_proof':proof,'human_review':False}
        with self.store.transaction() as db:
            result=self._parent_result(db,parent)
            history=result.get('machine_quality_history',[])
            # A retry reads the same pair and does not append duplicate events.
            if not any(x.get('model_job_id')==child['job_id'] and x.get('quality')==quality and x.get('model_proof')==proof for x in history):
                history=[*history,failure]
            result={**result,'machine_quality_failure':failure,'machine_quality_history':history}
            db.execute("UPDATE jobs SET result=?,stage='machine_quality_rejected',updated_at=? WHERE job_id=?",
                (encode(result),time.time(),parent['job_id']))
            db.execute("UPDATE jobs SET state='failed',error_code=?,stage='model_quality_rejected',result=?,updated_at=? WHERE job_id=?",
                (code,encode({**child['result'],**failure}),time.time(),child['job_id']))
        parent['result'],parent['stage']=result,'machine_quality_rejected'

    def resolve_node(self,parent,node,model,*,require_fresh=True):
        """One original pair, at most one evidence-bound correction and blind QA.

        Network, auth, budget or unknown outcomes propagate without revision.
        No number of restarts can allocate a third candidate or paid QA reroll.
        """
        if not require_fresh and not (parent['source_platform']=='acceptance' and
                parent['job_type']=='machine_render_acceptance' and parent['state']=='upload_pending'):
            raise HubError('MACHINE_V2_FRESHNESS_EXCEPTION_FORBIDDEN')
        def guard():
            if require_fresh:self.fresh(parent)
            with self.store.transaction() as db:self._parent_result(db,parent)
        def invoke(child,phase,translation=None):
            try:return model.invoke(child,phase,node['source'],translation)
            except HubError as exc:
                # Machine mode stops/alerts on technical errors rather than
                # converting provider review flags into per-post human review.
                raise HubError(exc.code,retryable=exc.retryable,retry_after=exc.retry_after) from None
        guard();child=self.model_node(parent,node);context=None
        for index in (0,1):
            guard();translated=invoke(child,'translation')
            guard();qa=invoke(child,'quality',translated['translation'])
            entry={'job_id':child['job_id'],'translation':translated['translation'],'quality':qa}
            if index:entry.update(revision_index=1,previous_model_job_id=context['previous_model_job_id'])
            try:proof,quality=self._model_proof(parent,node,entry)
            except HubError as exc:
                self.record_model_rejection(parent,node,entry,exc.code)
                proof=ModelReceipts(self.store).assess(self.store.get(child['job_id']),entry['translation'])
                if (index or not proof['proof_integrity_passed'] or not proof['live_transport_verified'] or
                    not proof['separate_requests_verified'] or proof.get('qa_recommends_pass') is not False or
                    qa.get('passed') is not False or not qa.get('issues')):raise
                guard()
                context=build_revision_context(self.store,child['job_id'],parent_job_id=parent['job_id'],
                    role=node['role'],source=node['source'])
                child=self.model_node(parent,node,context)
                continue
            with self.store.transaction() as db:
                result=self._parent_result(db,parent)
                result.setdefault('machine_model_resolution',{})[node['role']]={
                    'selected_model_job_id':child['job_id'],'revision_index':index,
                    'base_model_job_id':context['previous_model_job_id'] if index else child['job_id']}
                if result.get('machine_quality_failure',{}).get('role')==node['role']:
                    result.pop('machine_quality_failure')
                db.execute("UPDATE jobs SET result=?,updated_at=? WHERE job_id=?",(encode(result),time.time(),parent['job_id']))
                current=self.store.get(child['job_id'])
                db.execute("UPDATE jobs SET state='completed',error_code=NULL,stage='model_receipts_verified',result=?,updated_at=? WHERE job_id=?",
                    (encode({**current['result'],'translation':entry['translation'],'quality':quality,'model_proof':proof}),time.time(),child['job_id']))
            parent['result']=result
            return entry
        raise HubError('MODEL_REVISION_LIMIT_REACHED')

    @staticmethod
    def _write(file,value):
        file=Path(file);file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        raw=encode(value).encode('utf-8')
        if file.exists():
            if file.is_symlink() or file.read_bytes()!=raw:raise HubError('MACHINE_V2_EVIDENCE_IMMUTABLE')
        else:
            temp=file.with_suffix(file.suffix+'.partial')
            with temp.open('xb') as stream:
                os.chmod(temp,0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
            temp.replace(file)
            from .web_source import WebSources
            WebSources._sync_dir(file.parent)
        return {'file':str(file),'sha256':sha256_file(file)}

    def process(self,job,model):
        from .outcomes import JobOutcome
        self.fresh(job)
        rows=self.store.query("SELECT * FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=? AND account_id=?",
            (job['source_id'],'site_uid:'+self.settings.config['target_bilibili_site_uid']))
        if rows:
            if rows[0]['state']=='completed':return JobOutcome('completed',{'already_published':True,'url':rows[0]['external_url']},external_id=rows[0]['external_id'])
            raise HubError('MACHINE_SOURCE_ALREADY_RESERVED')
        source=self.source(job)
        if job['result'].get('machine_web'):return self.resume_prepared(job)
        entries={node['role']:self.resolve_node(job,node,model) for node in source['nodes']}
        return self.prepare_extended(job,source,entries)

    def _render_source_metadata(self,raw,node):
        raw['source_screenshot_file']=node['screenshot']['file']
        raw['source_screenshot_captured_at']=raw['capture']['captured_at']
        return {'source_screenshot':node['screenshot']}

    def _media_caption(self):
        return '\n附图保留公开页面完整尺寸版本；图片内文字未翻译。'

    def _verify_render_source_metadata(self,actual,node):
        if actual['sourceScreenshot']['reviewed'] is not False:
            raise HubError('MACHINE_POLICY_OR_ARTIFACT_INVALID')

    def _render_field(self,role):
        return 'post' if role=='post' else 'post.quote'

    def _render_input(self,prepared,source):
        post=prepared['post']
        if 'parent' in prepared:
            parent=prepared['parent'];post['quote']=parent;post['sourceQuote']='present'
            post['relationship']={'type':'reply','authorName':parent['author']['name'],
                'authorHandle':parent['author']['handle'],'url':parent['url']}
        else:post['relationship']={'type':'original'}
        return {'schemaVersion':1,'displayTimezone':'Asia/Shanghai','post':post}

    def _render_node(self,item,role):
        return item['post'] if role=='post' else item['post']['quote']

    def _validate_render_relationships(self,item,source):
        post=item['post']
        if (len(source['nodes'])==1 and (post['quote'] or post['relationship']['type']!='original')) or (
                len(source['nodes'])==2 and (post['relationship']['type']!='reply' or not post['quote'])):
            raise ValueError()

    def _relation_caption(self,prepared,source):
        return '\n附原推及主播回复。' if 'parent' in prepared else ''

    def _render_model_metadata(self):
        return {}

    def render_evidence(self,job,source,entries):
        """Build a render-only artifact; this method grants no send permission."""
        folder=self.settings.root/'media'/job['job_id'];folder.mkdir(exist_ok=True,mode=0o700)
        self._write(folder/'source.json',job['payload'])
        attempt=folder/self.render_folder/str(uuid.uuid4());attempt.mkdir(parents=True,mode=0o700)
        prepared={};ticket_nodes=[];models={}
        with resource_guard(self.settings.root/'data/locks/heavy'):
            for node in source['nodes']:
                role=node['role'];raw=deepcopy(node['source']);entry=entries[role]
                proof,quality=self._model_proof(job,node,entry);models[role]={'entry':entry,'proof':proof,'quality':quality}
                configured=next((s for s in self.settings.config['sources'] if s['x_handle']==raw['author']['username']),{})
                avatar=raw['author'].get('profile_image_url') or configured.get('profile_image_url')
                if not avatar:raise HubError('MACHINE_SOURCE_AVATAR_MISSING')
                asset=download(avatar,attempt/(role+'-avatar.bin'),allowed_hosts=self.settings.config['allowed_media_hosts'],
                    max_bytes=5_000_000,check_disk=Maintenance(self.settings,self.store).check_disk)
                raw['avatar_file']=asset['file']
                source_metadata=self._render_source_metadata(raw,node)
                item=compositor_input(raw,entry['translation'])['post'];item['notes']=[]
                item['sourceMediaCount']=len(node['media']);item['sourceQuote']='none'
                item['media']=[{'file':x['file'],'completeness':'complete','reviewed':False} for x in node['media']]
                prepared[role]=item
                payload_ref=self._write(attempt/(role+'-source-payload.json'),node['source'])
                source_ref=self._write(attempt/(role+'-source-proof.json'),{'version':self.render_source_version,
                    'evidence_kind':'server_receipt','human_review':False,'post_id':item['id'],'source_url':item['url'],
                    'original_text_sha256':hashlib.sha256(item['originalText'].encode()).hexdigest(),'source_payload':payload_ref,
                    **source_metadata,'media':node['media'],'provenance':node['provenance']})
                receipts=[]
                for phase,call in zip(('translation','quality'),proof['call_ids']):
                    file=ModelReceipts(self.store)._file(entry['job_id'],call)
                    receipts.append({'phase':phase,'file':str(file),'sha256':sha256_file(file)})
                model_ref=self._write(attempt/(role+'-model-proof.json'),{'version':self.render_model_version,
                    'evidence_kind':'server_receipt','human_review':False,'post_id':item['id'],
                    'source_evidence_sha256':source_ref['sha256'],'translation_sha256':hashlib.sha256(item['translation'].encode()).hexdigest(),
                    'assessment':proof,'receipts':receipts,**self._render_model_metadata()})
                ticket_nodes.append({'field':self._render_field(role),'post_id':item['id'],
                    'original_text_sha256':hashlib.sha256(item['originalText'].encode()).hexdigest(),
                    'translation_sha256':hashlib.sha256(item['translation'].encode()).hexdigest(),
                    'source_evidence':source_ref,'model_evidence':model_ref})
            item=self._render_input(prepared,source)
            input_ref=self._write(attempt/'input.json',item)
            ticket=self._write(attempt/'ticket.json',{'version':self.render_ticket_version,'policy':self.version,
                'purpose':'render_only','publication_authorized':False,'human_review':False,
                'input_sha256':input_ref['sha256'],'nodes':sorted(ticket_nodes,key=lambda x:x['field'])})
            env={k:os.environ[k] for k in ('PATH','TEMP','TMP','TMPDIR','PLAYWRIGHT_BROWSERS_PATH') if k in os.environ}
            env.setdefault('PLAYWRIGHT_BROWSERS_PATH',str(self.settings.root/'compositor/runtime/browsers'))
            try:
                proc=subprocess.run(['node',str(self.settings.root/'adapters'/self.render_script),
                    input_ref['file'],str(attempt/'rendered'),str(self.settings.root),ticket['file']],
                    env=env,cwd=self.settings.root/'compositor',capture_output=True,timeout=240)
            except (OSError,subprocess.SubprocessError):raise HubError('MACHINE_COMPOSITOR_RUNTIME_UNAVAILABLE') from None
            report_file=attempt/'rendered'/self.render_result_filename
            if proc.returncode or not report_file.is_file():raise HubError('MACHINE_COMPOSITOR_REAL_CHECK_FAILED')
            report=json.loads(report_file.read_text('utf-8'));image=Path(report['png']['file'])
            render={'png':str(image),'sha256':sha256_file(image),'report_file':str(report_file),
                'report_sha256':sha256_file(report_file),'report':report,'input_file':input_ref['file'],'input_sha256':input_ref['sha256'],
                'ticket_file':ticket['file'],'ticket_sha256':ticket['sha256']}
            # Publish a database checkpoint only after the newly rendered
            # evidence and its directory entries have reached stable storage.
            from .web_source import WebSources
            durable=[image,report_file,Path(input_ref['file']),Path(ticket['file'])]
            durable.extend(Path(a['inputFile']) for a in report['upstream_report']['assets'])
            for file in durable:
                if file.is_symlink() or not file.resolve().is_relative_to(self.settings.root.resolve()):
                    raise HubError('MACHINE_V2_EVIDENCE_PATH_INVALID')
                with file.open('rb') as stream:os.fsync(stream.fileno())
            for parent in {p.parent for p in durable}:WebSources._sync_dir(parent)
        caption=f"{job['payload']['author']['name']}｜原帖中文翻译（非原图） 原帖：{job['payload']['url']}"
        caption+=self._relation_caption(prepared,source)
        if any(n['media'] for n in source['nodes']):caption+=self._media_caption()
        return {'render':render,'models':models,'attempt':attempt,'caption':caption}

    def prepare_extended(self,job,source,entries):
        p=self.fresh(job)
        if source!=self.source(job):raise HubError('MACHINE_V2_SOURCE_CHANGED')
        built=self.render_evidence(job,source,entries)
        render,models,attempt,caption=(built[k] for k in ('render','models','attempt','caption'))
        folder=self.settings.root/'media'/job['job_id']
        manifest={'version':self.version,'job_id':job['job_id'],'job_policy_version':job['policy_version'],'post_id':job['source_id'],
            'source_url':job['payload']['url'],'source_sha256':sha256_file(folder/'source.json'),
            'target_site_uid':p['target_site_uid'],'target_name':self.settings.config['target_bilibili_account_name'],
            'policy_sha256':digest(p),'caption':caption,'caption_sha256':hashlib.sha256(caption.encode()).hexdigest(),
            'source_proof':source['proof'],'models':models,'render':render,'human_review':False,
            'approval_basis':'owner_authorized_versioned_machine_policy','renderer_grants_publication':False}
        ref=self._write(attempt/'manifest.json',manifest)
        self.store.checkpoint(job,'machine_render_ready',{**job['result'],'source_sha256':manifest['source_sha256'],
            'machine_web':{'manifest_file':ref['file'],'manifest_sha256':ref['sha256'],'artifact_sha256':render['sha256'],
                'state':'ready','policy':self.version,'human_review':False}})
        return self.resume_prepared(job)

    def _snapshot(self,job_id):
        p=self.policy();job=self.store.get(job_id)
        try:
            if not job or job['job_type']!='tweet_process' or job['source_platform']!='x':raise ValueError()
            self._require_snapshot_generation(job)
            entry=job['result']['machine_web'];file=self.files._file(job_id,entry['manifest_file'])
            if sha256_file(file)!=entry['manifest_sha256']:raise ValueError()
            m=json.loads(file.read_text('utf-8'));render=m['render']
            for filename,key in ((render['png'],'sha256'),(render['report_file'],'report_sha256'),
                (render['input_file'],'input_sha256'),(render['ticket_file'],'ticket_sha256')):
                if sha256_file(self.files._file(job_id,filename))!=render[key]:raise ValueError()
            source_file=self.files._file(job_id,self.settings.root/'media'/job_id/'source.json')
            source=self.source(job,require_fresh=False);report=json.loads(Path(render['report_file']).read_text('utf-8'))
            item=json.loads(Path(render['input_file']).read_text('utf-8'));post=item['post']
            if (m['version']!=self.version or m['job_policy_version']!=job['policy_version'] or m['job_id']!=job_id or
                m['post_id']!=job['source_id'] or m['source_url']!=job['payload']['url'] or m['policy_sha256']!=digest(p) or
                m['target_site_uid']!=p['target_site_uid'] or m['human_review'] is not False or
                m['renderer_grants_publication'] is not False or sha256_file(source_file)!=m['source_sha256'] or
                json.loads(source_file.read_text('utf-8'))!=job['payload'] or source['proof']!=m['source_proof'] or
                report!=render['report'] or report['version']!=self.render_report_version or report['machine_renderable'] is not True or
                report['machine_publishable'] is not False or report['publication_authorized'] is not False or
                report['human_review'] is not False or report['upstream_human_publishable'] is not False or
                report['input_sha256']!=render['input_sha256'] or report['ticket_sha256']!=render['ticket_sha256'] or
                report['png']['sha256']!=render['sha256'] or
                not all(x is True for x in report['checks'].values()) or
                hashlib.sha256(m['caption'].encode()).hexdigest()!=m['caption_sha256']):raise ValueError()
            if len(report['nodes'])!=len(source['nodes']):raise ValueError()
            for node in source['nodes']:
                role=node['role'];actual=self._render_node(item,role);raw=node['source'];model=m['models'][role]
                self._verify_render_source_metadata(actual,node)
                proof,quality=self._model_proof(job,node,model['entry'])
                if (proof!=model['proof'] or quality!=model['quality'] or actual['id']!=raw['post']['id'] or actual['url']!=raw['url'] or
                    actual['author']['name']!=raw['author']['name'] or actual['author']['handle']!=raw['author']['username'] or
                    actual['originalText']!=raw['post']['text'] or actual['translation']!=model['entry']['translation'] or
                    actual['review']!={'sourceConfirmed':False,'translationConfirmed':False} or
                    actual['sourceScreenshot']['reviewed'] is not False or any(x['reviewed'] is not False for x in actual['media']) or
                    len(actual['media'])!=len(node['media'])):raise ValueError()
                for actual_media,expected in zip(actual['media'],node['media']):
                    if actual_media['file']!=expected['file'] or sha256_file(Path(expected['file']))!=expected['sha256']:raise ValueError()
            self._validate_render_relationships(item,source)
            for asset in report['upstream_report']['assets']:
                original=Path(asset['inputFile'])
                if original.is_symlink() or not original.resolve().is_relative_to(self.settings.root.resolve()) or sha256_file(original)!=asset['sha256']:raise ValueError()
            return job,m
        except (KeyError,ValueError,TypeError,OSError):raise HubError('MACHINE_POLICY_OR_ARTIFACT_INVALID') from None
