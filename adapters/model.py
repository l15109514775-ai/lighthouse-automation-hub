import json
import os
from urllib.parse import urlsplit
from .http import ProviderHTTP
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.model_contract import VERSION,V6,V7,REVISION_CONTRACTS,SUPPORTED_CONTRACTS,strict_json,validate_value,parse_response,payload_for
from services.hub.model_receipts import ModelReceipts,digest
from services.hub.model_revision import validated_revision_context
from services.hub.model_profiles import ModelProfiles


class ModelAPI:
    """Configured text model protocol; receipts never attest human review or publication."""
    def __init__(self,client,store,*,settings=None):
        self.client,self.store=client,store
        self.receipts=ModelReceipts(store,disk_floor_gb=settings.config['disk_floor_gb'] if settings else 15)
        self.profiles=ModelProfiles(store)

    def _spec(self,job,phase,source,translation):
        if phase not in ('translation','quality'):raise HubError('MODEL_PHASE_INVALID',review=True)
        current=self.store.get(job.get('job_id')) if isinstance(job,dict) else None
        if not current:
            raise HubError('MODEL_JOB_REQUIRED',review=True)
        if (not isinstance(source,dict) or (phase=='translation' and translation is not None) or
            (phase=='quality' and (not isinstance(translation,str) or not translation.strip()))):
            raise HubError('MODEL_INPUT_INVALID',review=True)
        if source!=current['payload']:raise HubError('MODEL_JOB_INPUT_CHANGED',review=True)
        contract=current['result'].get('translation_contract_version')
        previous=self.store.query('''SELECT contract_version FROM model_call_receipts WHERE job_id=?
            UNION SELECT contract_version FROM model_request_profiles WHERE job_id=?''',(job['job_id'],job['job_id']))
        if previous:
            # A completed response pins the whole node, including its other
            # phase. Mutable result metadata cannot purchase a new candidate
            # for the same job by switching contracts after a known outcome.
            if len(previous)!=1 or (contract is not None and contract!=previous[0]['contract_version']):
                raise HubError('MODEL_CONTRACT_PIN_CHANGED',review=True)
            contract=previous[0]['contract_version']
        elif contract is None:contract=VERSION
        if contract not in SUPPORTED_CONTRACTS:raise HubError('MODEL_CONTRACT_UNSUPPORTED',review=True)
        if contract in (V6,V7):
            expected_policy='machine-tweet-v3-api' if contract==V6 else 'machine-tweet-v4-api-relations'
            parent=self.store.get(current['result'].get('parent_job_id'))
            if (current['source_platform']!='machine_model' or current['job_type']!='machine_translation_node' or
                    current['policy_version']!=expected_policy or not parent or
                    parent['policy_version'].split(':',1)[0]!=expected_policy):
                raise HubError('MODEL_CONTRACT_V6_MACHINE_API_REQUIRED',review=True)
        with self.store.transaction() as db:self.profiles.require_clear(db,job['job_id'])
        revision=validated_revision_context(self.store,current)
        bound=self.profiles.parameters(job['job_id'],phase)
        style=bound['protocol'] if bound else os.getenv('MODEL_API_STYLE','openai_responses')
        model=bound['requested_model'] if bound else (os.getenv('OPENAI_MODEL' if phase=='translation' else 'OPENAI_QA_MODEL') or os.getenv('MODEL_NAME' if phase=='translation' else 'QA_MODEL_NAME',''))
        suffix='/responses' if style=='openai_responses' else '/chat/completions'
        if bound:
            if not bound['endpoint'].endswith(suffix):raise HubError('MODEL_REQUEST_PROFILE_CHANGED',review=True)
            base=bound['endpoint'][:-len(suffix)]
        else:base=os.getenv('MODEL_BASE_URL','https://api.openai.com/v1' if style=='openai_responses' else '')
        key=os.getenv('OPENAI_API_KEY') or os.getenv('MODEL_API_KEY','')
        hosts=[h.strip() for h in os.getenv('MODEL_ALLOWED_HOSTS','api.openai.com' if style=='openai_responses' else '').split(',') if h.strip()]
        if not base or not key or not model:raise HubError('MODEL_CREDENTIAL_OR_SELECTION_REQUIRED',review=True)
        if key in base or key in model:raise HubError('MODEL_CREDENTIAL_IN_SELECTION',review=True)
        try:parsed=urlsplit(base);port=parsed.port
        except (ValueError,TypeError):raise HubError('MODEL_ENDPOINT_NOT_AUTHORIZED',review=True) from None
        if (parsed.scheme!='https' or parsed.hostname not in hosts or parsed.username or parsed.password or
            parsed.query or parsed.fragment or port not in (None,443)):
            raise HubError('MODEL_ENDPOINT_NOT_AUTHORIZED',review=True)
        request={'source':source,'translation':translation}
        if revision is not None:request['translation_revision']=revision
        try:
            encoded=encode(request);strict_json(encoded)
        except (TypeError,ValueError,HubError):raise HubError('MODEL_INPUT_INVALID',review=True) from None
        if len(encoded)>32000:raise HubError('MODEL_INPUT_BOUND_EXCEEDED',review=True)
        try:maximum=bound['max_output_tokens'] if bound else int(os.getenv('MODEL_MAX_OUTPUT_TOKENS','4096'))
        except ValueError:raise HubError('MODEL_OUTPUT_BOUND_INVALID',review=True) from None
        if not 128<=maximum<=8192:raise HubError('MODEL_OUTPUT_BOUND_INVALID',review=True)
        payload=payload_for(phase,source,translation,style,model,maximum,contract=contract,revision=revision)
        endpoint=base.rstrip('/')+suffix
        compatible_keys=[digest([v,job['job_id'],phase,style,endpoint,
            payload_for(phase,source,translation,style,model,maximum,contract=v,
                revision=revision if v in REVISION_CONTRACTS else None)]) for v in SUPPORTED_CONTRACTS]
        # Keep pre-revision legacy unknown keys blocking as well. The new basis
        # is not allowed to make an old uncertain request disappear.
        legacy_request={'source':source,'translation':translation}
        spec={'request_key':digest([contract,job['job_id'],phase,style,endpoint,payload]),
            'contract_version':contract,'compatible_keys':compatible_keys,
            'legacy_key':bound.get('legacy_key') if bound and bound.get('legacy_key') else digest([job['job_id'],phase,style,base,model,legacy_request]),
            'job_id':job['job_id'],'phase':phase,'protocol':style,'endpoint':endpoint,'model':model,
            'maximum':maximum,'source_sha256':digest(source),'request_sha256':digest(payload),
            'payload':payload,'translation':translation,'_profile_origin':bound['origin'] if bound else 'first_invocation'}
        if bound:
            selected=self.profiles.values(spec)
            if any(bound[k]!=selected[k] for k in selected if k in bound):
                raise HubError('MODEL_REQUEST_PROFILE_CHANGED',review=True)
        return spec,key

    def invoke(self,job,phase,source,translation=None):
        spec,key=self._spec(job,phase,source,translation)
        cached=False;legacy=None
        with self.store.transaction() as db:
            self.profiles.require_clear(db,job['job_id'])
            self.profiles.bind(db,spec)
            previous=db.execute('SELECT * FROM model_requests WHERE request_key=?',(spec['request_key'],)).fetchone()
            old=db.execute('SELECT * FROM model_requests WHERE request_key=?',(spec['legacy_key'],)).fetchone()
            unresolved=db.execute('SELECT 1 FROM model_requests WHERE request_key IN ('+','.join('?' for _ in spec['compatible_keys'])+
                ") AND state IN ('dispatching','unknown')",spec['compatible_keys']).fetchone()
            if unresolved or (old and old['state'] in ('dispatching','unknown')):
                raise HubError('MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED',review=True)
            if previous and previous['state']=='done':cached=True
            elif old and old['state']=='done' and spec['contract_version'] not in (V6,V7):legacy=old['response']
            else:db.execute('INSERT OR REPLACE INTO model_requests VALUES(?,?,NULL,NULL)',(spec['request_key'],'dispatching'))
        if cached:return self.receipts.verify(spec)[0]
        if legacy is not None:
            # Preserve old advice for human preview; never manufacture a receipt for an old call.
            return validate_value(strict_json(legacy),phase)
        try:
            captured=[]
            response=self.client.request('model','POST',spec['endpoint'],job_id=job['job_id'],
                headers={'Authorization':'Bearer '+key},body=spec['payload'],_receipt=captured)
            if len(captured)!=1:raise HubError('MODEL_HTTP_RECEIPT_MISSING',review=True)
            envelope=strict_json(captured[0]['raw_body'])
            if envelope!=response:raise HubError('MODEL_RESPONSE_INTEGRITY_CHANGED',review=True)
            value=parse_response(envelope,phase,spec['protocol'])
            if type(self.client) is not ProviderHTTP:captured[0]['transport_mode']='custom_transport'
            self.receipts.commit(spec,value,captured[0],envelope)
            return value
        except HubError as exc:
            state='unknown' if exc.code=='NETWORK_ERROR' or exc.code.startswith(('MODEL_','DISK_')) else 'rejected'
            with self.store.transaction() as db:
                db.execute('UPDATE model_requests SET state=?,error_code=? WHERE request_key=?',
                    (state,exc.code,spec['request_key']))
            raise
        except (KeyError,ValueError,TypeError,AttributeError,IndexError):
            with self.store.transaction() as db:
                db.execute("UPDATE model_requests SET state='unknown',error_code='MODEL_OUTPUT_INVALID' WHERE request_key=?",(spec['request_key'],))
            raise HubError('MODEL_OUTPUT_INVALID',review=True) from None
        except BaseException:
            with self.store.transaction() as db:
                db.execute("UPDATE model_requests SET state='unknown',error_code='CALL_INTERRUPTED' WHERE request_key=?",(spec['request_key'],))
            raise
