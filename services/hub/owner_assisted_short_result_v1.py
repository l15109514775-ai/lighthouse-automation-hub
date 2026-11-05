"""Reconcile an operator-observed, owner-assisted short result; never send/delete.

Authentication assistance is distinct from human review and from evidence that
the original automated click caused publication. Original reservations remain.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from .db import encode
from .errors import HubError
from .short_video_web import STATE, KIND, PRINCIPAL, _reference
from .youtube_download import digest

VERSION = 'owner-assisted-short-result-v1'
TOMBSTONE = 'owner-authorized-short-remote-deletion-v1'
FIELDS = {'job_id','attempt_id','recovery_id','target_site_uid','source_url','source_id','download_job_id',
          'file','file_bytes','artifact_sha256','metadata_sha256','upload_metadata_sha256','binding_sha256',
          'metadata_revision_id','upload_reference_sha256'}
SHA = re.compile(r'[a-f0-9]{64}')
BV = re.compile(r'BV[A-Za-z0-9]{10}')


def _fail():
    raise HubError('OWNER_ASSISTED_SHORT_EVIDENCE_REQUIRED', review=True)


def _when(value):
    parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None or not -60 <= time.time()-parsed.timestamp() <= 86400:
        _fail()
    return parsed.astimezone(timezone.utc).isoformat()


def _file(path, *, maximum, expected_sha=None):
    path = Path(path)
    if (not path.is_absolute() or path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)) or
            not path.is_file() or not 0<path.stat().st_size<=maximum):
        _fail()
    before=path.stat();body=path.read_bytes();after=path.stat();sha=hashlib.sha256(body).hexdigest()
    if (len(body)!=before.st_size or any(getattr(before,k)!=getattr(after,k) for k in
            ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) or expected_sha is not None and sha!=expected_sha):
        _fail()
    return {'file':str(path),'bytes':len(body),'sha256':sha},body


def _json_evidence(service,job_id,file,sha,name):
    file=Path(file);base=service.settings.root/'media'/job_id/'owner-assisted-v1'
    if (file.name!=name or file.parent.parent!=base or str(uuid.UUID(file.parent.name))!=file.parent.name or
            not isinstance(sha,str) or not SHA.fullmatch(sha)):
        _fail()
    reference,body=_file(file,maximum=262144,expected_sha=sha)
    return reference,json.loads(body)


def _png(reference,folder,name):
    if (not isinstance(reference,dict) or set(reference)!={'file','bytes','sha256'} or
            reference['file']!=str(folder/name) or type(reference['bytes']) is not int or
            not isinstance(reference['sha256'],str) or not SHA.fullmatch(reference['sha256'])):
        _fail()
    actual,body=_file(reference['file'],maximum=20_000_000,expected_sha=reference['sha256'])
    if (actual!=reference or len(body)<33 or body[:16]!=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' or
            int.from_bytes(body[16:20],'big')<1 or int.from_bytes(body[20:24],'big')<1):
        _fail()


def _publication(db,job,state):
    rows=db.execute("SELECT * FROM publications WHERE job_id=? AND idempotency_key=? AND platform='bilibili'",
                    (job['job_id'],state['key'])).fetchall()
    if (len(rows)!=1 or rows[0]['kind']!=KIND or rows[0]['source_platform']!='youtube' or
            rows[0]['source_id']!=job['source_id'] or rows[0]['account_id']!='site_uid:'+job['payload']['site_uid']):
        _fail()
    return rows[0]


def record_owner_assisted_result_v1(service, principal, job_id, *, binding_sha256,
        actual_auth_response, auth_response_reference, actual_operator_reference,
        native_observation_file, native_observation_sha256, public_observation_file, public_observation_sha256):
    """Record real native/public observations after the owner's authentication help."""
    service._operator(principal)
    try:
        job,binding,state=service.snapshot(job_id,write=False)
        if (principal!=PRINCIPAL or digest(binding)!=binding_sha256 or binding.get('human_review') is not False or
                state.get('human_review') is not False or state.get('public_submission_approved') is not True or
                state.get('owner_acceptance',{}).get('binding_sha256')!=binding_sha256 or
                state.get('owner_acceptance',{}).get('human_review') is not False or
                state.get('owner_acceptance',{}).get('automatic_other_video_publication_authorized') is not False or
                actual_auth_response!='验证成功了' or not _reference(auth_response_reference) or
                not _reference(actual_operator_reference) or job.get('lease_owner') is not None or job.get('lease_until') is not None):
            _fail()
        nref,native=_json_evidence(service,job_id,native_observation_file,native_observation_sha256,'native.json')
        pref,public=_json_evidence(service,job_id,public_observation_file,public_observation_sha256,'public.json')
        if Path(nref['file']).parent!=Path(pref['file']).parent:
            _fail()
        common={'job_id':job_id,'source_url':binding['source_url'],'title':binding['metadata']['title']}
        external=native.get('external_id');url='https://www.bilibili.com/video/'+str(external)
        expected_normalization={'version':'bilibili-public-description-source-prefix-v1',
            'removed_prefix':binding['source_url']+'\n','occurrences_removed':1}
        if (native.get('version')!='short-owner-assisted-native-observation-v1' or
                public.get('version')!='short-owner-assisted-public-observation-v1' or
                any(native.get(key)!=value or public.get(key)!=value for key,value in common.items()) or
                native.get('site_uid')!=binding['site_uid'] or public.get('publisher_uid')!=binding['site_uid'] or
                native.get('metadata_sha256')!=binding['metadata_sha256'] or
                native.get('observed_description')!=binding['source_url']+'\n'+binding['metadata']['description'] or
                native.get('normalized_description')!=binding['metadata']['description'] or
                native.get('description_normalization')!=expected_normalization or
                not isinstance(external,str) or not BV.fullmatch(external) or
                public.get('external_id')!=external or native.get('url')!=url or public.get('url')!=url or
                native.get('state')!='completed' or public.get('public_accessible') is not True or
                public.get('observed_description')!=binding['source_url']+'\n'+binding['metadata']['description'] or
                public.get('normalized_description')!=binding['metadata']['description'] or
                public.get('description_normalization')!=expected_normalization):
            _fail()
        _when(native['observed_at']);observed_at=_when(public['observed_at']);folder=Path(nref['file']).parent
        _png(native['screenshot'],folder,'native.png');_png(public['screenshot'],folder,'public.png')
        gate_file=service.settings.root/'media'/job_id/'recovered-submit-intent.json'
        gref,gbody=_file(gate_file,maximum=65536);gate=json.loads(gbody);g=gate['binding']
        expected={'job_id':job_id,'attempt_id':state['attempt_id'],'target_site_uid':binding['site_uid'],
            'source_url':binding['source_url'],'source_id':binding['source_id'],'download_job_id':binding['download_job_id'],
            'file':binding['file'],'file_bytes':binding['file_bytes'],'artifact_sha256':binding['artifact_sha256'],
            'metadata_sha256':binding['metadata_sha256'],'binding_sha256':binding_sha256,
            'metadata_revision_id':state['active_metadata_revision_id'],
            'upload_reference_sha256':state['upload_observation']['upload_reference_sha256']}
        history=state['metadata_revision_history']
        if (set(g)!=FIELDS or any(g.get(key)!=value for key,value in expected.items()) or
                g['upload_metadata_sha256']!=history[0]['previous_metadata_sha256'] or
                str(uuid.UUID(g['recovery_id']))!=g['recovery_id'] or
                gate.get('version')!='recovered-short-video-submit-intent-v1' or
                gate.get('state')!='submit_reserved_result_unknown' or gate.get('reconcile_before_retry') is not True or
                gate.get('native_click_performed')!='unknown' or gate.get('human_review') is not False or
                gate.get('new_file_selection') is not False):
            _fail()
        if (str(uuid.UUID(gate['ticket_id']))!=gate['ticket_id'] or str(uuid.UUID(gate['command_id']))!=gate['command_id'] or
                not SHA.fullmatch(gate['approval_sha256']) or not SHA.fullmatch(gate['reservation_sha256'])):
            _fail()
        _when(gate['created_at'])
        click_file=gate_file.parent/'recovered-editors'/g['recovery_id']/'native-short-submit-click-intent.json'
        cref,cbody=_file(click_file,maximum=65536);click=json.loads(cbody)
        if (click.get('version')!='short-native-submit-controller-v1' or click.get('job_id')!=job_id or
                click.get('attempt_id')!=state['attempt_id'] or click.get('recovery_id')!=g['recovery_id'] or
                click.get('metadata_sha256')!=binding['metadata_sha256'] or click.get('ticket_id')!=gate['ticket_id'] or
                click.get('command_id')!=gate['command_id'] or click.get('state')!='native_click_reserved_result_unknown' or
                click.get('title')!=binding['metadata']['title'] or click.get('description')!=binding['metadata']['description'] or
                click.get('source')!=binding['source_url'] or click.get('reconcile_before_retry') is not True or
                click.get('new_video_file_selection') is not False):
            _fail()
        _when(click['created_at'])
        cover_file=gate_file.parent/'cover-assets-v4/top-bottom-blur-cover.png'
        cover_ref,cover_bytes=_file(cover_file,maximum=5_000_000)
        provenance_ref,_=_file(cover_file.parent/'provenance.json',maximum=65536)
        if (cover_bytes[:16]!=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' or
                click.get('cover',{}).get('cover_sha256')!=cover_ref['sha256'] or
                click.get('cover',{}).get('cover_provenance_sha256')!=provenance_ref['sha256'] or
                click.get('cover',{}).get('server_image_hash_verified') is not False):
            _fail()
        fingerprint=digest({'version':VERSION,'binding_sha256':binding_sha256,'native':nref,'public':pref,
            'gate':gref,'click':cref,'actual_auth_response':actual_auth_response,
            'auth_response_reference':auth_response_reference,'actual_operator_reference':actual_operator_reference})
        previous=state.get('owner_assisted_result_v1')
        if previous is not None:
            if (previous.get('evidence_sha256')!=fingerprint or job['external_publish_id']!=external or
                    job['state']!='completed' or state['state']!='completed'):
                _fail()
            return {'job_id':job_id,'state':job['state'],'external_id':external,'url':url,'changed':False}
        if job['state']!='publish_unknown' or state['state']!='submit_unknown':
            _fail()
        assisted={'version':VERSION,'evidence_sha256':fingerprint,'binding_sha256':binding_sha256,
            'native_observation':nref,'public_observation':pref,'original_gate_intent':gref,'original_click_intent':cref,
            'description_normalization':expected_normalization,'observed_public_description':public['observed_description'],
            'normalized_public_description':public['normalized_description'],
            'observed_native_description':native['observed_description'],'normalized_native_description':native['normalized_description'],
            'authentication_assistance':{'actual_response':actual_auth_response,'reference':auth_response_reference,
                'meaning':'authentication_assistance_only','publication_authorization_inferred':False},
            'actual_operator_reference':actual_operator_reference,'human_review':False,
            'same_submit_attempt_observed':False,'recorded_at':datetime.now(timezone.utc).isoformat()}
        observation={'version':'short-owner-assisted-result-v1','site_uid':binding['site_uid'],'source_url':binding['source_url'],
            'metadata_sha256':binding['metadata_sha256'],'external_id':external,'url':url,'state':'completed',
            'public_accessible':True,'public_lookup_observed_at':observed_at,'human_review':False,
            'same_submit_attempt_observed':False,'execution_origin':'owner_assisted_native_web',
            'reconciliation_evidence_sha256':fingerprint}
        updated={**state,'state':'completed','result_observation':observation,'owner_assisted_result_v1':assisted}
        now=time.time()
        with service.store.transaction() as db:
            current=db.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
            if not current or current['updated_at']!=job['updated_at'] or current['state']!='publish_unknown':
                _fail()
            publication=_publication(db,job,state)
            if publication['state'] not in ('unknown','dispatching') or publication['external_id'] not in (None,external):
                _fail()
            if db.execute("SELECT COUNT(*) FROM audit WHERE target=? AND action='short_video_reserve_submit' AND outcome='one_artifact_one_click_reserved'",(job_id,)).fetchone()[0]!=1:
                _fail()
            db.execute('UPDATE publications SET state=?,external_id=?,external_url=?,updated_at=? WHERE idempotency_key=?',
                ('completed',external,url,now,state['key']))
            db.execute('UPDATE jobs SET state=?,result=?,stage=?,error_code=NULL,external_publish_id=?,updated_at=? WHERE job_id=?',
                ('completed',encode({**job['result'],STATE:updated,'publish_performed':True}),VERSION,external,now,job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now,principal,VERSION,job_id,'owner_auth_assistance_reconciled_without_machine_same_attempt_claim'))
        return {'job_id':job_id,'state':'completed','external_id':external,'url':url,'changed':True,
                'human_review':False,'same_submit_attempt_observed':False,'evidence_sha256':fingerprint}
    except HubError:
        raise
    except (OSError,KeyError,ValueError,TypeError,AttributeError,OverflowError):
        _fail()


