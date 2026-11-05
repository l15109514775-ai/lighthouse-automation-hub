"""Acknowledge a real protected page only after a durable unknown/hold.

This module never solves a challenge, clicks a native control, authorizes a
send or extends a submission ticket. The original Worker lease stays real.
"""
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid
from urllib.parse import urlsplit

from .db import encode
from .errors import HubError
from .media_processing import sync_directory
from .replay_parts_v5 import reference_body,WORKER_PRINCIPAL
from .replay_video_web import ReplayVideoWeb,_timestamp
from .youtube_download import digest

EVENT='replay-part-owner-handoff-event-v5'
OBSERVATION='replay-part-owner-handoff-observation-v5'
ACK='replay-part-owner-handoff-parent-ack-v5'
CONFIG='bilibili-video-owner-handoff-v5'


def nonce_folder(settings,manifest):
    folder=settings.root/'media'/manifest['job_id']/'replay-browser-part-v5'/manifest['attempt_id']/'owner-handoff'
    if folder.resolve()!=folder or any(p.is_symlink() for p in (folder,*folder.parents)):fail('REPLAY_PART_OWNER_HANDOFF_PATH_INVALID')
    return folder


def existing_nonces(settings,manifest):
    folder=nonce_folder(settings,manifest)
    if not folder.exists():return set()
    return {p.name for p in folder.iterdir()}


def cleanup_credentials(settings,manifest,before):
    """After all owned processes exit, remove only this run's two secrets.

    No contents are read or printed. Older nonces and evidence are retained.
    This complements Node's finally block after a forced process termination.
    """
    folder=nonce_folder(settings,manifest)
    if not folder.exists():return 0
    created=[p for p in folder.iterdir() if p.name not in before]
    if len(created)>1:fail('REPLAY_PART_OWNER_HANDOFF_CREDENTIAL_CLEANUP_REQUIRED')
    deleted=0
    for directory in created:
        if str(uuid.UUID(directory.name))!=directory.name or directory.is_symlink() or not directory.is_dir():fail()
        parent=directory.stat()
        if os.name=='posix' and (parent.st_uid!=10001 or parent.st_mode&0o077):fail()
        for name in ('Xauthority','vnc-password.txt'):
            file=directory/name
            try:item=file.lstat()
            except FileNotFoundError:continue
            if (not stat.S_ISREG(item.st_mode) or item.st_nlink!=1 or not 0<=item.st_size<=4096 or
                    os.name=='posix' and (item.st_uid!=10001 or item.st_mode&0o077)):fail('REPLAY_PART_OWNER_HANDOFF_CREDENTIAL_CLEANUP_REQUIRED')
            fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            try:
                actual=os.fstat(fd);now=file.lstat();current_parent=directory.stat()
                keys=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
                if (any(getattr(item,k)!=getattr(actual,k) or getattr(item,k)!=getattr(now,k) for k in keys) or
                        (parent.st_dev,parent.st_ino)!=(current_parent.st_dev,current_parent.st_ino)):fail()
                if os.name=='posix':
                    dfd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|getattr(os,'O_NOFOLLOW',0))
                    try:
                        opened=os.fstat(dfd)
                        if (opened.st_dev,opened.st_ino)!=(parent.st_dev,parent.st_ino):fail()
                        target=os.stat(name,dir_fd=dfd,follow_symlinks=False)
                        if any(getattr(target,k)!=getattr(item,k) for k in keys):fail()
                        os.unlink(name,dir_fd=dfd);os.fsync(dfd)
                    finally:os.close(dfd)
                else:
                    os.close(fd);fd=None
                    if any(getattr(file.lstat(),k)!=getattr(item,k) for k in keys):fail()
                    file.unlink()
                deleted+=1
            finally:
                if fd is not None:os.close(fd)
    return deleted


def fail(code='REPLAY_PART_OWNER_HANDOFF_EVIDENCE_REQUIRED'):
    raise HubError(code,review=True)


