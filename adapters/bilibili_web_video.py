"""Disabled-by-default website video transport prototype; never content approval.

Implements the existing Publishing transport interface without changing its
signed human-video review path or treating a website UID as an app openid.
No scheduler, CLI, MCP endpoint, browser launcher, cookies or live UI profile.
"""
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import time

from adapters.bilibili import BilibiliOfficial
from services.hub.db import encode
from services.hub.errors import HubError,PublishRejected,PublishUnknown
from services.hub.evidence import sha256_file
from services.hub.locks import resource_guard
from services.hub.maintenance import Maintenance
from services.hub.review_protocol import digest
from services.hub.video_reviews import VideoReviews

VERSION='website-video-upload-attempt-v1'
NAMESPACE='website_video_upload_v1'
BV=re.compile(r'BV[A-Za-z0-9]{10}')


class BilibiliWebsiteVideo:
    """Internal transport only. Live website-specific authorization is not supplied.

    driver is an already-scoped page transport. authorization must independently
    verify a website-video binding at each write boundary. Passing an old
    human-video-review-v1/openid document is not a supported authorization.
    """
    def __init__(self,settings,store,job,driver,*,authorization=None,probe=None):
        self.settings,self.store,self.job,self.driver=settings,store,deepcopy(job),driver
        self.authorization,self.probe=authorization,probe

    def _current(self,*,leased=False):
        current=self.store.get(self.job.get('job_id'))
        if (not current or current['source_platform']!='youtube' or current['job_type']!='video_process' or
            current['source_id']!=self.job['source_id'] or current['payload']!=self.job['payload']):
            raise HubError('WEB_VIDEO_JOB_INPUT_CHANGED')
        if (not re.fullmatch(r'[A-Za-z0-9_-]{11}',current['source_id']) or
            current['payload'].get('video_id')!=current['source_id'] or
            current['payload'].get('url')!='https://www.youtube.com/watch?v='+current['source_id']):
            raise HubError('WEB_VIDEO_SOURCE_IDENTITY_INVALID')
        if leased and (current['state']!='processing' or not self.job.get('lease_owner') or
            current.get('lease_owner')!=self.job['lease_owner'] or (current.get('lease_until') or 0)<=time.time()):
            raise HubError('WEB_VIDEO_PARENT_LEASE_LOST')
        return current

    def _write_policy(self):
        cfg=self.settings.config
        if self.settings.dry_run:raise HubError('DRY_RUN_PUBLISH_BLOCKED',review=True)
        if (cfg.get('web_video_prototype_enabled') is not True or cfg.get('bilibili_publish_mode')!='website_video_prototype' or
            cfg.get('auto_publish') is not True or cfg.get('first_publish_accepted') is not True):
            raise HubError('WEB_VIDEO_ROUTE_NOT_ENABLED',review=True)
        if self.authorization is None or not callable(getattr(self.authorization,'verify',None)):
            raise HubError('WEBSITE_VIDEO_AUTHORIZATION_REQUIRED',review=True)

    def _uid(self):
        uid=self.settings.config.get('target_bilibili_site_uid')
        if not isinstance(uid,str) or not re.fullmatch(r'[1-9][0-9]{0,19}',uid):
            raise HubError('WEB_VIDEO_TARGET_UID_INVALID')
        return uid

    def _account(self):
        value=self.driver.accountIdentity()
        if (not isinstance(value,dict) or value.get('site_uid')!=self._uid() or
            value.get('name')!=self.settings.config['target_bilibili_account_name']):
            raise HubError('TARGET_ACCOUNT_IDENTITY_MISMATCH',review=True)
        return 'site_uid:'+value['site_uid']

    def account_identity(self):
        self._write_policy()
        return self._account()

    def _key(self,current):
        return digest(['bilibili',current['source_platform'],current['source_id'],'site_uid:'+self._uid(),'video'])

    def _binding(self,key,artifact):
        current=self._current(leased=True)
        if key!=self._key(current):raise HubError('INVALID_PUBLICATION_KEY')
        # Reuse per-video grant parsing and strict prepared-file/metadata checks.
        # Neither the owner's broad scope statement nor a browser login supplies
        # this missing per-video acquisition/integrity binding automatically.
        review=VideoReviews(self.settings,self.store,probe=self.probe)
        grant=review._grant(current['source_id'])
        source=review._file(current['job_id'],current['result'].get('source_asset',{}).get('file'))
        file,size,metadata=BilibiliOfficial(self.settings,self.store,probe=self.probe)._artifact(artifact)
        if review._file(current['job_id'],str(file))!=file:raise HubError('PUBLISH_ARTIFACT_PATH_INVALID')
        if (current['result'].get('source_sha256')!=grant.get('source_sha256') or
            sha256_file(source)!=grant.get('source_sha256') or current['result'].get('rights_evidence')!=grant['evidence'] or
            grant.get('bilibili')!=artifact.get('bilibili') or current['result'].get('processed_parts') or
            metadata['source']!=current['payload']['url']):
            raise HubError('WEB_VIDEO_RIGHTS_ARTIFACT_OR_MULTIPART_NOT_BOUND',review=True)
        if (not isinstance(current['result'].get('processed'),dict) or
            Path(current['result']['processed'].get('file','')).resolve()!=file or
            current['result']['processed'].get('sha256')!=artifact['sha256']):
            raise HubError('WEB_VIDEO_PREPARED_ARTIFACT_REQUIRED',review=True)
        binding={'version':'website-video-binding-v1','job_id':current['job_id'],'source_id':current['source_id'],
            'source_sha256':grant['source_sha256'],'artifact_sha256':artifact['sha256'],'file_bytes':size,
            'metadata_sha256':digest(metadata),'rights_grant_sha256':digest(grant),'site_uid':self._uid(),
            'account_kind':'website_site_uid_not_official_openid'}
        return current,binding,file,metadata

    def _authorize(self,binding,boundary):
        value=self.authorization.verify(deepcopy(binding),boundary=boundary)
        if (not isinstance(value,dict) or set(value)!={'version','binding_sha256','reference'} or
            value.get('version')!='website-video-authorization-v1' or value.get('binding_sha256')!=digest(binding) or
            not isinstance(value.get('reference'),str) or not value['reference'].strip() or len(value['reference'])>256):
            raise HubError('WEBSITE_VIDEO_AUTHORIZATION_INVALID',review=True)
        return value

    def _save(self,current,value):
        self.store.checkpoint(current,current['stage'],{**current['result'],NAMESPACE:value})
        self.store.audit('internal-website-video-transport','checkpoint',current['job_id'],value['state'])

    def _known(self,current,key):
        value=current['result'].get(NAMESPACE)
        if value is not None and (not isinstance(value,dict) or value.get('version')!=VERSION or value.get('key')!=key):
            raise HubError('WEB_VIDEO_CHECKPOINT_INVALID')
        return value

    def prepare(self,key,artifact):
        self._write_policy()
        if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{64}',key):raise HubError('INVALID_PUBLICATION_KEY')
        with resource_guard(self.settings.root/'data/locks/heavy',self.settings.root/('data/locks/web-video-'+key)):
            current,binding,file,metadata=self._binding(key,artifact)
            grant=self._authorize(binding,'private_upload')
            self._account()
            old=self._known(current,key)
            if old and old.get('binding')!=binding:raise HubError('WEB_VIDEO_UPLOAD_INPUT_CHANGED')
            if old:
                if old['state'] in ('uploaded','submitting','submit_unknown','awaiting_moderation','completed','failed'):
                    return {'private_upload_ready':True,'checkpoint_reused':True}
                # A native filename/progress label cannot establish server-side
                # file identity after a lost upload result. Never select it again.
                raise HubError('WEB_VIDEO_UPLOAD_RESULT_UNKNOWN',review=True)
            existing=self.store.query("SELECT * FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=? AND kind='video' AND state!='rejected'",(current['source_id'],))
            if existing:raise HubError('WEB_VIDEO_EXISTING_PUBLICATION_REQUIRES_LOOKUP')
            Maintenance(self.settings,self.store).check_disk(64_000_000)
            attempt={'version':VERSION,'key':key,'binding':binding,'authorization_reference':grant['reference'],
                'state':'uploading','human_review':False,'authorization_granted_by_transport':False}
            self._save(current,attempt)
            try:
                receipt=self.driver.prepareUpload({'key':key,'file':str(file),**{k:binding[k] for k in ('artifact_sha256','file_bytes')},'metadata':metadata})
                required={'version','key','artifact_sha256','file_bytes','site_uid','upload_reference','evidence_kind','server_file_hash_verified'}
                if (not isinstance(receipt,dict) or set(receipt)!=required or receipt['version']!='website-video-upload-receipt-v1' or
                    any(receipt[k]!=binding[k] for k in ('artifact_sha256','file_bytes','site_uid')) or receipt['key']!=key or
                    receipt['evidence_kind']!='same_browser_attempt_observation' or receipt['server_file_hash_verified'] is not False or
                    not isinstance(receipt['upload_reference'],str) or not 0<len(receipt['upload_reference'])<=512):
                    raise HubError('WEB_VIDEO_UPLOAD_RECEIPT_INVALID')
                # A future native upload reference might itself be private. Its
                # hash is enough for the page driver to recheck the visible
                # draft; do not put the raw reference in jobs/MCP/logs/backups.
                stored_receipt={k:v for k,v in receipt.items() if k!='upload_reference'}
                stored_receipt['upload_reference_sha256']=hashlib.sha256(receipt['upload_reference'].encode()).hexdigest()
                self._save(self._current(leased=True),{**attempt,'state':'uploaded','upload_receipt':stored_receipt})
                return {'private_upload_ready':True,'checkpoint_reused':False}
            except BaseException:
                self._save(self._current(leased=True),{**attempt,'state':'upload_unknown','error_code':'WEB_VIDEO_UPLOAD_RESULT_UNKNOWN'})
                raise

    def send(self,key,artifact):
        raise PublishRejected('WEB_VIDEO_REVALIDATION_CALLBACK_REQUIRED',review=True)

    def send_reviewed(self,key,artifact,revalidate):
        # Compatible with Publishing._dispatch, whose real submit() review gate
        # is unchanged. This transport additionally requires a website binding.
        try:
            self._write_policy()
            if not callable(revalidate):raise HubError('WEB_VIDEO_REVALIDATION_CALLBACK_REQUIRED')
            revalidate()
            current,binding,_,_=self._binding(key,artifact)
            self._authorize(binding,'public_submit');self._account()
            old=self._known(current,key)
            if not old or old.get('binding')!=binding:raise HubError('WEB_VIDEO_UPLOAD_CHECKPOINT_REQUIRED')
            if old['state']!='uploaded':raise PublishUnknown()
        except PublishUnknown:raise
        except HubError as exc:raise PublishRejected(exc.code,review=exc.review) from None
        with resource_guard(self.settings.root/'data/locks/heavy',self.settings.root/('data/locks/web-video-'+key)):
            # A final local check stays before the one allowed submit click.
            revalidate();current,binding,_,_=self._binding(key,artifact);self._authorize(binding,'public_submit');self._account()
            old=self._known(current,key)
            if not old or old['state']!='uploaded' or old.get('binding')!=binding:raise PublishUnknown()
            self._save(current,{**old,'state':'submitting'})
            attempt=old
            try:
                result=self.driver.submit({'key':key,'upload_receipt':old['upload_receipt']})
                observed=self._reference(result)
                if observed:
                    # A BV observed in the same one-click attempt is useful
                    # for read-only reconciliation even if moderation is not
                    # readable yet. It is not an accepted/completed result.
                    attempt={**old,'observed_submission':observed}
                result=self._result(result)
                self._save(self._current(leased=True),{**attempt,'state':result['state'],'submission_receipt':result})
                return result
            except BaseException:
                self._save(self._current(leased=True),{**attempt,'state':'submit_unknown','error_code':'PUBLISH_UNKNOWN'})
                raise

    @staticmethod
    def _reference(value):
        if (isinstance(value,dict) and isinstance(value.get('external_id'),str) and BV.fullmatch(value['external_id']) and
            value.get('url')=='https://www.bilibili.com/video/'+value['external_id']):
            return {k:value[k] for k in ('external_id','url')}
        return None

    @staticmethod
    def _result(value):
        if (not BilibiliWebsiteVideo._reference(value) or
            value.get('state') not in ('awaiting_moderation','completed','failed') or
            (value['state']=='failed' and value.get('error_code')!='BILIBILI_MODERATION_REJECTED')):
            raise PublishUnknown()
        return {k:value[k] for k in ('state','external_id','url','error_code') if k in value}

    def lookup(self,key,external_id=None):
        # Deliberately read-only and independent of current write switches,
        # current grant validity or an expired worker lease.
        current=self._current()
        if key!=self._key(current):raise HubError('INVALID_PUBLICATION_KEY')
        with resource_guard(self.settings.root/'data/locks/heavy',self.settings.root/('data/locks/web-video-'+key)):
            self._account();old=self._known(current,key)
            recorded=old.get('submission_receipt') or old.get('observed_submission') if old else None
            if recorded:
                recorded=self._reference(recorded)
                if not recorded:raise HubError('WEB_VIDEO_CHECKPOINT_INVALID')
                if external_id and external_id!=recorded['external_id']:raise HubError('WEB_VIDEO_BV_BINDING_MISMATCH')
                external_id=recorded['external_id']
            # A caller-supplied BV without a recorded receipt is not file identity.
            if not recorded:return {'state':'unknown'}
            value=self.driver.lookup({'key':key,'external_id':external_id})
            result=self._result(value)
            if result['external_id']!=external_id:raise PublishUnknown()
            return result