def record_owner_authorized_remote_deletion_v1(service, principal, job_id, *, external_id,
        actual_owner_delete_response, owner_response_reference, actual_operator_reference,
        deletion_observation_file, deletion_observation_sha256):
    """Record already observed deletion. Never click, delete files or erase rows."""
    service._operator(principal)
    try:
        job,binding,state=service.snapshot(job_id,write=False);previous=state.get('result_observation',{})
        if (principal!=PRINCIPAL or job['state']!='completed' or state['state']!='completed' or
                job['external_publish_id']!=external_id or previous.get('external_id')!=external_id or
                previous.get('public_accessible') is not True or not BV.fullmatch(external_id) or
                any(not _reference(value) for value in (actual_owner_delete_response,owner_response_reference,actual_operator_reference))):
            _fail()
        reference,observed=_json_evidence(service,job_id,deletion_observation_file,deletion_observation_sha256,'deletion.json')
        expected={'version':'short-owner-authorized-remote-deletion-observation-v1','job_id':job_id,
            'site_uid':binding['site_uid'],'source_url':binding['source_url'],'external_id':external_id,
            'url':'https://www.bilibili.com/video/'+external_id,'native_deletion_confirmed':True,'public_accessible':False}
        if (any(observed.get(key)!=value for key,value in expected.items()) or
                observed.get('native_deletion_confirmed') is not True or observed.get('public_accessible') is not False or
                datetime.fromisoformat(_when(observed['observed_at'])).timestamp()<datetime.fromisoformat(previous['public_lookup_observed_at']).timestamp()):
            _fail()
        folder=Path(reference['file']).parent
        _png(observed['native_screenshot'],folder,'deletion-native.png');_png(observed['public_screenshot'],folder,'deletion-public.png')
        fingerprint=digest({'version':TOMBSTONE,'observation':reference,'actual_owner_delete_response':actual_owner_delete_response,
            'owner_response_reference':owner_response_reference,'actual_operator_reference':actual_operator_reference})
        if 'owner_authorized_remote_deletion_v1' in state:
            if state['owner_authorized_remote_deletion_v1']['evidence_sha256']!=fingerprint:_fail()
            return {'job_id':job_id,'external_id':external_id,'changed':False,'remote_state':'owner_deleted'}
        tombstone={'version':TOMBSTONE,'external_id':external_id,'url':expected['url'],'remote_state':'owner_deleted',
            'evidence_sha256':fingerprint,'observation':reference,'actual_owner_delete_response':actual_owner_delete_response,
            'owner_response_reference':owner_response_reference,'actual_operator_reference':actual_operator_reference,
            'recorded_at':datetime.now(timezone.utc).isoformat(),'local_media_deletion_authorized':False,
            'publication_record_removed':False,'browser_deletion_executed_by_this_method':False,'human_review':False}
        updated={**state,'owner_authorized_remote_deletion_v1':tombstone}
        updated.setdefault('test_retention_policy',{'version':'short-video-remote-deletion-local-hold-v1',
            'reason':'REMOTE_DELETION_DOES_NOT_AUTHORIZE_LOCAL_EVIDENCE_CLEANUP','local_cleanup_authorized':False})
        result={**job['result'],STATE:updated}
        with service.store.transaction() as db:
            current=db.execute('SELECT updated_at FROM jobs WHERE job_id=?',(job_id,)).fetchone()
            if not current or current['updated_at']!=job['updated_at']:_fail()
            publication=_publication(db,job,state)
            if publication['external_id']!=external_id or publication['state']!='completed':_fail()
            db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',(encode(result),time.time(),job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (time.time(),principal,TOMBSTONE,job_id,'remote_tombstone_recorded_local_and_publication_records_preserved'))
        return {'job_id':job_id,'external_id':external_id,'changed':True,'remote_state':'owner_deleted',
                'local_media_deleted':False,'publication_record_removed':False}
    except HubError:
        raise
    except (OSError,KeyError,ValueError,TypeError,AttributeError,OverflowError):
        _fail()
