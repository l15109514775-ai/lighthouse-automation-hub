"""Operator-signed video attestations. Nothing here infers or fabricates human review."""
import json
from pathlib import Path
import re
import time
import tomllib
import uuid
from adapters.bilibili import BilibiliOfficial
from adapters.video import VideoProcessor
from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .locks import resource_guard
from .review_protocol import digest,decode_key,read_review_json,load_reviewers,verify_packet

VERSION='human-video-review-v1'
CHECKS=('source_identity_and_completeness','source_and_output_watched',
        'audio_video_and_subtitle_quality','rights_and_credit','title_description_tags','target_account')


class VideoReviews:
    def __init__(self,settings,store,*,probe=None):
        self.settings,self.store=settings,store
        self.probe=probe or VideoProcessor(settings.root).probe

    def reviewers(self):
        return load_reviewers(self.settings, "video")

    def readiness(self):
        try:
            count=len(self.reviewers())
            return {'implemented':True,'authorized_signers':count,'private_key_required_on_server':False,
                'ready_for_signed_review':count>0,'error_code':None if count else 'HUMAN_REVIEW_SIGNER_REQUIRED',
                'publishing_enabled_by_review':False}
        except HubError as exc:
            return {'implemented':True,'authorized_signers':0,'ready_for_signed_review':False,
                    'error_code':exc.code,'publishing_enabled_by_review':False}

    def request(self,request_id):
        try: uuid.UUID(request_id)
        except (ValueError,TypeError,AttributeError):
            raise HubError('HUMAN_REVIEW_REQUEST_ID_INVALID',review=True) from None
        rows=self.store.query('SELECT * FROM video_review_requests WHERE request_id=?',(request_id,))
        if not rows: raise HubError('HUMAN_REVIEW_REQUEST_NOT_FOUND',review=True)
        return rows[0]

    def _grant(self,source_id):
        try:
            grants=tomllib.loads((self.settings.root/'config/rights.toml').read_text('utf-8-sig'))['grants']
            matches=[g for g in grants if isinstance(g,dict) and g.get('video_id')==source_id]
            if len(matches)!=1: raise ValueError()
            grant=matches[0]
            if (not {'download','process','publish'}<=set(grant.get('actions',[])) or
                not isinstance(grant.get('evidence'),str) or not grant['evidence'].strip() or
                not isinstance(grant.get('authorized_by'),str) or not grant['authorized_by'].strip()): raise ValueError()
            return grant
        except (OSError,KeyError,TypeError,ValueError):
            raise HubError('VIDEO_PUBLICATION_RIGHTS_REQUIRED',review=True) from None

    def _file(self,job_id,value):
        try:
            path=Path(value)
            root=(self.settings.root/'media'/job_id).resolve()
            if (not root.is_relative_to((self.settings.root/'media').resolve()) or
                (self.settings.root/'media'/job_id).is_symlink() or path.is_symlink() or
                not path.resolve().is_relative_to(root) or not path.is_file()): raise ValueError()
            return path.resolve()
        except (ValueError,TypeError,OSError):
            raise HubError('HUMAN_REVIEW_ARTIFACT_PATH_INVALID',review=True) from None

    def snapshot(self,job,account_id,*,processing=False):
        states={'awaiting_review','processing'} if processing else {'awaiting_review'}
        if (not job or job['source_platform']!='youtube' or job['job_type']!='video_process' or
            job['stage']!='video_ready_for_review' or job['state'] not in states):
            raise HubError('VIDEO_REVIEWABLE_CHECKPOINT_REQUIRED',review=True)
        source_id=job['source_id']
        url='https://www.youtube.com/watch?v='+source_id
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}',source_id) or job['payload'].get('url')!=url:
            raise HubError('HUMAN_REVIEW_SOURCE_IDENTITY_INVALID',review=True)
        observations=self.store.query('SELECT * FROM youtube_videos WHERE video_id=?',(source_id,))
        channels={s['youtube_channel_id'] for s in self.settings.config['sources']}
        if (not observations or observations[0]['state']!='ended' or observations[0]['channel_id'] not in channels or
            observations[0]['channel_id']!=job['payload'].get('channel_id') or
            observations[0]['replay_job_id']!=job['job_id'] or observations[0]['source_api']!='youtube-data-v3-official'):
            raise HubError('HUMAN_REVIEW_SOURCE_IDENTITY_INVALID',review=True)
        grant=self._grant(source_id)
        data=job['result']
        if data.get('source_sha256')!=grant.get('source_sha256') or data.get('rights_evidence')!=grant['evidence']:
            raise HubError('MEDIA_RIGHTS_OR_SOURCE_CHANGED',review=True)
        if not account_id or account_id not in self.settings.config['authorized_bilibili_accounts']:
            raise HubError('TARGET_ACCOUNT_NOT_AUTHORIZED',review=True)
        accounts=self.store.query('SELECT * FROM bilibili_accounts WHERE open_id=?',(account_id,))
        if not accounts or accounts[0]['name']!=self.settings.config['target_bilibili_account_name']:
            raise HubError('TARGET_ACCOUNT_IDENTITY_UNVERIFIED',review=True)
        account=accounts[0]
        if not {'USER_INFO','ARC_BASE'}<=set(json.loads(account['scopes'])):
            raise HubError('BILIBILI_SCOPE_REQUIRED',review=True)
        if not isinstance(data.get('source_asset'),dict) or not isinstance(data.get('processed'),dict):
            raise HubError('VIDEO_REVIEWABLE_CHECKPOINT_REQUIRED',review=True)
        source=self._file(job['job_id'],data['source_asset'].get('file'))
        output=self._file(job['job_id'],data['processed'].get('file'))
        if sha256_file(source)!=grant['source_sha256']:
            raise HubError('HUMAN_REVIEW_SOURCE_HASH_MISMATCH',review=True)
        artifact={**data['processed'],'bilibili':grant.get('bilibili')}
        BilibiliOfficial(self.settings,self.store,probe=self.probe)._artifact(artifact)
        if grant.get('bilibili',{}).get('source')!=url:
            raise HubError('HUMAN_REVIEW_SOURCE_IDENTITY_INVALID',review=True)
        return {'version':VERSION,'job_id':job['job_id'],'policy_version':job['policy_version'],
            'source_platform':'youtube','source_id':source_id,'source_url':url,'channel_id':observations[0]['channel_id'],
            'source':{'path':source.relative_to(self.settings.root).as_posix(),'sha256':grant['source_sha256'],'bytes':source.stat().st_size},
            'artifact':{'path':output.relative_to(self.settings.root).as_posix(),'sha256':artifact['sha256'],
                        'bytes':output.stat().st_size},
            'submission':grant['bilibili'],
            'rights':{'reference':grant['evidence'],'authorized_by':grant['authorized_by'],'grant_sha256':digest(grant)},
            'account':{'open_id':account_id,'app_id':account['app_id'],'name':account['name'],
                       'scopes':sorted(json.loads(account['scopes']))}}

    def prepare(self,job_id,account_id):
        with resource_guard(self.settings.root/'data/locks/heavy',self.settings.root/'data/locks/video-review'):
            job=self.store.get(job_id)
            manifest=self.snapshot(job,account_id)
            fingerprint=digest(manifest)
            now=time.time()
            with self.store.transaction() as db:
                db.execute("UPDATE video_review_requests SET decision='expired' WHERE job_id=? AND decision='pending' AND expires_at<?",(job_id,now))
                previous=db.execute("SELECT * FROM video_review_requests WHERE job_id=? AND decision='pending'",(job_id,)).fetchone()
                if previous and previous['manifest_sha256']!=fingerprint:
                    db.execute("UPDATE video_review_requests SET decision='expired' WHERE request_id=?",(previous['request_id'],))
                    previous=None
                ident=previous['request_id'] if previous else str(uuid.uuid4())
                if not previous:
                    db.execute('''INSERT INTO video_review_requests(request_id,job_id,manifest,manifest_sha256,created_at,expires_at,decision)
                        VALUES(?,?,?,?,?,?,'pending')''',(ident,job_id,encode(manifest),fingerprint,now,now+86400))
            packet={'manifest':manifest,'attestation':{'version':VERSION,'request_id':ident,
                'manifest_sha256':fingerprint,'reviewer_id':'','decision':'pending',
                'checks':{name:False for name in CHECKS},'notes':''},'signature':''}
            file=self.settings.root/'media'/job_id/('video-review-'+ident+'.json')
            try:
                with file.open('x',encoding='utf-8') as stream: json.dump(packet,stream,ensure_ascii=False,indent=2)
            except FileExistsError:
                pass  # Never overwrite an operator's edited draft.
            self.store.audit('local-operator','prepare_video_review',ident,'unsigned_draft')
            return {'request_id':ident,'file':str(file),'human_review_required':True,'published':False}

    def _signed(self,value,row):
        return verify_packet(value,row,version=VERSION,checks=CHECKS,reviewers=self.reviewers())

    def submit_file(self,file):
        value=read_review_json(file)
        ident=value.get('attestation',{}).get('request_id') if isinstance(value.get('attestation'),dict) else None
        try:
            return self.submit(value)
        except HubError as exc:
            target=ident if isinstance(ident,str) and re.fullmatch(r'[a-fA-F0-9-]{36}',ident) else 'invalid-document'
            self.store.audit('unverified','submit_video_review',target,exc.code)
            raise

    def submit(self,value):
        try: ident=value['attestation']['request_id']
        except (KeyError,TypeError): raise HubError('HUMAN_REVIEW_DOCUMENT_INVALID',review=True) from None
        with resource_guard(self.settings.root/'data/locks/heavy',self.settings.root/'data/locks/video-review'):
            row=self.request(ident)
            att=self._signed(value,row)
            principal='reviewer:'+att['reviewer_id']
            decision=att['decision']
            if decision!='revoke' and row['decision']=='pending' and row['expires_at']<time.time():
                with self.store.transaction() as db:
                    db.execute("UPDATE video_review_requests SET decision='expired' WHERE request_id=?",(ident,))
                raise HubError('HUMAN_REVIEW_REQUEST_EXPIRED',review=True)
            terminal={'approve':'accepted','reject':'rejected','revoke':'revoked'}[decision]
            if row['decision']==terminal and row['attestation']==encode(att) and row['signature']==value['signature']:
                return {'request_id':ident,'decision':terminal,'published':False,'job_queued':False}
            if row['decision']!='pending' and not (decision=='revoke' and row['decision']=='accepted'):
                raise HubError('HUMAN_REVIEW_ALREADY_DECIDED',review=True)
            job=self.store.get(row['job_id'])
            if decision=='approve':
                if not all(att['checks'].values()):
                    raise HubError('HUMAN_REVIEW_CHECKS_INCOMPLETE',review=True)
                current=self.snapshot(job,json.loads(row['manifest'])['account']['open_id'])
                if digest(current)!=row['manifest_sha256']:
                    raise HubError('HUMAN_REVIEW_INPUT_CHANGED',review=True)
            with self.store.transaction() as db:
                if decision=='approve':
                    if db.execute('SELECT 1 FROM publication_reviews WHERE job_id=?',(row['job_id'],)).fetchone():
                        raise HubError('HUMAN_REVIEW_REPLACEMENT_REQUIRES_REVOCATION',review=True)
                    current_job=db.execute('SELECT state,updated_at FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
                    if current_job['state']!='awaiting_review' or current_job['updated_at']!=job['updated_at']:
                        raise HubError('HUMAN_REVIEW_INPUT_CHANGED',review=True)
                    manifest=json.loads(row['manifest'])
                    db.execute('INSERT INTO publication_reviews VALUES(?,?,?,?,?,?,?,?)',
                        (row['job_id'],att['reviewer_id'],time.time(),manifest['source']['sha256'],
                         manifest['artifact']['sha256'],manifest['rights']['reference'],manifest['account']['open_id'],'human'))
                    db.execute('INSERT INTO video_review_bindings VALUES(?,?,?)',(row['job_id'],ident,row['manifest_sha256']))
                elif decision=='revoke':
                    binding=db.execute('SELECT request_id FROM video_review_bindings WHERE job_id=?',(row['job_id'],)).fetchone()
                    if binding and binding['request_id']==ident:
                        db.execute('DELETE FROM video_review_bindings WHERE job_id=?',(row['job_id'],))
                        db.execute('DELETE FROM publication_reviews WHERE job_id=?',(row['job_id'],))
                db.execute('''UPDATE video_review_requests SET decision=?,reviewer_id=?,attestation=?,signature=?,decided_at=?
                    WHERE request_id=?''',(terminal,att['reviewer_id'],encode(att),value['signature'],time.time(),ident))
                db.execute('INSERT INTO video_review_decisions VALUES(?,?,?,?,?,?,?)',
                    (digest([att,value['signature']]),ident,att['reviewer_id'],decision,encode(att),value['signature'],time.time()))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                           (time.time(),principal,'submit_video_review',ident,terminal))
                if decision=='reject':
                    db.execute("UPDATE jobs SET error_code='HUMAN_VIDEO_REVIEW_REJECTED',updated_at=? WHERE job_id=? AND state='awaiting_review'",
                               (time.time(),row['job_id']))
            return {'request_id':ident,'decision':terminal,'published':False,'job_queued':False}

    def verify_approval(self,job,artifact,account_id):
        rows=self.store.query('''SELECT r.*,b.manifest_sha256 binding_hash FROM video_review_requests r JOIN video_review_bindings b ON b.request_id=r.request_id
            WHERE b.job_id=? AND r.decision='accepted' ''',(job['job_id'],))
        if not rows: raise HubError('SIGNED_HUMAN_VIDEO_REVIEW_REQUIRED',review=True)
        row=rows[0]
        if row['binding_hash']!=row['manifest_sha256']:
            raise HubError('HUMAN_REVIEW_MANIFEST_MISMATCH',review=True)
        value={'manifest':json.loads(row['manifest']),'attestation':json.loads(row['attestation']),'signature':row['signature']}
        att=self._signed(value,row)
        if att['decision']!='approve' or not all(att['checks'].values()):
            raise HubError('SIGNED_HUMAN_VIDEO_REVIEW_REQUIRED',review=True)
        current=self.snapshot(job,account_id,processing=True)
        if digest(current)!=row['manifest_sha256'] or artifact!={**job['result']['processed'],'bilibili':current['submission']}:
            raise HubError('HUMAN_REVIEW_INPUT_CHANGED',review=True)
        return row['request_id']

    def queue(self,request_id,token):
        from .control import Control
        principal=Control(self.settings,self.store).authorize(token,write=True)
        try:
            return self._queue(request_id,principal)
        except HubError as exc:
            target=request_id if isinstance(request_id,str) and re.fullmatch(r'[a-fA-F0-9-]{36}',request_id) else 'invalid-request'
            self.store.audit(principal,'queue_reviewed_video',target,exc.code)
            raise

    def _queue(self,request_id,principal):
        self.request(request_id)
        cfg=self.settings.config
        if self.settings.dry_run or not cfg['auto_publish'] or not cfg['first_publish_accepted'] or cfg['bilibili_publish_mode']!='official_video':
            raise HubError('FIRST_PUBLISH_ACCEPTANCE_REQUIRED',review=True)
        with resource_guard(self.settings.root/'data/locks/heavy',self.settings.root/'data/locks/video-review'):
            row=self.request(request_id)
            job=self.store.get(row['job_id'])
            if row['decision']!='accepted':
                raise HubError('SIGNED_HUMAN_VIDEO_REVIEW_REQUIRED',review=True)
            if row['queued_at'] is not None or job['state']!='awaiting_review':
                raise HubError('HUMAN_REVIEW_JOB_NOT_QUEUEABLE',review=True)
            manifest=json.loads(row['manifest'])
            artifact={**job['result'].get('processed',{}),'bilibili':manifest['submission']}
            if self.verify_approval(job,artifact,manifest['account']['open_id'])!=request_id:
                raise HubError('SIGNED_HUMAN_VIDEO_REVIEW_REQUIRED',review=True)
            with self.store.transaction() as db:
                publication=db.execute('SELECT state,external_id FROM publications WHERE job_id=?',(job['job_id'],)).fetchone()
                if publication and (publication['state']!='rejected' or publication['external_id']):
                    raise HubError('RETRY_REQUIRES_REVIEW_OR_RECONCILIATION',review=True)
                changed=db.execute("UPDATE jobs SET state='pending',attempts=0,next_run=0,error_code=NULL,updated_at=? WHERE job_id=? AND state='awaiting_review' AND updated_at=?",
                    (time.time(),job['job_id'],job['updated_at'])).rowcount
                if not changed: raise HubError('HUMAN_REVIEW_INPUT_CHANGED',review=True)
                db.execute('UPDATE video_review_requests SET queued_at=? WHERE request_id=?',(time.time(),request_id))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                           (time.time(),principal,'queue_reviewed_video',job['job_id'],'pending'))
            return {'job_id':job['job_id'],'state':'pending','published':False}