def configuration(settings):
    file=settings.root/'config/bilibili-video-handoff-v5.json'
    raw=ReplayVideoWeb(settings,None)._root_readonly_file(file,maximum=4096)
    try:
        p=json.loads(raw)
        if (set(p)!={'version','enabled','ttl_seconds'} or p['version']!=CONFIG or p['enabled'] is not True or
                type(p['ttl_seconds']) is not int or not 60<=p['ttl_seconds']<=1200):raise ValueError()
        return p
    except (ValueError,TypeError,KeyError):fail('REPLAY_PART_OWNER_HANDOFF_CONFIG_REQUIRED')


def body_marker(protection):
    evidence=protection.get('evidence',{});controls=evidence.get('visible_challenge_controls',[])
    return (protection.get('blocked') is True and protection.get('error_code')=='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED' and
        evidence.get('version')=='bilibili-video-protection-guard-v3' and evidence.get('inspection_failed') is not True and
        evidence.get('truncated') is False and evidence.get('top_level_authentication') is False and
        type(evidence.get('native_mp4_uploaders')) is int and evidence['native_mp4_uploaders']==0 and evidence.get('visible_dialogs')==[] and evidence.get('challenge_frames')==[] and
        isinstance(controls,list) and len(controls)==1 and controls[0].get('tag')=='BODY' and controls[0].get('role')=='' and
        sorted(controls[0].get('classes',[]))==['risk-captcha-adapt','risk-captcha-adapt-pc'])


def readiness(settings,manifest,reference,*,empty):
    file=Path(reference['file']);base=settings.root/'media'/manifest['job_id']/'replay-browser-part-v5'/manifest['attempt_id']/'protection-readiness'
    if file.name!='observation.json' or file.parent.parent!=base or str(uuid.UUID(file.parent.name))!=file.parent.name:fail()
    value=reference_body(settings.root,reference)
    if (value['version']!='replay-part-empty-body-readiness-v5' or value['job_id']!=manifest['job_id'] or value['attempt_id']!=manifest['attempt_id'] or
            value['site_uid']!=manifest['target_site_uid'] or value['source_id']!=manifest['source_id'] or value['binding_sha256']!=manifest['binding_sha256'] or
            value['metadata_sha256']!=manifest['metadata_sha256'] or value['max_wait_ms']!=5000 or value['read_only'] is not True or
            not 0<=time.time()-_timestamp(value['observed_at'])<=60 or
            set(value['actions'])!={'file_selections','submit_clicks','save_clicks'} or any(type(v) is not int or v!=0 for v in value['actions'].values())):fail()
    for key in ('before','after'):
        part=value[key];text=part['visible_body_text']
        if not isinstance(text,str) or len(text)>30000:fail()
        image=reference_body(settings.root,part['screenshot'],maximum=20_000_000,expected=file.parent/(key+'.png'),json_body=False)
        if (len(image)<33 or image[:16]!=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' or
                not 0<int.from_bytes(image[16:20],'big')<=10000 or not 0<int.from_bytes(image[20:24],'big')<=10000):fail()
        if empty and (text.strip() or not body_marker(part['protection'])):fail()
    if empty and value['outcome']!='still_unreadable_body_marker':fail()
    if not isinstance(value['polls'],list) or not 1<=len(value['polls'])<=100:fail()
    previous=-1
    for poll in value['polls']:
        elapsed=poll['elapsed_ms']
        if (type(elapsed) not in (int,float) or not 0<=elapsed<120000 or elapsed<previous or
                type(poll['visible_body_empty']) is not bool or empty and (not poll['visible_body_empty'] or not body_marker(poll['protection']))):fail()
        previous=elapsed
    return value


