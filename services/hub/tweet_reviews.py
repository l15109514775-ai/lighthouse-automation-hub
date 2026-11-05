"""Signed human artifact review; no publication, credentials or automatic confirmation."""
from copy import deepcopy
from pathlib import Path
import json
import re
import time
import tomllib
import uuid
from adapters.compositor import Compositor
from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .locks import resource_guard
from .maintenance import Maintenance
from .review_protocol import digest,read_review_json,load_reviewers,verify_packet

VERSION='human-tweet-review-v1'
CHECKS=('source_identity_time_and_full_text','translation_names_tone_numbers_and_breaks',
        'source_screenshot_media_and_references','rendered_image_and_layout','rights_and_credit','target_account')


class TweetReviews:
    def __init__(self,settings,store):
        self.settings,self.store=settings,store

    def readiness(self):
        try:
            count=len(load_reviewers(self.settings,'tweet'))
            return {'implemented':True,'authorized_signers':count,'ready_for_signed_review':count>0,
                'error_code':None if count else 'HUMAN_REVIEW_SIGNER_REQUIRED',
                'artifact_review_only':True,'publishing_enabled_by_review':False}
        except HubError as exc:
            return {'implemented':True,'authorized_signers':0,'ready_for_signed_review':False,
                'error_code':exc.code,'artifact_review_only':True,'publishing_enabled_by_review':False}

    def _file(self,job_id,value,*,base=None):
        try:
            if str(uuid.UUID(job_id))!=job_id: raise ValueError()
            media=self.settings.root/'media';root=media/job_id
            path=Path(value)
            if not path.is_absolute(): path=(base or root)/path
            if (path.is_symlink() or any(p.is_symlink() for p in path.parents if p!=self.settings.root.parent) or
                not root.resolve().is_relative_to(media.resolve()) or not path.resolve().is_relative_to(root.resolve()) or
                not path.is_file()): raise ValueError()
            return path.resolve()
        except (ValueError,TypeError,OSError,AttributeError):
            raise HubError('HUMAN_REVIEW_ARTIFACT_PATH_INVALID',review=True) from None

    def _info(self,file):
        return {'path':file.relative_to(self.settings.root).as_posix(),'sha256':sha256_file(file),'bytes':file.stat().st_size}

    def _grant(self,job,source_hash):
        file=self.settings.root/'config/tweet-rights.toml'
        if not file.exists(): return {'valid':False,'error_code':'TWEET_PUBLICATION_RIGHTS_REQUIRED'}
        try:
            data=tomllib.loads(file.read_text('utf-8-sig'))
            if set(data)!={'grants'} or not isinstance(data['grants'],list): raise ValueError()
            rows=[g for g in data['grants'] if isinstance(g,dict) and g.get('post_id')==job['source_id']]
            if len(rows)!=1: raise ValueError()
            grant=rows[0]
            if (set(grant)!={'post_id','x_handle','source_sha256','actions','evidence','authorized_by','target_site_uid'} or
                grant['x_handle']!=job['payload']['author']['username'] or grant['source_sha256']!=source_hash or
                grant['target_site_uid']!=self.settings.config['target_bilibili_site_uid'] or
                not isinstance(grant['actions'],list) or not {'publish_translation','publish_image'}<=set(grant['actions']) or
                any(not isinstance(grant.get(k),str) or not grant[k].strip() or len(grant[k])>256
                    for k in ('evidence','authorized_by'))): raise ValueError()
            return {'valid':True,'reference':grant['evidence'],'authorized_by':grant['authorized_by'],
                    'grant_sha256':digest(grant)}
        except (OSError,ValueError,KeyError,TypeError):
            return {'valid':False,'error_code':'TWEET_PUBLICATION_RIGHTS_REQUIRED'}

    def _assets(self,job_id,entry,base):
        try:return self._asset_paths(job_id,entry,base)
        except (KeyError,TypeError,AttributeError,ValueError):
            raise HubError('HUMAN_REVIEW_INPUT_INVALID',review=True) from None

    def _asset_paths(self,job_id,entry,base):
        posts=[entry['post']]
        for post in posts:
            if len(posts)>5: raise HubError('HUMAN_REVIEW_INPUT_INVALID',review=True)
            if post.get('quote'): posts.append(post['quote'])
            for container,key in [(post['author'],'avatar'),(post['sourceScreenshot'],'file')]+[(m,'file') for m in post['media']]:
                if container.get(key):container[key]=str(self._file(job_id,container[key],base=base))
        return entry

    def _checkpoint(self,job):
        if (not job or job['source_platform']!='x' or job['job_type']!='tweet_process' or
                job['state']!='awaiting_review' or job['stage']!='rendered_awaiting_human'):
            raise HubError('TWEET_REVIEWABLE_CHECKPOINT_REQUIRED',review=True)
        return self.settings.root/'media'/job['job_id']

    def snapshot(self,job,render=None):
        try:return self._snapshot(job,render)
        except (KeyError,TypeError,AttributeError,ValueError,OSError,RecursionError):
            raise HubError('HUMAN_REVIEW_INPUT_INVALID',review=True) from None

    def _snapshot(self,job,render=None):
        folder=self._checkpoint(job)
        source=self._file(job['job_id'],folder/'source.json')
        baseline=self._file(job['job_id'],folder/'compositor-input.json')
        original=read_review_json(source);base_input=read_review_json(baseline)
        source_hash=sha256_file(source)
        if original!=job['payload'] or source_hash!=job['result'].get('source_sha256'):
            raise HubError('HUMAN_REVIEW_INPUT_CHANGED',review=True)
        post=original.get('post',{});author=original.get('author',{})
        allowed={s['x_handle'] for s in self.settings.config['sources']}
        if (not re.fullmatch(r'[0-9]{1,30}',job['source_id']) or post.get('id')!=job['source_id'] or
                author.get('username') not in allowed or original.get('url')!=f"https://x.com/{author['username']}/status/{job['source_id']}"):
            raise HubError('HUMAN_REVIEW_SOURCE_IDENTITY_INVALID',review=True)
        if render is None:
            current=job['result'].get('render',{})
            render={'input':str(baseline),'artifact':current.get('png'),
                    'report':current.get('report_file') or str(folder/'rendered/hub-render-result.json')}
            if sha256_file(baseline)!=current.get('input_sha256'):
                raise HubError('HUMAN_REVIEW_INPUT_CHANGED',review=True)
        files={key:self._file(job['job_id'],value) for key,value in render.items()}
        if set(files)!={'input','artifact','report'}:raise HubError('HUMAN_REVIEW_INPUT_INVALID',review=True)
        item=read_review_json(files['input']);record=read_review_json(files['report']);report=record.get('report',{})
        p=item.get('post',{});bp=base_input.get('post',{})
        if (any(p.get(k)!=bp.get(k) for k in ('id','url','originalText','translation','publishedAt','sourceTimezone')) or
                any(p.get('author',{}).get(k)!=bp.get('author',{}).get(k) for k in ('name','handle'))):
            raise HubError('HUMAN_REVIEW_SOURCE_OR_TRANSLATION_CHANGED',review=True)
        if (p.get('id')!=post['id'] or p.get('url')!=original['url'] or p.get('originalText')!=post.get('text') or
                p.get('author',{}).get('handle')!=author['username'] or p.get('author',{}).get('name')!=author.get('name')):
            raise HubError('HUMAN_REVIEW_SOURCE_IDENTITY_INVALID',review=True)
        if (not isinstance(report,dict) or not isinstance(report.get('png'),dict) or
                report['png'].get('sha256')!=sha256_file(files['artifact']) or
                report.get('sourceUrl')!=original['url'] or
                self._file(job['job_id'],Path(record.get('dir',''))/report['png'].get('file',''))!=files['artifact']):
            raise HubError('HUMAN_REVIEW_RENDER_MISMATCH',review=True)
        if digest(item)!=report.get('inputSha256'):
            raise HubError('HUMAN_REVIEW_INPUT_CHANGED',review=True)
        normalized=self._assets(job['job_id'],deepcopy(item),files['input'].parent)
        required=set()
        posts=[normalized['post']]
        for node in posts:
            if node.get('quote'):posts.append(node['quote'])
            required.update(x for x in [node['author'].get('avatar'),node['sourceScreenshot'].get('file')]+[m['file'] for m in node['media']] if x)
        assets=[];observed=set()
        for asset in report.get('assets',[]):
            file=self._file(job['job_id'],asset.get('inputFile'),base=files['input'].parent)
            copy=self._file(job['job_id'],asset.get('outputFile'),base=files['artifact'].parent)
            if sha256_file(file)!=asset.get('sha256') or sha256_file(copy)!=asset.get('sha256'):
                raise HubError('HUMAN_REVIEW_ASSET_CHANGED',review=True)
            observed.add(str(file));assets.extend([self._info(file),self._info(copy)])
        if required!=observed:raise HubError('HUMAN_REVIEW_ASSET_CHANGED',review=True)
        evidence=self._file(job['job_id'],folder/'machine-evidence-v1.json')
        quality=read_review_json(evidence)
        rights=self._grant(job,source_hash)
        blockers=[]
        if not rights['valid']:blockers.append(rights['error_code'])
        if quality.get('machine_checks_passed') is not True:blockers.append('TRANSLATION_CHECK_FAILED')
        if report.get('mode')!='publish' or report.get('publishable') is not True or any(
                i.get('severity')=='error' for i in report.get('issues',[])):
            blockers.append('TWEET_FINAL_RENDER_REQUIRED')
        return {'version':VERSION,'job_id':job['job_id'],'policy_version':job['policy_version'],
            'source_id':job['source_id'],'source_url':original['url'],'source':self._info(source),
            'input':self._info(files['input']),'artifact':self._info(files['artifact']),
            'renderer_report':self._info(files['report']),'render_mode':report.get('mode'),
            'render_issues':[i['code'] for i in report.get('issues',[])],
            'assets':sorted(assets,key=lambda v:v['path']),'translation_evidence':self._info(evidence),
            'rights':rights,'target':{'site_uid':self.settings.config['target_bilibili_site_uid'],
                'name':self.settings.config['target_bilibili_account_name'],'identity_kind':'website_uid_not_api_openid'},
            'approval_blockers':blockers,'publication_transport_connected':False}

    def prepare(self,job_id,input_file=None):
        with resource_guard(self.settings.root/'data/locks/heavy',self.settings.root/'data/locks/tweet-review'):
            job=self.store.get(job_id);folder=self._checkpoint(job)
            candidate=None
            if input_file is not None:
                Maintenance(self.settings,self.store).check_disk(reserve_bytes=1_000_000_000)
                file=self._file(job_id,input_file)
                item=self._assets(job_id,read_review_json(file),file.parent)
                # Only paths are resolved; no human confirmation or review field is changed.
                target=folder/'human-review-candidates'/digest(item)
                result=Compositor(Path(__file__).resolve().parents[2]).render(item,target,mode='publish',allowed_root=folder)
                candidate={'input':str(target/'compositor-input.json'),'artifact':result['png'],'report':result['report_file']}
            manifest=self.snapshot(job,candidate);fingerprint=digest(manifest);now=time.time()
            with self.store.transaction() as db:
                db.execute("UPDATE tweet_review_requests SET decision='expired' WHERE job_id=? AND decision='pending' AND expires_at<?",(job_id,now))
                prior=db.execute("SELECT * FROM tweet_review_requests WHERE job_id=? AND decision='pending'",(job_id,)).fetchone()
                if prior and prior['manifest_sha256']!=fingerprint:
                    db.execute("UPDATE tweet_review_requests SET decision='expired' WHERE request_id=?",(prior['request_id'],));prior=None
                ident=prior['request_id'] if prior else str(uuid.uuid4())
                if not prior:
                    db.execute('''INSERT INTO tweet_review_requests(request_id,job_id,manifest,manifest_sha256,created_at,expires_at,decision)
                        VALUES(?,?,?,?,?,?,'pending')''',(ident,job_id,encode(manifest),fingerprint,now,now+86400))
            packet={'manifest':manifest,'attestation':{'version':VERSION,'request_id':ident,'manifest_sha256':fingerprint,
                'reviewer_id':'','decision':'pending','checks':{key:False for key in CHECKS},'notes':''},'signature':''}
            file=folder/('tweet-review-'+ident+'.json')
            try:
                with file.open('x',encoding='utf-8') as stream:json.dump(packet,stream,ensure_ascii=False,indent=2)
            except FileExistsError:pass
            self.store.audit('local-operator','prepare_tweet_review',ident,'unsigned_draft')
            return {'request_id':ident,'file':str(file),'approval_blockers':manifest['approval_blockers'],
                'published':False,'human_review_required':True,'publication_transport_connected':False}

    def request(self,request_id):
        try:uuid.UUID(request_id)
        except (ValueError,TypeError,AttributeError):raise HubError('HUMAN_REVIEW_REQUEST_ID_INVALID',review=True) from None
        rows=self.store.query('SELECT * FROM tweet_review_requests WHERE request_id=?',(request_id,))
        if not rows:raise HubError('HUMAN_REVIEW_REQUEST_NOT_FOUND',review=True)
        return rows[0]

    def _signed(self,value,row):
        return verify_packet(value,row,version=VERSION,checks=CHECKS,reviewers=load_reviewers(self.settings,'tweet'))

    def _current(self,job,manifest):
        render={key:str(self.settings.root/manifest[name]['path']) for key,name in
                [('input','input'),('artifact','artifact'),('report','renderer_report')]}
        return self.snapshot(job,render)

    def submit_file(self,file):
        try:return self.submit(read_review_json(file))
        except HubError as exc:
            self.store.audit('unverified','submit_tweet_review','signed-document',exc.code);raise

    def submit(self,value):
        try:ident=value['attestation']['request_id']
        except (KeyError,TypeError):raise HubError('HUMAN_REVIEW_DOCUMENT_INVALID',review=True) from None
        with resource_guard(self.settings.root/'data/locks/heavy',self.settings.root/'data/locks/tweet-review'):
            row=self.request(ident);att=self._signed(value,row);decision=att['decision'];now=time.time()
            terminal={'approve':'accepted','reject':'rejected','revoke':'revoked'}[decision]
            if decision!='revoke' and row['decision']=='pending' and row['expires_at']<now:
                with self.store.transaction() as db:db.execute("UPDATE tweet_review_requests SET decision='expired' WHERE request_id=?",(ident,))
                raise HubError('HUMAN_REVIEW_REQUEST_EXPIRED',review=True)
            if row['decision']==terminal and row['attestation']==encode(att) and row['signature']==value['signature']:
                return {'request_id':ident,'decision':terminal,'published':False,'job_queued':False}
            if row['decision']!='pending' and not (decision=='revoke' and row['decision']=='accepted'):
                raise HubError('HUMAN_REVIEW_ALREADY_DECIDED',review=True)
            job=self.store.get(row['job_id']);manifest=json.loads(row['manifest'])
            if decision=='approve':
                if not all(att['checks'].values()):raise HubError('HUMAN_REVIEW_CHECKS_INCOMPLETE',review=True)
                current=self._current(job,manifest)
                if digest(current)!=row['manifest_sha256']:raise HubError('HUMAN_REVIEW_INPUT_CHANGED',review=True)
                if current['approval_blockers']:raise HubError(current['approval_blockers'][0],review=True)
            with self.store.transaction() as db:
                if decision=='approve':
                    state=db.execute('SELECT state,updated_at FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
                    if state['state']!='awaiting_review' or state['updated_at']!=job['updated_at']:
                        raise HubError('HUMAN_REVIEW_INPUT_CHANGED',review=True)
                    if db.execute('SELECT 1 FROM tweet_review_bindings WHERE job_id=?',(job['job_id'],)).fetchone():
                        raise HubError('HUMAN_REVIEW_REPLACEMENT_REQUIRES_REVOCATION',review=True)
                    db.execute('INSERT INTO tweet_review_bindings VALUES(?,?,?)',(job['job_id'],ident,row['manifest_sha256']))
                elif decision=='revoke':
                    db.execute('DELETE FROM tweet_review_bindings WHERE job_id=? AND request_id=?',(row['job_id'],ident))
                db.execute('''UPDATE tweet_review_requests SET decision=?,reviewer_id=?,attestation=?,signature=?,decided_at=?
                    WHERE request_id=?''',(terminal,att['reviewer_id'],encode(att),value['signature'],now,ident))
                db.execute('INSERT INTO tweet_review_decisions VALUES(?,?,?,?,?,?,?)',
                    (digest([att,value['signature']]),ident,att['reviewer_id'],decision,encode(att),value['signature'],now))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (now,'reviewer:'+att['reviewer_id'],'submit_tweet_review',ident,terminal))
                if decision=='reject':
                    db.execute("UPDATE jobs SET error_code='HUMAN_TWEET_REVIEW_REJECTED',updated_at=? WHERE job_id=? AND state='awaiting_review'",(now,row['job_id']))
            return {'request_id':ident,'decision':terminal,'published':False,'job_queued':False}

    def verify_approval(self,job):
        if not job:raise HubError('TWEET_REVIEWABLE_CHECKPOINT_REQUIRED',review=True)
        rows=self.store.query('''SELECT r.*,b.manifest_sha256 binding_hash FROM tweet_review_requests r
            JOIN tweet_review_bindings b ON b.request_id=r.request_id WHERE b.job_id=? AND r.decision='accepted' ''',(job['job_id'],))
        if not rows:raise HubError('SIGNED_HUMAN_TWEET_REVIEW_REQUIRED',review=True)
        row=rows[0];manifest=json.loads(row['manifest'])
        att=self._signed({'manifest':manifest,'attestation':json.loads(row['attestation']),'signature':row['signature']},row)
        if (row['binding_hash']!=row['manifest_sha256'] or att['decision']!='approve' or not all(att['checks'].values()) or
                digest(self._current(job,manifest))!=row['manifest_sha256'] or manifest['approval_blockers']):
            raise HubError('HUMAN_REVIEW_INPUT_CHANGED',review=True)
        return row['request_id']
