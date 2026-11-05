"""Bind actual visible upload limits and a correlated native UID observation.

The original DOM/identity/image bytes remain evidence, never a platform file
hash. A new live native UID/limit check is still required before selection.
"""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from .errors import HubError

VERSION='bilibili-highest-native-limit-binding-v2'


def fail():
    raise HubError('REPLAY_HIGHEST_NATIVE_LIMIT_EVIDENCE_REQUIRED',review=True)


def _read(file,maximum,sha=None):
    if (file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)) or
            not file.is_file() or not 0<file.stat().st_size<=maximum or
            os.name=='posix' and file.stat().st_mode & 0o022):
        fail()
    before=file.stat();raw=file.read_bytes();after=file.stat()
    actual=hashlib.sha256(raw).hexdigest()
    if (len(raw)!=before.st_size or any(getattr(before,k)!=getattr(after,k) for k in
            ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) or sha is not None and actual!=sha):
        fail()
    return raw,{'file':str(file),'bytes':len(raw),'sha256':actual}


def native_limit_evidence(settings,policy):
    try:
        file=settings.root/'config/replay-highest-native-limits-v2/binding.json'
        expected=policy['native_upload_limits_sha256']
        if not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected):fail()
        raw,reference=_read(file,65536,expected);record=json.loads(raw)
        fields={'version','site_uid','url','observed_at','account_correlation_reference',
                'dom','account_identity','screenshot','max_file_bytes','max_duration_seconds',
                'server_file_hash_verified','human_review'}
        if (set(record)!=fields or record['version']!=VERSION or record['site_uid']!=policy['target_site_uid'] or
                record['site_uid']!=settings.config['target_bilibili_site_uid'] or
                record['url']!='https://member.bilibili.com/platform/upload/video/frame' or
                record['max_file_bytes']!=16_000_000_000 or type(record['max_file_bytes']) is not int or
                record['max_duration_seconds']!=36000 or type(record['max_duration_seconds']) is not int or
                record['server_file_hash_verified'] is not False or record['human_review'] is not False or
                not isinstance(record['account_correlation_reference'],str) or not 0<len(record['account_correlation_reference'].strip())<=2048):
            fail()
        instant=datetime.fromisoformat(record['observed_at'].replace('Z','+00:00'))
        if instant.tzinfo is None or instant.timestamp()>datetime.now(timezone.utc).timestamp()+30:fail()
        bodies={}
        for key,name,maximum in (('dom','native.json',65536),('account_identity','account.json',16384),('screenshot','native-image.bin',20_000_000)):
            ref=record[key]
            if not isinstance(ref,dict) or set(ref)!={'file','bytes','sha256'} or ref['file']!=str(file.parent/name) or type(ref['bytes']) is not int or not re.fullmatch('[a-f0-9]{64}',ref.get('sha256','')):
                fail()
            body,actual=_read(file.parent/name,maximum,ref['sha256'])
            if actual!=ref:fail()
            bodies[key]=body
        dom=json.loads(bodies['dom']);account=json.loads(bodies['account_identity'])
        if (dom.get('url')!=record['url'] or dom.get('observed_at')!=record['observed_at'] or
                not isinstance(dom.get('body'),str) or '视频大小16G以内，时长10小时以内' not in dom['body'] or
                account.get('version')!='bilibili-native-uid-operator-association-v2' or
                account.get('site_uid')!=record['site_uid'] or account.get('method')!='existing_native_uid_record' or
                account.get('identity_context') not in ('server_bilibili_video','local_iab') or
                account.get('limits_context') not in ('server_bilibili_video','local_iab') or
                type(account.get('same_context_as_limits')) is not bool or
                account['same_context_as_limits'] and account['identity_context']!=account['limits_context'] or
                not isinstance(account.get('operator_reference'),str) or not 0<len(account['operator_reference'].strip())<=2048):
            fail()
        identity_ref=account.get('raw_identity')
        if (not isinstance(identity_ref,dict) or set(identity_ref)!={'file','bytes','sha256'} or
                identity_ref['file']!=str(file.parent/'account-raw.json') or type(identity_ref['bytes']) is not int or
                not re.fullmatch('[a-f0-9]{64}',identity_ref.get('sha256',''))):fail()
        identity_bytes,actual_identity=_read(file.parent/'account-raw.json',16384,identity_ref['sha256'])
        raw_identity=json.loads(identity_bytes)
        if (actual_identity!=identity_ref or raw_identity.get('site_uid')!=record['site_uid'] or
                raw_identity.get('method')!='visible_native_avatar_href' or raw_identity.get('same_browser_context') is not True):fail()
        image=bodies['screenshot']
        mime='image/png' if len(image)>=33 and image[:16]==b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' else 'image/jpeg' if len(image)>=4 and image[:2]==b'\xff\xd8' and image[-2:]==b'\xff\xd9' else None
        if mime is None:fail()
        return {'version':VERSION,'binding':reference,'site_uid':record['site_uid'],'url':record['url'],
                'observed_at':record['observed_at'],'max_file_bytes':record['max_file_bytes'],
                'max_duration_seconds':record['max_duration_seconds'],'dom':record['dom'],
                'account_identity':record['account_identity'],'raw_identity':identity_ref,
                'identity_context':account['identity_context'],'limits_context':account['limits_context'],
                'same_context_as_limits':account['same_context_as_limits'],'screenshot':{**record['screenshot'],'mime_type':mime},
                'account_correlation_reference':record['account_correlation_reference'],
                'fresh_native_uid_and_limit_check_required':True,'server_file_hash_verified':False,'human_review':False}
    except HubError:
        raise
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError):
        fail()
