"""A single correction based on a verified rejected pair, never publication authority.

The job payload stays identical to the original model node. Mutable job.result
only names a basis: every use reconstructs it from the recorded response bytes,
cache, request identities and API ledger before any paid call or assessment.
"""
import re
import uuid

from .errors import HubError
from .model_contract import V6,V7,REVISION_CONTRACTS

VERSION='model-translation-revision-v1'
CONTRACT='model-text-contract-v5'
POLICY='machine-tweet-v2'
SUPPORTED_POLICIES={POLICY,'machine-tweet-v3-api'}
FIELDS={'version','index','previous_model_job_id','source_sha256','previous_translation_sha256',
        'previous_quality_sha256','translation_request_key','quality_request_key'}


def _invalid(code='MODEL_REVISION_CONTEXT_INVALID'):
    raise HubError(code,review=True)


def _uuid(value):
    try:
        if not isinstance(value,str) or str(uuid.UUID(value))!=value:_invalid()
    except (ValueError,TypeError,AttributeError):_invalid()


def _node(store,job,parent_job_id,role,source,*,revised):
    try:
        _uuid(parent_job_id)
        parent=store.get(parent_job_id)
        if not parent:_invalid()
        # v2 historically permits model-only owned/synthetic parents; retaining
        # that meaning does not grant publication. API nodes additionally bind
        # their new policy to the API parent and cannot borrow a v2 correction.
        parent_policy=parent['policy_version'].split(':',1)[0]
        if role not in (('post','parent','quote') if parent_policy=='machine-tweet-v4-api-relations' else ('post','parent')):_invalid()
        expected_policy=parent_policy if parent_policy in ('machine-tweet-v3-api','machine-tweet-v4-api-relations') else POLICY
        suffix=':revision-1' if revised else ''
        if (not job or job['source_platform']!='machine_model' or job['job_type']!='machine_translation_node' or
                job['policy_version']!=expected_policy or job['source_id']!=parent_job_id+':'+role+suffix or
                job['state'] not in ('upload_pending','completed','failed') or job['payload']!=source or
                job['result'].get('parent_job_id')!=parent_job_id or job['result'].get('role')!=role):_invalid()
    except (ValueError,TypeError,KeyError,AttributeError):_invalid()


def _base_pair(store,previous_model_job_id,*,parent_job_id,role,source,required_contract=None):
    # Local import avoids a cycle: receipt assessment also validates revisions.
    from .model_receipts import ModelReceipts,digest,text_hash
    from .model_contract import SUPPORTED_CONTRACTS,payload_for,strict_json
    _uuid(previous_model_job_id)
    previous=store.get(previous_model_job_id)
    _node(store,previous,parent_job_id,role,source,revised=False)
    if previous['result'].get('translation_revision') is not None:_invalid('MODEL_REVISION_LIMIT_REACHED')
    if store.query("SELECT 1 FROM api_calls WHERE job_id=? AND provider='model' AND state IN ('reserved','unknown') LIMIT 1",
                   (previous_model_job_id,)):_invalid('MODEL_REVISION_BASIS_UNKNOWN')
    receipts=ModelReceipts(store)
    translations=store.query('''SELECT request_key FROM model_call_receipts WHERE job_id=? AND phase='translation'
        AND source_sha256=? ORDER BY finished_at DESC,request_key DESC LIMIT 1''',
        (previous_model_job_id,digest(source)))
    if not translations:_invalid('MODEL_REVISION_BASIS_INVALID')
    candidate=receipts._row(translations[0]['request_key'])
    old_value=strict_json(candidate['cache_response'])
    old_translation=old_value.get('translation')
    proof=receipts.assess(previous,old_translation)
    if not proof['proof_integrity_passed'] or not proof['separate_requests_verified']:
        _invalid('MODEL_REVISION_BASIS_INVALID')
    rows=[receipts._row(key) for key in proof['request_keys']]
    versions={row['contract_version'] for row in rows}
    if V6 in versions or V7 in versions:
        contract=V7 if V7 in versions else V6
        expected='machine-tweet-v4-api-relations' if contract==V7 else 'machine-tweet-v3-api'
        parent=store.get(parent_job_id)
        if (versions!={contract} or not parent or parent['policy_version'].split(':',1)[0]!=expected or
                required_contract not in (None,contract)):_invalid()
    if required_contract in (V6,V7) and versions!={required_contract}:_invalid('MODEL_REVISION_BASIS_CONTRACT_CHANGED')
    quality=strict_json(rows[1]['cache_response'])
    if quality.get('passed') is not False or not quality.get('issues'):_invalid('MODEL_REVISION_REJECTION_REQUIRED')
    # Block an unresolved alternate-version request even when the selected pair
    # itself is complete; upgrading cannot bypass a previous uncertain charge.
    for phase,row in zip(('translation','quality'),rows):
        translation=None if phase=='translation' else old_translation
        keys=[digest([version,previous_model_job_id,phase,row['protocol'],row['endpoint'],
              payload_for(phase,source,translation,row['protocol'],row['requested_model'],row['max_output_tokens'],contract=version)])
              for version in SUPPORTED_CONTRACTS]
        suffix='/responses' if row['protocol']=='openai_responses' else '/chat/completions'
        base=row['endpoint'][:-len(suffix)]
        keys.append(digest([previous_model_job_id,phase,row['protocol'],base,row['requested_model'],
                           {'source':source,'translation':translation}]))
        if store.query('SELECT 1 FROM model_requests WHERE request_key IN ('+','.join('?' for _ in keys)+
                       ") AND state IN ('dispatching','unknown') LIMIT 1",keys):_invalid('MODEL_REVISION_BASIS_UNKNOWN')
    basis={'version':VERSION,'index':1,'previous_model_job_id':previous_model_job_id,'source_sha256':digest(source),
           'previous_translation_sha256':text_hash(old_translation),'previous_quality_sha256':digest(quality),
           'translation_request_key':rows[0]['request_key'],'quality_request_key':rows[1]['request_key']}
    context={'previous_translation':old_translation,'quality_issues':quality['issues']}
    return basis,context