def acknowledge(settings,manifest,event,*,request_id,operation,callback,seen,assert_lease):
    """Return the separate owner-wait limit after the bound callback commits."""
    if not callable(callback) or not callable(assert_lease) or manifest['version']!='replay-part-native-browser-manifest-v5':fail('REPLAY_PART_OWNER_HANDOFF_CALLBACK_REQUIRED')
    p=configuration(settings)
    try:
        nonce=event['nonce']
        if (event['version']!=EVENT or event['request_id']!=request_id or event['operation']!=operation or nonce in seen or
                str(uuid.UUID(nonce))!=nonce):raise ValueError()
        reason=event['reason']
        if reason not in ('visible_human_verification','unreadable_body_marker'):raise ValueError()
        interactive=reason=='visible_human_verification'
        if type(event['visible_body_empty']) is not bool:raise ValueError()
        expected={'job_id':manifest['job_id'],'attempt_id':manifest['attempt_id'],'nonce':nonce,'site_uid':manifest['target_site_uid'],
            'source_id':manifest['source_id'],'binding_sha256':manifest['binding_sha256'],'metadata_sha256':manifest['metadata_sha256'],
            'human_review':False,'publication_authorized':False,'authentication_assistance_only':interactive,'automatic_resubmit':False,
            'same_browser_context':True,'same_page':True,'ttl_seconds':p['ttl_seconds'],'expires_at':event['expires_at'],
            'reason':reason,'interactive_verification_observed':interactive,'visible_body_empty':event['visible_body_empty'],'readiness_observation':event['readiness_observation']}
        if set(event)!=set(expected)|{'version','event_reference','request_id','operation'} or any(event.get(k)!=v or type(event.get(k)) is not type(v) for k,v in expected.items()):raise ValueError()
        folder=settings.root/'media'/manifest['job_id']/'replay-browser-part-v5'/manifest['attempt_id']/'owner-handoff'/nonce
        body=reference_body(settings.root,event['event_reference'],expected=folder/'detected.json')
        if (set(body)!=set(expected)|{'version','state','observed_at','page','protection','screenshot','handoff_actions'} or
                body['version']!=OBSERVATION or body['state']!='detected' or
                any(body.get(k)!=v or type(body.get(k)) is not type(v) for k,v in expected.items()) or
                not 0<=time.time()-_timestamp(body['observed_at'])<=60):raise ValueError()
        expires=_timestamp(body['expires_at']);remaining=expires-time.time()
        if not 0<remaining<=p['ttl_seconds']+1 or not 0<=expires-_timestamp(body['observed_at'])<=p['ttl_seconds']:raise ValueError()
        page=body['page'];origin=page['origin'];path=page['path']
        if (set(page)!={'origin','path','query_keys'} or origin not in ('https://member.bilibili.com','https://www.bilibili.com') or
                origin=='https://member.bilibili.com' and path not in ('/platform/upload/video/frame','/platform/upload-manager/article') or
                origin=='https://www.bilibili.com' and not re.fullmatch('/video/BV[A-Za-z0-9]{10}/?',path) or
                not isinstance(page['query_keys'],list) or len(page['query_keys'])>30 or
                any(not isinstance(k,str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}',k) for k in page['query_keys'])):raise ValueError()
        protection=body['protection']
        if (protection.get('blocked') is not True or protection.get('error_code')!='BILIBILI_VIDEO_HUMAN_VERIFICATION_REQUIRED' or
                protection.get('evidence',{}).get('inspection_failed') is True):raise ValueError()
        structural=protection.get('evidence',{})
        if interactive:
            if not (structural.get('challenge_frames') or structural.get('visible_dialogs') or
                    any(c.get('tag')!='BODY' for c in structural.get('visible_challenge_controls',[]))):fail('REPLAY_PART_OWNER_HANDOFF_REAL_CHALLENGE_REQUIRED')
            if expected['readiness_observation'] is not None:readiness(settings,manifest,expected['readiness_observation'],empty=False)
        else:
            if not body_marker(protection) or expected['visible_body_empty'] is not True or expected['readiness_observation'] is None:fail()
            value=readiness(settings,manifest,expected['readiness_observation'],empty=True)
            if value['page']!=page:fail()
        actions=body['handoff_actions']
        if set(actions)!={'automatic_public_submit_clicks','file_selections','private_save_clicks'} or any(type(v) is not int or v!=0 for v in actions.values()):raise ValueError()
        image=reference_body(settings.root,body['screenshot'],maximum=20_000_000,expected=folder/'detected.png',json_body=False)
        if (len(image)<33 or image[:16]!=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' or
                not 0<int.from_bytes(image[16:20],'big')<=10000 or not 0<int.from_bytes(image[20:24],'big')<=10000):raise ValueError()
        seen.add(nonce)
        assert_lease()
        callback(body,event['event_reference'])  # code-owned; must durably checkpoint or raise
        ack={'version':ACK,'event_reference':event['event_reference'],'job_id':manifest['job_id'],'attempt_id':manifest['attempt_id'],
            'nonce':nonce,'recorded_unknown_or_hold':True,'automatic_retry':False,'human_review':False,'publication_authorized':False}
        target=folder/'parent-ack.json'
        assert_lease()
        if expires<=time.time():fail('REPLAY_PART_OWNER_HANDOFF_EXPIRED')
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(encode(ack).encode());stream.flush();os.fsync(stream.fileno())
        sync_directory(folder)
        return max(0,expires-time.time())+45
    except HubError:raise
    except (OSError,ValueError,TypeError,KeyError,AttributeError):fail()


def record_worker_handoff(service,job_id,body,event_reference):
    """The main publisher's actual lease-bound checkpoint before GUI starts."""
    service._operator(WORKER_PRINCIPAL);job,binding,state=service.snapshot(job_id,write=False)
    if (body['job_id']!=job_id or body['attempt_id']!=state.get('attempt_id') or body['binding_sha256']!=digest(binding)):fail()
    before=state['state']
    if before in ('uploading','upload_unknown','submitting','submit_unknown'):
        service.mark_unknown(WORKER_PRINCIPAL,job_id);job,binding,state=service.snapshot(job_id,write=False)
    elif before not in ('prepared','uploaded','awaiting_moderation','completed','failed'):fail('REPLAY_PART_OWNER_HANDOFF_STAGE_INVALID')
    hold={'version':OBSERVATION,'nonce':body['nonce'],'event_reference':event_reference,'stage_before':before,'reason':body['reason'],
        'recorded_at':datetime.now(timezone.utc).isoformat(),'authentication_assistance_only':body['authentication_assistance_only'],'automatic_retry':False,'human_review':False}
    code='OWNER_AUTHENTICATION_REQUIRED' if body['reason']=='visible_human_verification' else 'OWNER_VISIBLE_UI_DIAGNOSTIC_REQUIRED'
    service._update(WORKER_PRINCIPAL,job,{**state,'owner_handoff':hold},job_state=job['state'],stage='replay_part_owner_handoff_wait',code=code)
    with service.store.transaction() as db:
        service.assert_worker_lease()
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (time.time(),WORKER_PRINCIPAL,'replay_part_owner_handoff_before_gui',job_id,encode(hold)))


def record_lookup_handoff(service,child,body,event_reference):
    """Independent read-only child's real lease; never manufacture a parent lease."""
    from .replay_part_moderation_v5 import lookup_context,LOOKUP_PRINCIPAL
    context,_=lookup_context(service.settings,service.store,service,child)
    parent,binding,state=context[:3]
    if body['job_id']!=parent['job_id'] or body['attempt_id']!=state['attempt_id'] or body['binding_sha256']!=digest(binding):fail()
    hold={'version':OBSERVATION,'nonce':body['nonce'],'event_reference':event_reference,'parent_publish_job_id':parent['job_id'],'reason':body['reason'],
        'recorded_at':datetime.now(timezone.utc).isoformat(),'authentication_assistance_only':body['authentication_assistance_only'],'automatic_retry':False,'human_review':False}
    with service.store.transaction() as db:
        lookup_context(service.settings,service.store,service,child)
        current=db.execute('SELECT result FROM jobs WHERE job_id=?',(child['job_id'],)).fetchone()
        result={**json.loads(current['result']),'owner_handoff':hold}
        if db.execute("UPDATE jobs SET result=?,stage='replay_part_lookup_owner_handoff_wait',updated_at=? WHERE job_id=? AND state='processing' AND lease_owner=? AND lease_until>?",
                (encode(result),time.time(),child['job_id'],child['lease_owner'],time.time())).rowcount!=1:fail('REPLAY_PART_OWNER_HANDOFF_LOOKUP_LEASE_LOST')
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (time.time(),LOOKUP_PRINCIPAL,'replay_part_lookup_owner_handoff_before_gui',child['job_id'],encode(hold)))
    child.update(result=result,stage='replay_part_lookup_owner_handoff_wait')
