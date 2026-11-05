"""Official ARC_BASE video protocol; never enables credentials or publishing itself."""
from pathlib import Path
import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from cryptography.fernet import Fernet, InvalidToken
from services.hub.db import encode
from services.hub.errors import HubError, PublishRejected, PublishUnknown
from services.hub.evidence import sha256_file
from services.hub.locks import resource_guard
from .video import VideoProcessor

MEMBER = 'https://member.bilibili.com/arcopen/fn/'
PARTS = 'https://openupos.bilivideo.com/video/v2/part/upload'
BV = re.compile(r'BV[A-Za-z0-9]{10}')
NOT_ACCEPTED = {'AUTH_EXPIRED','BILIBILI_SCOPE_REQUIRED','BILIBILI_APP_APPROVAL_REQUIRED',
    'BILIBILI_ACCOUNT_AUTHORIZATION_REQUIRED','BILIBILI_SIGNATURE_REJECTED',
    'BILIBILI_HUMAN_VERIFICATION_REQUIRED','BILIBILI_CONTENT_REJECTED','API_RATE_LIMITED',
    'PERMISSION_DENIED','BILIBILI_INVALID_SUBMISSION',
    'API_BUDGET_EXCEEDED','PAID_SERVICE_NOT_AUTHORIZED','INSUFFICIENT_BALANCE','INVALID_BUDGET'}


def signed_headers(app_id, app_secret, access_token, content=b'', *, timestamp=None, nonce=None):
    fields = {'x-bili-accesskeyid':app_id,
        'x-bili-content-md5':hashlib.md5(content,usedforsecurity=False).hexdigest(),
        'x-bili-signature-method':'HMAC-SHA256','x-bili-signature-nonce':nonce or uuid.uuid4().hex,
        'x-bili-signature-version':'2.0','x-bili-timestamp':str(int(time.time() if timestamp is None else timestamp))}
    canonical = '\n'.join(key + ':' + fields[key] for key in sorted(fields))
    return {**fields,'Authorization':hmac.new(app_secret.encode(),canonical.encode(),hashlib.sha256).hexdigest(),
        'Access-Token':access_token,'Content-Type':'application/json','Accept':'application/json'}


def check_response(value):
    if not isinstance(value,dict) or type(value.get('code')) is not int:
        raise HubError('BILIBILI_INVALID_RESPONSE',review=True)
    code=value['code']
    if code==0:
        return
    if code in (127001,127000,127023):
        raise HubError('AUTH_EXPIRED',review=True)
    if code in (127007,127304,127305):
        raise HubError('BILIBILI_SCOPE_REQUIRED',review=True)
    if code in (127005,127006):
        raise HubError('BILIBILI_APP_APPROVAL_REQUIRED',review=True)
    if code in (127003,127008,127011):
        raise HubError('BILIBILI_ACCOUNT_AUTHORIZATION_REQUIRED',review=True)
    if code in (4002,4003,4005,4006,4008,127002):
        raise HubError('BILIBILI_SIGNATURE_REJECTED',review=True)
    if code in (127009,127306,123026):
        raise HubError('API_RATE_LIMITED',retryable=True,retry_after=60)
    if code in (123042,123050,123051):
        raise HubError('BILIBILI_HUMAN_VERIFICATION_REQUIRED',review=True)
    if code in (123024,123052):
        raise HubError('BILIBILI_CONTENT_REJECTED',review=True)
    if code in (4000,4001,123008,123009,123010,123012,123013,123014,123016,123017,123018,123019,123020,123030):
        raise HubError('BILIBILI_INVALID_SUBMISSION',review=True)
    if code in (4010,4011,123002,123028,123039):
        raise HubError('BILIBILI_SERVER_PROCESSING',retryable=True,retry_after=60)
    # An undocumented or duplicate-title error does not prove a post was never accepted.
    raise HubError('BILIBILI_UNCLASSIFIED_RESULT',review=True)