def build_revision_context(store,previous_model_job_id,*,parent_job_id,role,source):
    """Produce a durable reference from an actual rejected original model pair."""
    try:
        return _base_pair(store,previous_model_job_id,parent_job_id=parent_job_id,role=role,source=source)[0]
    except (ValueError,TypeError,KeyError,AttributeError):_invalid()


def validated_revision_context(store,job):
    """Reconstruct model-only correction text; supplied booleans never attest it."""
    try:
        _uuid(job['job_id'])
        current=store.get(job['job_id'])
        if not current or current['payload']!=job['payload']:_invalid()
        revision=current['result'].get('translation_revision')
        if revision is None:
            # A canonical revision slot cannot lose its context and become a
            # second base request merely by removing a mutable result field.
            if (current['source_platform']=='machine_model' and ':revision-' in current['source_id']):_invalid()
            return None
        if (not isinstance(revision,dict) or set(revision)!=FIELDS or revision.get('version')!=VERSION or
                type(revision.get('index')) is not int or revision['index']!=1 or
                current['result'].get('translation_contract_version') not in REVISION_CONTRACTS):_invalid()
        _uuid(revision['previous_model_job_id'])
        if any(not isinstance(revision[field],str) or not re.fullmatch('[a-f0-9]{64}',revision[field])
               for field in FIELDS-{'version','index','previous_model_job_id'}):_invalid()
        parent=current['result'].get('parent_job_id');role=current['result'].get('role')
        _node(store,current,parent,role,current['payload'],revised=True)
        contract=current['result']['translation_contract_version']
        if contract==V6 and store.get(parent)['policy_version'].split(':',1)[0]!='machine-tweet-v3-api':_invalid()
        if contract==V7 and store.get(parent)['policy_version'].split(':',1)[0]!='machine-tweet-v4-api-relations':_invalid()
        if revision['previous_model_job_id']==current['job_id']:_invalid('MODEL_REVISION_LIMIT_REACHED')
        basis,context=_base_pair(store,revision['previous_model_job_id'],parent_job_id=parent,role=role,
            source=current['payload'],required_contract=contract)
        if basis!=revision:_invalid('MODEL_REVISION_BASIS_CHANGED')
        return context
    except (ValueError,TypeError,KeyError,AttributeError):_invalid()
