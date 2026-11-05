"""Reconcile an already sent replay from later native/public observations.

This module cannot upload, submit, authorize publication, obtain a lease, or
delete anything. In particular it never invents same-submit-attempt evidence.
"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .db import encode
from .errors import HubError
from .short_video_web import PRINCIPAL, _reference
from .youtube_download import digest

VERSION = 'replay-later-native-result-reconciliation-v1'
ORIGIN = 'later_native_lookup_reconciled_v1'
BV = re.compile(r'BV[A-Za-z0-9]{10}')


def require(condition):
    if not condition:
        raise HubError('REPLAY_LATER_NATIVE_RESULT_EVIDENCE_REQUIRED', review=True)


def when(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None and math.isfinite(parsed.timestamp()))
    return parsed.timestamp()


def read_reference(root, reference, *, maximum=1_000_000):
    require(isinstance(reference, dict) and set(reference) == {'file', 'bytes', 'sha256'})
    file = Path(reference['file'])
    require(file.is_absolute() and file.is_relative_to(root) and file.resolve() == file and
            not any(p.is_symlink() for p in (file, *file.parents)) and file.is_file() and
            type(reference['bytes']) is int and 0 < reference['bytes'] <= maximum)
    fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        before = os.fstat(stream.fileno()); raw = stream.read(maximum+1); after = os.fstat(stream.fileno())
    require(len(raw) == before.st_size == reference['bytes'] and
            all(getattr(before, k) == getattr(after, k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) and
            hashlib.sha256(raw).hexdigest() == reference['sha256'])
    return raw


def ref(file):
    require(file.resolve()==file and not any(p.is_symlink() for p in (file,*file.parents)) and
            file.is_file() and 0<file.stat().st_size<=1_000_000)
    fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:raw=stream.read(1_000_001)
    require(0<len(raw)<=1_000_000)
    return {'file': str(file), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}


def validate_public(value, binding, attempt_id, duration, *, now=None):
    now = time.time() if now is None else now
    external = value.get('external_id'); require(isinstance(external, str) and BV.fullmatch(external))
    require(value.get('version') == 'known-bv-public-passive-inventory-v1' and
            value.get('job_id') == binding['job_id'] and value.get('attempt_id') == attempt_id and
            value.get('expected_title') == binding['metadata']['title'] and
            0 <= now-when(value['observed_at']) <= 300 and
            all(value.get(k) == 0 for k in ('file_selections','save_clicks','submit_clicks','player_seeks')) and
            value.get('source_or_media_modified') is False)
    dom = value['visible_dom']; url = urlsplit(dom['url'])
    require(url.scheme == 'https' and url.netloc == 'www.bilibili.com' and url.path in
            ('/video/'+external, '/video/'+external+'/') and not url.query and not url.fragment)
    require(len(dom['titles']) == 1 and dom['titles'][0]['text'] == binding['metadata']['title'])
    # Only the observed UP-name card is evidence of the publisher. Viewer
    # headers, generic space links and a probe's derived booleans are ignored.
    owners = [item for item in dom['owner_candidates'] if item.get('visible') is True and
              'up-name' in item.get('class','').split()]
    require(len(owners) == 1 and owners[0].get('text') and
            owners[0]['text'] == binding['account_name'] and owners[0]['text'] in owners[0].get('card_text',''))
    owner = urlsplit(owners[0]['url'])
    require(owner.scheme == 'https' and owner.netloc == 'space.bilibili.com' and not owner.query and not owner.fragment and
            re.fullmatch('/'+re.escape(binding['site_uid'])+'/?', owner.path))
    descriptions = [item for item in dom['descriptions'] if item.get('visible') is True and
                    'basic-desc-info' in item.get('class','').split()]
    require(len(descriptions) == 1)
    raw_description = descriptions[0]['text']; expected = binding['metadata']['description']
    if raw_description == expected:
        normalization = {'version':'bilibili-public-description-source-prefix-v1','removed_prefix':'','occurrences_removed':0}
    else:
        prefix = binding['source_url']+'\n'
        require(raw_description == prefix+expected)
        normalization = {'version':'bilibili-public-description-source-prefix-v1','removed_prefix':prefix,'occurrences_removed':1}
    videos = [item for item in dom['videos'] if item.get('visible') is True]
    require(len(videos) == 1); video = videos[0]
    finite = lambda x: type(x) in (int,float) and math.isfinite(x)
    require(finite(video['duration_seconds']) and abs(video['duration_seconds']-duration) <= 2 and
            finite(video['current_time']) and 0 <= video['current_time'] <= video['duration_seconds']+1 and
            type(video['ready_state']) is int and 2 <= video['ready_state'] <= 4 and video['error_code'] is None and
            type(video['video_width']) is int and video['video_width'] > 0 and
            type(video['video_height']) is int and video['video_height'] > 0 and
            isinstance(video['seekable_ranges'],list) and 1 <= len(video['seekable_ranges']) <= 100 and
            all(set(r)=={'start','end'} and finite(r['start']) and finite(r['end']) and
                0 <= r['start'] < r['end'] <= video['duration_seconds']+1 for r in video['seekable_ranges']))
    return {'external_id': external, 'url':'https://www.bilibili.com/video/'+external,
            'description_normalization':normalization,'observed_description':raw_description,
            'normalized_description':expected,'publisher':deepcopy(owners[0]),
            'player':{key:deepcopy(video[key]) for key in ('duration_seconds','current_time','ready_state','seekable_ranges','error_code')},
            'observed_at':value['observed_at']}


def validate_manager(value, binding, external):
    require(value['url'] == 'https://member.bilibili.com/platform/upload-manager/article')
    text = value['visible_text']; lines = [x.strip() for x in text.splitlines()]
    require(all(label in lines for label in ('视频管理','图文管理','互动视频管理','全部稿件 1','进行中 0','已发布 1','未通过 0')))
    candidates = []
    for link in value['links']:
        url = urlsplit(link.get('url') or '')
        match = re.fullmatch('/video/(BV[A-Za-z0-9]{10})/?', url.path)
        if url.scheme == 'https' and url.netloc == 'www.bilibili.com' and match:
            require(not url.query and not url.fragment); candidates.append((match[1], link.get('text')))
    require({item[0] for item in candidates} == {external} and
            sum(item[1] == binding['metadata']['title'] for item in candidates) == 1)
    require(binding['metadata']['title'] in lines)
    stamps = re.findall(r'(\d{4})年(\d{2})月(\d{2})日 (\d{2}):(\d{2}):(\d{2})', text)
    require(len(stamps) == 1)
    return datetime(*map(int, stamps[0]), tzinfo=timezone(timedelta(hours=8))).timestamp()


def _commit_later_result(service, principal, job, state, publications, intent_refs, observation, evidence, extra, *, recheck,
                         version=VERSION, origin=ORIGIN):
    """The single CAS boundary, after actual observation/coverage validation."""
    service._operator(principal)
    updated={**state,'state':'completed','result_observation':observation,'result_reconciliation':evidence,**extra}
    job_state=service._checkpoint_job_state(job,'completed');now=time.time()
    job_id=job['job_id'];intents=service.settings.root/'media'/job_id/'replay-native-intents'
    with service.store.transaction() as db:
        current=db.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
        require(service._checkpoint_matches(current,job))
        decoded=dict(current)
        for key in ('payload','result'):decoded[key]=json.loads(decoded[key])
        require(all(decoded[k]==v for k,v in job.items() if k not in ('updated_at','lease_until')))
        rows=[dict(r) for r in db.execute("SELECT * FROM publications WHERE job_id=? OR (platform='bilibili' AND source_platform='youtube' AND source_id=?)",(job_id,job['source_id']))]
        require(rows==publications and {p.name:ref(p) for p in intents.iterdir()}==intent_refs)
        recheck()  # Only bounded evidence refs/source rows, never a whole MP4 read.
        require(0 <= time.time()-when(observation['public_lookup_observed_at']) <= 300)
        db.execute('UPDATE publications SET state=?,external_id=?,external_url=?,updated_at=? WHERE idempotency_key=?',
                   ('completed',observation['external_id'],observation['url'],now,state['key']))
        db.execute('UPDATE jobs SET state=?,stage=?,result=?,error_code=NULL,external_publish_id=?,updated_at=? WHERE job_id=?',
            (job_state,version,encode({**job['result'],service.STATE:updated,'publish_performed':True}),observation['external_id'],now,job_id))
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
            (now,principal,version,job_id,encode({'origin':origin,'evidence_sha256':digest(evidence),'same_submit_attempt_observed':False})))
    service._checkpoint_committed(job_id)


def record_later_native_result(service, principal, job_id, *, public_reference, screenshot_reference,
                              manager_reference, actual_operator_reference, apply=False):
    """Root operator or an actual service worker lease; never fabricate either.

    V1 deliberately requires the recorded empty-all manager before the send.
    Future nonempty-manager evidence needs a separately versioned producer.
    """
    return _record_later_native_result(service,principal,job_id,public_reference=public_reference,
        screenshot_reference=screenshot_reference,manager_reference=manager_reference,
        actual_operator_reference=actual_operator_reference,apply=apply,
        version=VERSION,origin=ORIGIN,before_reference=None,
        before_validator=_validate_empty_before,manager_validator=validate_manager)


def _validate_empty_before(before,binding,state,intent):
    require(before['visible_dom']['url']=='https://member.bilibili.com/platform/upload-manager/article' and
            all(label in before['visible_dom']['visible_text'].splitlines() for label in ('全部稿件 0','进行中 0','已发布 0','未通过 0')) and
            not re.search(r'BV[A-Za-z0-9]{10}',before['visible_dom']['visible_text']) and
            not any(re.search(r'/video/BV[A-Za-z0-9]{10}',item.get('url') or '') for item in before['visible_dom']['links']) and
            when(before['observed_at']) < when(intent['created_at']))


def _record_later_native_result(service,principal,job_id,*,public_reference,screenshot_reference,
        manager_reference,actual_operator_reference,apply,version,origin,before_reference,before_validator,manager_validator):
    """Shared causal/permission/CAS gates; versioned callers supply only DOM rules."""
    service._operator(principal)
    require(_reference(actual_operator_reference) and type(apply) is bool)
    job, binding, state = service.snapshot(job_id, write=False)
    require(binding.get('job_id',job_id) == job_id)
    binding = {**binding, 'job_id':job_id}  # Readback identity only, never rehash this augmented copy.
    actual_binding = job['payload']; root = service.settings.root/'media'/job_id
    require(job['state'] in ('publish_unknown','processing') and state['state'] == 'submit_unknown' and
            not job['external_publish_id'] and not state.get('result_observation') and
            state['public_submission_approved'] is False and state['human_review'] is False and
            job['result']['publish_performed'] is False)
    if principal == PRINCIPAL:
        require(job['lease_owner'] is None and job['lease_until'] is None)
    else:
        require(service._worker_job is not None); service.assert_worker_lease()
    service._validate_snapshot_binding(job, actual_binding, verify_artifact_hash=False)
    publications = service.store.query("SELECT * FROM publications WHERE job_id=? OR (platform='bilibili' AND source_platform='youtube' AND source_id=?)",(job_id,job['source_id']))
    require(len(publications)==1); publication = publications[0]
    require(publication['job_id']==job_id and publication['idempotency_key']==state['key'] and
            publication['kind']==service.KIND and publication['source_id']==job['source_id'] and
            publication['account_id']=='site_uid:'+binding['site_uid'] and publication['state']=='unknown' and
            publication['external_id'] is None and publication['external_url'] is None)
    intents = root/'replay-native-intents'
    require({p.name for p in intents.iterdir()} == {'video-selection.json','cover-completion.json','private-draft-save.json','public-submit.json'})
    intent_refs = {p.name:ref(p) for p in intents.iterdir()}
    intent = json.loads(read_reference(root,intent_refs['public-submit.json']))
    from .replay_highest_coverage_v2 import expected_binding, upload_coverage
    expected = expected_binding(job,actual_binding,state)
    for name, reference in intent_refs.items():
        item=json.loads(read_reference(root,reference))
        require(item['version']=='replay-native-one-use-intent-v1' and item['kind']+'.json'==name and
                all(item[k]==v for k,v in expected.items()))
    require(intent['version']=='replay-native-one-use-intent-v1' and intent['kind']=='public-submit' and
            all(intent[k]==v for k,v in expected.items()))
    ticket = intent['reservation']; proof = state['machine_acceptance']
    require(ticket['version']==service.NATIVE_RESERVATION_VERSION and ticket['job_id']==job_id and
            ticket['attempt_id']==state['attempt_id'] and ticket['idempotency_key']==state['key'] and
            ticket['binding_sha256']==digest(actual_binding) and ticket['metadata_sha256']==binding['metadata_sha256'] and
            ticket['can_click_submit'] is True and ticket['state']=='dispatching' and
            ticket['machine_evidence_sha256']==proof['evidence_sha256'] and when(intent['created_at']) < ticket['expires_at'])
    proof_ref = {'file':proof['evidence_file'],'bytes':proof['evidence_bytes'],'sha256':proof['evidence_sha256']}
    proof_body = json.loads(read_reference(root,proof_ref))
    require(proof_body == {k:v for k,v in proof.items() if k not in ('evidence_file','evidence_bytes','evidence_sha256')} and
            proof['version']==service.MACHINE_PROOF_VERSION and proof['binding_sha256']==digest(actual_binding) and
            proof['policy_sha256']==binding['policy_sha256'] and when(proof['created_at']) <= when(intent['created_at']) and
            all(proof[k] is False for k in ('human_review','per_artifact_owner_acceptance','publication_authorized')))
    prepared=state['native_preparation']
    require(all(prepared[k]==v for k,v in expected.items()) and
            prepared['private_draft_saved'] is prepared['metadata_verified'] is prepared['cover_applied'] is True and
            prepared['upload_reference_sha256']==state['upload_observation']['upload_reference_sha256'] and
            0 <= when(intent['created_at'])-when(prepared['observed_at']) <= 300)
    for key in ('native_observation_file','pre_submit_screenshot'):
        read_reference(root,prepared[key],maximum=20_000_000)
    output = root/service.NATIVE_DIRECTORY/state['attempt_id']
    unknown_ref = ref(output/'public-result.json')
    require(json.loads(read_reference(root,unknown_ref)) == {'state':'unknown','submitted':None,'reconcile_required':True})
    before_ref = before_reference or state.get('reservation_recovery',{}).get('manager_query')
    require(isinstance(before_ref,dict)); before = json.loads(read_reference(root,before_ref))
    before_validator(before,binding,state,intent)
    raw_public = json.loads(read_reference(root,public_reference))
    source = service.store.get(binding['download_job_id']); duration = source['result'][service.DOWNLOAD_STATE]['binding']['duration']
    public = validate_public(raw_public,binding,state['attempt_id'],duration)
    manager = json.loads(read_reference(root,manager_reference)); posted = manager_validator(manager,binding,public['external_id'])
    require(when(intent['created_at']) <= posted <= when(public['observed_at'])+60)
    screenshot = read_reference(root,screenshot_reference,maximum=20_000_000)
    require(Path(screenshot_reference['file']).parent == Path(public_reference['file']).parent and
            screenshot[:16]==b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' and len(screenshot)>=33 and
            0<int.from_bytes(screenshot[16:20],'big')<=10000 and 0<int.from_bytes(screenshot[20:24],'big')<=10000)
    uploaded = upload_coverage(service.settings,job,actual_binding,state,state['upload_observation']['upload_coverage'],fresh=False)
    require(uploaded == state['upload_coverage'])
    require(source['result'][service.DOWNLOAD_STATE]['receipt']['source_to_merged_packet_coverage_verified'] is True)
    evidence = {'version':version,'origin':origin,'public_observation':public_reference,'screenshot':screenshot_reference,
        'manager_observation':manager_reference,'manager_before_submit':before_ref,'original_public_submit':intent_refs['public-submit.json'],
        'original_unknown_result':unknown_ref,'original_machine_proof':proof_ref,'actual_operator_reference':actual_operator_reference,
        'same_submit_attempt_observed':False,'human_review':False,'publication_authorization_inferred':False,
        'binding_sha256':digest(actual_binding),'public_description':public['observed_description'],
        'description_normalization':public['description_normalization'],'publisher':public['publisher']}
    plan = {'version':version,'state':'verified_plan_only','job_id':job_id,'external_id':public['external_id'],
            'url':public['url'],'changed':False,'attempts':job['attempts'],'same_submit_attempt_observed':False,
            'new_uploads':0,'submit_clicks':0,'evidence_sha256':digest(evidence)}
    if not apply: return plan
    player = {**public['player'],'observed_at':public['observed_at'],
              'near_end_playability_observed':public['player']['current_time']>0 and public['player']['current_time']>=max(0,public['player']['duration_seconds']-5),
              'near_end_seek_performed':False}
    coverage = {**expected,'version':'replay-highest-public-coverage-v2','external_id':public['external_id'],'url':public['url'],
        'source_duration_seconds':duration,'upload_coverage_ref':state['upload_observation']['upload_coverage'],'public_player':player,
        'source_to_merged_packet_coverage_verified':True,'server_file_hash_verified':False,'human_review':False}
    file = output/'public-coverage.json'
    if file.exists():
        require(json.loads(read_reference(root,ref(file))) == coverage)
    else:
        fd=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(encode(coverage).encode());stream.flush();os.fsync(stream.fileno())
        if os.name=='posix':
            fd=os.open(output,os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
    observation = {'version':service.RESULT_OBSERVATION_VERSION,'attempt_id':state['attempt_id'],'site_uid':binding['site_uid'],
        'source_url':binding['source_url'],'metadata_sha256':binding['metadata_sha256'],'external_id':public['external_id'],
        'url':public['url'],'state':'completed','public_accessible':True,'public_lookup_observed_at':public['observed_at'],
        'cleanup_coverage':ref(file),'origin':origin,'same_submit_attempt_observed':False,'human_review':False}
    extra=service._result_extra_state(job,actual_binding,state,observation,lookup=True)
    def recheck():
        require(service.store.get(binding['download_job_id']) == source)
        service._validate_snapshot_binding(job,actual_binding,verify_artifact_hash=False)
        for reference in [public_reference,screenshot_reference,manager_reference,before_ref,unknown_ref,proof_ref,
                          prepared['native_observation_file'],prepared['pre_submit_screenshot']]:
            read_reference(root,reference,maximum=20_000_000)
    _commit_later_result(service,principal,job,state,publications,intent_refs,observation,evidence,extra,recheck=recheck,version=version,origin=origin)
    return {**plan,'state':'completed','changed':True}