class BilibiliOfficial:
    def __init__(self, settings=None, store=None, client=None, *, probe=None, access_token_override=None):
        self.settings,self.store,self.client=settings,store,client
        self.probe=probe or (VideoProcessor(settings.root).probe if settings else None)
        self.access_token_override=access_token_override

    def credentials(self):
        names=('BILIBILI_ACCESS_KEY_ID','BILIBILI_ACCESS_KEY_SECRET','BILIBILI_ACCESS_TOKEN','BILIBILI_ACCOUNT_ID')
        values={key:os.getenv(key,'') for key in names}
        if self.access_token_override is not None:
            values['BILIBILI_ACCESS_TOKEN']=self.access_token_override
        elif self.settings is not None and self.store is not None:
            from services.hub.bilibili_tokens import BilibiliTokens
            managed=BilibiliTokens(self.settings,self.store).access_token(
                values['BILIBILI_ACCOUNT_ID'],values['BILIBILI_ACCESS_KEY_ID'])
            if managed is not None:
                values['BILIBILI_ACCESS_TOKEN']=managed
        if any(not re.fullmatch(r'[!-~]{1,4096}',value) for value in values.values()):
            raise HubError('BILIBILI_CREDENTIAL_AND_ACCOUNT_REQUIRED',review=True)
        return values

    def _call(self, method, path, *, body=None, params=None):
        allowed={'user/account/info','user/account/scopes','archive/video/init','archive/video/complete',
                 'archive/add-by-utoken','archive/view','archive/viewlist'}
        if path not in allowed or self.client is None:
            raise HubError('BILIBILI_API_NOT_CONFIGURED',review=True)
        c=self.credentials()
        content=b'' if body is None else encode(body).encode('utf-8')
        headers=signed_headers(c['BILIBILI_ACCESS_KEY_ID'],c['BILIBILI_ACCESS_KEY_SECRET'],c['BILIBILI_ACCESS_TOKEN'],content)
        value=self.client.request('bilibili',method,MEMBER+path,headers=headers,params=params,
            content=content,response_check=check_response)
        return value.get('data') or {}

    def account_identity(self):
        if self.settings is None or self.store is None:
            raise HubError('BILIBILI_SCOPE_AND_ACCOUNT_AUTHORIZATION_REQUIRED',review=True)
        c=self.credentials()
        expected=c['BILIBILI_ACCOUNT_ID']
        if expected not in self.settings.config['authorized_bilibili_accounts']:
            raise HubError('TARGET_ACCOUNT_NOT_AUTHORIZED',review=True)
        account=self._call('GET','user/account/info')
        permissions=self._call('GET','user/account/scopes')
        scopes=permissions.get('scopes',[])
        if account.get('openid')!=expected or permissions.get('openid')!=expected:
            raise HubError('TARGET_ACCOUNT_IDENTITY_MISMATCH',review=True)
        if account.get('name')!=self.settings.config.get('target_bilibili_account_name'):
            raise HubError('TARGET_ACCOUNT_NAME_MISMATCH',review=True)
        if (not isinstance(scopes,list) or any(not isinstance(s,str) for s in scopes)
                or not {'USER_INFO','ARC_BASE'} <= set(scopes)):
            raise HubError('BILIBILI_SCOPE_REQUIRED',review=True)
        with self.store.transaction() as db:
            db.execute('INSERT OR REPLACE INTO bilibili_accounts VALUES(?,?,?,?,?)',
                (expected,c['BILIBILI_ACCESS_KEY_ID'],account['name'],encode(sorted(scopes)),time.time()))
        return expected

    def _write_policy(self):
        if self.settings is None or self.store is None or self.settings.dry_run:
            raise HubError('DRY_RUN_PUBLISH_BLOCKED',review=True)
        cfg=self.settings.config
        if (cfg.get('bilibili_publish_mode')!='official_video' or not cfg.get('auto_publish')
                or not cfg.get('first_publish_accepted')):
            raise HubError('FIRST_PUBLISH_ACCEPTANCE_REQUIRED',review=True)

    def _cipher(self):
        try:
            master=base64.urlsafe_b64decode(os.environ['EVENT_ENCRYPTION_KEY'])
            if len(master)!=32:
                raise ValueError()
            key=hmac.new(master,b'lighthouse-bilibili-upload-token-v1',hashlib.sha256).digest()
            return Fernet(base64.urlsafe_b64encode(key))
        except (KeyError,ValueError):
            raise HubError('UPLOAD_ENCRYPTION_KEY_REQUIRED',review=True) from None

    def _get(self,key):
        if not re.fullmatch(r'[0-9a-f]{64}',key):
            raise HubError('INVALID_PUBLICATION_KEY',review=True)
        rows=self.store.query('SELECT * FROM bilibili_uploads WHERE publication_key=?',(key,))
        return rows[0] if rows else None

    def _update(self,key,**fields):
        if not set(fields) <= {'state','encrypted_upload_token','next_part','resource_id','last_error_code'}:
            raise HubError('UPLOAD_CHECKPOINT_FIELD_INVALID',review=True)
        with self.store.transaction() as db:
            db.execute('UPDATE bilibili_uploads SET '+','.join(name+'=?' for name in fields)+
                ',updated_at=? WHERE publication_key=?',(*fields.values(),time.time(),key))

    def _token(self,row):
        try:
            return self._cipher().decrypt(row['encrypted_upload_token'].encode()).decode('utf-8')
        except (InvalidToken,AttributeError,UnicodeError):
            raise HubError('BILIBILI_UPLOAD_TOKEN_DECRYPTION_FAILED',review=True) from None

    def _artifact(self,artifact):
        source=Path(artifact.get('file',''))
        file=source.resolve()
        if source.is_symlink() or not file.is_relative_to((self.settings.root/'media').resolve()) or not file.is_file():
            raise HubError('PUBLISH_ARTIFACT_PATH_INVALID',review=True)
        size=file.stat().st_size
        if not 0<size<=4_000_000_000:
            raise HubError('BILIBILI_VIDEO_REQUIRES_SEGMENTATION',review=True)
        if artifact.get('sha256')!=sha256_file(file):
            raise HubError('PUBLISH_ARTIFACT_HASH_MISMATCH',review=True)
        if not 0<self.probe(file)<18000:
            raise HubError('BILIBILI_VIDEO_REQUIRES_SEGMENTATION',review=True)
        metadata=artifact.get('bilibili')
        allowed={'title','tid','tag','desc','copyright','source','no_reprint'}
        if not isinstance(metadata,dict) or set(metadata)-allowed:
            raise HubError('BILIBILI_SUBMISSION_METADATA_INVALID',review=True)
        if (not isinstance(metadata.get('title'),str) or not 0<len(metadata['title'])<80 or
                type(metadata.get('tid')) is not int or metadata['tid']<1 or
                not isinstance(metadata.get('tag'),str) or not 0<len(metadata['tag'])<200 or
                not isinstance(metadata.get('desc'),str) or len(metadata['desc'])>=250 or
                metadata.get('copyright')!=2 or
                not re.fullmatch(r'https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}',metadata.get('source','')) or
                metadata['source'] not in metadata['desc'] or
                type(metadata.get('no_reprint',1)) is not int or metadata.get('no_reprint',1) not in (0,1)):
            raise HubError('BILIBILI_SUBMISSION_METADATA_INVALID',review=True)
        return file,size,{**metadata,'no_reprint':metadata.get('no_reprint',1)}

    def prepare(self,key,artifact):
        """Private upload only; no public submission or publication record yet."""
        self._write_policy()
        self.account_identity()
        self._cipher()
        file,size,metadata=self._artifact(artifact)
        self._get(key)
        with resource_guard(self.settings.root/'data/locks/heavy',self.settings.root/('data/locks/bili-'+key)):
            row=self._get(key)
            if row and (row['artifact_sha256']!=artifact['sha256'] or row['file_bytes']!=size or
                    row['metadata']!=encode(metadata) or row['account_id']!=os.environ['BILIBILI_ACCOUNT_ID']):
                raise HubError('BILIBILI_UPLOAD_INPUT_CHANGED',review=True)
            if row and row['state'] in ('uploaded','submitted','submitting','submit_unknown','rejected'):
                return {'private_upload_ready':True}
            if row and row['state'] in ('initializing','init_unknown','merging','merge_unknown'):
                raise HubError('BILIBILI_PRIVATE_UPLOAD_RESULT_REQUIRES_REVIEW',review=True)
            if not row:
                part_bytes=10*1024*1024
                now=time.time()
                with self.store.transaction() as db:
                    db.execute('''INSERT INTO bilibili_uploads(publication_key,account_id,artifact_sha256,file_bytes,
                        metadata,state,part_bytes,created_at,updated_at) VALUES(?,?,?,?,?,'initializing',?,?,?)''',
                        (key,os.environ['BILIBILI_ACCOUNT_ID'],artifact['sha256'],size,encode(metadata),part_bytes,now,now))
                try:
                    result=self._call('POST','archive/video/init',body={'name':file.name,'utype':'0'})
                    token=result.get('upload_token')
                    if not isinstance(token,str) or not 1<=len(token)<=4096:
                        raise HubError('BILIBILI_UPLOAD_TOKEN_MISSING',review=True)
                    encrypted=self._cipher().encrypt(token.encode()).decode()
                    self._update(key,state='initialized',encrypted_upload_token=encrypted,last_error_code=None)
                except HubError as exc:
                    if exc.code in NOT_ACCEPTED:
                        with self.store.transaction() as db:
                            db.execute('DELETE FROM bilibili_uploads WHERE publication_key=?',(key,))
                    else:
                        self._update(key,state='init_unknown',last_error_code=exc.code)
                    raise
                except BaseException:
                    self._update(key,state='init_unknown',last_error_code='BILIBILI_UPLOAD_INIT_INTERRUPTED')
                    raise
                row=self._get(key)
            token=self._token(row)
            self._update(key,state='uploading')
            with file.open('rb') as stream:
                stream.seek((row['next_part']-1)*row['part_bytes'])
                part=row['next_part']
                while chunk:=stream.read(row['part_bytes']):
                    try:
                        self.client.request('bilibili','POST',PARTS,
                            params={'upload_token':token,'part_number':part},
                            headers={'Content-Type':'application/octet-stream','Accept':'application/json'},
                            content=chunk,response_check=check_response)
                    except HubError as exc:
                        self._update(key,last_error_code=exc.code)
                        raise
                    with self.store.transaction() as db:
                        db.execute('INSERT OR REPLACE INTO bilibili_parts VALUES(?,?,?,?,?)',
                            (key,part,hashlib.sha256(chunk).hexdigest(),len(chunk),time.time()))
                        db.execute('UPDATE bilibili_uploads SET next_part=?,updated_at=? WHERE publication_key=?',
                            (part+1,time.time(),key))
                    part+=1
            if file.stat().st_size!=size or sha256_file(file)!=artifact['sha256']:
                raise HubError('PUBLISH_ARTIFACT_CHANGED_DURING_UPLOAD',review=True)
            self._update(key,state='merging')
            try:
                self._call('POST','archive/video/complete',params={'upload_token':token})
                self._update(key,state='uploaded',last_error_code=None)
            except HubError as exc:
                self._update(key,state='uploading' if exc.code in NOT_ACCEPTED else 'merge_unknown',last_error_code=exc.code)
                raise
            except BaseException:
                self._update(key,state='merge_unknown',last_error_code='BILIBILI_MERGE_RESULT_UNKNOWN')
                raise
        return {'private_upload_ready':True}

    def send_reviewed(self,key,artifact,revalidate):
        return self.send(key,artifact,before_submit=revalidate)

    def send(self,key,artifact,*,before_submit=None):
        try:
            self._write_policy()
            self.account_identity()
            row=self._get(key)
            if not row or row['state'] not in ('uploaded','rejected'):
                raise PublishUnknown()
            if before_submit is not None:
                try:
                    before_submit()
                except HubError as exc:
                    self._update(key,state='rejected',last_error_code=exc.code)
                    raise PublishRejected(exc.code,review=exc.review,retryable=exc.retryable,retry_after=exc.retry_after) from None
            self._update(key,state='submitting')
            result=self._call('POST','archive/add-by-utoken',body=json.loads(row['metadata']),
                              params={'upload_token':self._token(row)})
            resource=result.get('resource_id','')
            if not isinstance(resource,str) or not BV.fullmatch(resource):
                raise PublishUnknown()
            self._update(key,state='submitted',resource_id=resource,last_error_code=None)
            return {'state':'awaiting_moderation','external_id':resource,'url':'https://www.bilibili.com/video/'+resource}
        except PublishRejected:
            raise
        except HubError as exc:
            if exc.code in NOT_ACCEPTED:
                self._update(key,state='rejected',last_error_code=exc.code)
                raise PublishRejected(exc.code,review=exc.review,retryable=exc.retryable,retry_after=exc.retry_after) from None
            self._update(key,state='submit_unknown',last_error_code=exc.code)
            raise PublishUnknown() from None
        except BaseException:
            self._update(key,state='submit_unknown',last_error_code='PUBLISH_UNKNOWN')
            raise

    def lookup(self,key,external_id=None):
        self.account_identity()
        row=self._get(key)
        resource=external_id or (row and row['resource_id'])
        if not resource:
            # No official lookup by client idempotency key. Matching captions cannot prove file identity.
            self._call('GET','archive/viewlist',params={'pn':1,'ps':50,'status':'all'})
            return {'state':'unknown','error_code':'BILIBILI_RESOURCE_ID_UNCONFIRMED'}
        if not isinstance(resource,str) or not BV.fullmatch(resource):
            raise HubError('BILIBILI_RESOURCE_ID_INVALID',review=True)
        result=self._call('GET','archive/view',params={'resource_id':resource})
        if result.get('resource_id')!=resource or type(result.get('addit_info',{}).get('state')) is not int:
            return {'state':'unknown','error_code':'BILIBILI_RESULT_NOT_VERIFIED'}
        info=result['addit_info']
        state='completed' if info['state']==0 else 'failed' if info.get('reject_reason') else 'awaiting_moderation'
        return {'state':state,'external_id':resource,'url':'https://www.bilibili.com/video/'+resource,
            'platform_state':info['state'],'error_code':'BILIBILI_MODERATION_REJECTED' if state=='failed' else None}
