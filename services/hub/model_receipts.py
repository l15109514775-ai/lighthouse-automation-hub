"""API/cache provenance for future machine verification; never human or publishing approval."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import time
from urllib.parse import urlsplit
import uuid
from .db import encode
from .errors import HubError
from .model_contract import VERSION as CONTRACT,SUPPORTED_CONTRACTS,strict_json,parse_response,payload_for
from .model_revision import validated_revision_context

VERSION='model-receipt-v1'


def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()
def text_hash(text):return hashlib.sha256(text.encode()).hexdigest()


class ModelReceipts:
    def __init__(self,store,*,disk_floor_gb=15):
        self.store=store;self.base=store.path.parent.resolve();self.disk_floor_gb=max(15,disk_floor_gb)

    def _file(self,job_id,call_id):
        try:
            if str(uuid.UUID(job_id))!=job_id or str(uuid.UUID(call_id))!=call_id:raise ValueError()
            file=self.base/'model-receipts'/job_id/(call_id+'.json')
            if (file.is_symlink() or any(p.is_symlink() for p in file.parents if p!=self.base.parent) or
                    not file.resolve().is_relative_to(self.base)):
                raise ValueError()
            return file
        except (ValueError,TypeError,AttributeError,OSError):
            raise HubError('MODEL_RECEIPT_PATH_INVALID',review=True) from None

    def commit(self,spec,value,capture,envelope):
        raw=capture.get('raw_body')
        if (not isinstance(raw,bytes) or len(raw)>5*1024*1024 or
            capture.get('request_sha256')!=spec['request_sha256'] or
            capture.get('request_method')!='POST' or capture.get('endpoint_sha256')!=text_hash(spec['endpoint']) or
            not re.fullmatch('[0-9a-f]{64}',capture.get('request_wire_sha256','')) or
            capture.get('transport_mode') not in ('default_tls','custom_transport')):
            raise HubError('MODEL_HTTP_RECEIPT_MISSING',review=True)
        for name in ('MODEL_API_KEY','OPENAI_API_KEY','YOUTUBE_API_KEY','X_BEARER_TOKEN',
                     'HUB_READ_TOKEN','HUB_MAINTAIN_TOKEN','EVENT_ENCRYPTION_KEY',
                     'BILIBILI_ACCESS_KEY_SECRET','BILIBILI_ACCESS_TOKEN'):
            secret=os.getenv(name,'')
            if len(secret)>16 and any(secret.encode() in part for part in (raw,encode(envelope).encode(),encode(value).encode())):
                raise HubError('MODEL_RESPONSE_CONTAINS_SECRET',review=True)
        rows=self.store.query('SELECT * FROM api_calls WHERE call_id=?',(capture['call_id'],))
        if not rows or rows[0]['provider']!='model' or rows[0]['job_id']!=spec['job_id'] or rows[0]['state']!='done':
            raise HubError('MODEL_API_CALL_NOT_CONFIRMED',review=True)
        call=rows[0];file=self._file(spec['job_id'],capture['call_id'])
        if shutil.disk_usage(self.base).free-len(raw)<self.disk_floor_gb*1_000_000_000:
            self.store.event('disk.low',{'component':'model-receipts'},dedup_key='model-receipt-disk')
            raise HubError('DISK_LOW',review=True)
        try:
            (self.base/'model-receipts').mkdir(mode=0o700,exist_ok=True)
            file.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            with file.with_suffix('.partial').open('xb') as stream:
                os.chmod(stream.name,0o600);stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.replace(file.with_suffix('.partial'),file)
            if os.name!='nt':
                descriptor=os.open(file.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
            translation=value['translation'] if spec['phase']=='translation' else spec['translation']
            with self.store.transaction() as db:
                current=db.execute('SELECT * FROM api_calls WHERE call_id=?',(call['call_id'],)).fetchone()
                if (current['state']!='done' or current['finished_at']!=call['finished_at'] or
                    current['provider']!='model' or current['job_id']!=spec['job_id']):
                    raise HubError('MODEL_API_CALL_NOT_CONFIRMED',review=True)
                changed=db.execute("UPDATE model_requests SET state='done',response=?,error_code=NULL WHERE request_key=? AND state='dispatching'",
                    (encode(value),spec['request_key'])).rowcount
                if changed!=1:raise HubError('MODEL_REQUEST_STATE_CHANGED',review=True)
                returned=envelope.get('model');returned=returned if isinstance(returned,str) and len(returned)<=256 else None
                db.execute('INSERT INTO model_call_receipts VALUES('+','.join('?'*18)+')',
                    (spec['request_key'],call['call_id'],spec['job_id'],spec['phase'],spec.get('contract_version',CONTRACT),spec['protocol'],
                     spec['endpoint'],spec['model'],returned,spec['maximum'],spec['source_sha256'],text_hash(translation),
                     spec['request_sha256'],capture['request_wire_sha256'],hashlib.sha256(raw).hexdigest(),digest(value),
                     capture['transport_mode'],call['finished_at']))
        except OSError:
            raise HubError('MODEL_RECEIPT_PERSIST_FAILED',review=True) from None

    def _row(self,key):
        rows=self.store.query('''SELECT m.*,r.state cache_state,r.response cache_response,
            a.state api_state,a.provider api_provider,a.job_id api_job_id,a.finished_at api_finished_at
            FROM model_call_receipts m JOIN model_requests r ON r.request_key=m.request_key
            JOIN api_calls a ON a.call_id=m.call_id WHERE m.request_key=?''',(key,))
        if not rows:raise HubError('MODEL_RECEIPT_MISSING',review=True)
        return rows[0]

    def verify(self,spec):
        row=self._row(spec['request_key'])
        if (row['cache_state']!='done' or row['api_state']!='done' or row['api_provider']!='model' or
            row['api_job_id']!=spec['job_id'] or row['finished_at']!=row['api_finished_at'] or
            not 0<row['finished_at']<=time.time()+30):
            raise HubError('MODEL_API_CALL_NOT_CONFIRMED',review=True)
        expected={'job_id':spec['job_id'],'phase':spec['phase'],'contract_version':spec.get('contract_version',CONTRACT),'protocol':spec['protocol'],
            'endpoint':spec['endpoint'],'requested_model':spec['model'],'max_output_tokens':spec['maximum'],
            'source_sha256':spec['source_sha256'],'request_sha256':spec['request_sha256']}
        if any(row[k]!=v for k,v in expected.items()):raise HubError('MODEL_RECEIPT_INPUT_CHANGED',review=True)
        file=self._file(row['job_id'],row['call_id'])
        try:
            if not file.is_file() or file.stat().st_size>5*1024*1024:
                raise HubError('MODEL_RESPONSE_FILE_MISSING',review=True)
            raw=file.read_bytes()
        except OSError:raise HubError('MODEL_RESPONSE_FILE_MISSING',review=True) from None
        if hashlib.sha256(raw).hexdigest()!=row['response_wire_sha256']:
            raise HubError('MODEL_RESPONSE_INTEGRITY_CHANGED',review=True)
        parsed=parse_response(strict_json(raw),spec['phase'],spec['protocol'])
        translation=parsed['translation'] if spec['phase']=='translation' else spec['translation']
        if (digest(parsed)!=row['response_sha256'] or strict_json(row['cache_response'])!=parsed or
            text_hash(translation)!=row['translation_sha256']):
            raise HubError('MODEL_RESPONSE_INTEGRITY_CHANGED',review=True)
        return parsed,row

    def assess(self,job,translation):
        result={'version':VERSION,'proof_integrity_passed':False,'separate_requests_verified':False,
            'live_transport_verified':False,'human_review':False,'auto_publish_eligible':False,'failures':[],
            'limitations':['Proves recorded requests and output integrity, not semantic accuracy, source facts, rights or human review.']}
        try:
            if not isinstance(translation,str) or not translation:raise HubError('MODEL_TRANSLATION_REQUIRED',review=True)
            current=self.store.get(job['job_id'])
            if not current or current['payload']!=job['payload'] or current['source_id']!=job['source_id']:
                raise HubError('MODEL_JOB_INPUT_CHANGED',review=True)
            revision=validated_revision_context(self.store,current)
            source=current['payload'];source_hash=digest(source);rows=[];values=[]
            for phase in ('translation','quality'):
                matches=self.store.query('''SELECT request_key FROM model_call_receipts WHERE job_id=? AND phase=?
                    AND source_sha256=? AND translation_sha256=? ORDER BY finished_at DESC,request_key DESC LIMIT 1''',
                    (job['job_id'],phase,source_hash,text_hash(translation)))
                if not matches:raise HubError('MODEL_RECEIPT_MISSING',review=True)
                row=self._row(matches[0]['request_key'])
                endpoint=urlsplit(row['endpoint'])
                if (endpoint.scheme!='https' or not endpoint.hostname or endpoint.username or endpoint.password or
                    endpoint.query or endpoint.fragment or row['contract_version'] not in SUPPORTED_CONTRACTS or
                    type(row['max_output_tokens']) is not int or not 128<=row['max_output_tokens']<=8192):
                    raise HubError('MODEL_RECEIPT_INPUT_CHANGED',review=True)
                payload=payload_for(phase,source,None if phase=='translation' else translation,row['protocol'],
                    row['requested_model'],row['max_output_tokens'],contract=row['contract_version'],revision=revision)
                key=digest([row['contract_version'],job['job_id'],phase,row['protocol'],row['endpoint'],payload])
                if key!=row['request_key']:raise HubError('MODEL_RECEIPT_INPUT_CHANGED',review=True)
                value,row=self.verify({'request_key':key,'job_id':job['job_id'],'phase':phase,
                    'contract_version':row['contract_version'],
                    'protocol':row['protocol'],'endpoint':row['endpoint'],'model':row['requested_model'],
                    'maximum':row['max_output_tokens'],'source_sha256':source_hash,'request_sha256':digest(payload),
                    'translation':None if phase=='translation' else translation})
                rows.append(row);values.append(value)
            if rows[0]['call_id']==rows[1]['call_id'] or values[0]['translation']!=translation:
                raise HubError('MODEL_INDEPENDENT_REQUESTS_REQUIRED',review=True)
            result.update(proof_integrity_passed=True,separate_requests_verified=True,
                live_transport_verified=all(r['transport_mode']=='default_tls' for r in rows),
                request_keys=[r['request_key'] for r in rows],call_ids=[r['call_id'] for r in rows],
                source_sha256=source_hash,translation_sha256=text_hash(translation),
                requested_models=[r['requested_model'] for r in rows],returned_models=[r['returned_model'] for r in rows],
                transport_modes=[r['transport_mode'] for r in rows],
                qa_recommends_pass=values[1]['passed'] is True and not values[1]['issues'])
            if revision is not None:result['revision_basis']=current['result']['translation_revision']
            if not result['live_transport_verified']:result['failures'].append('MODEL_CUSTOM_TRANSPORT_NOT_LIVE_VERIFIED')
        except HubError as exc:result['failures'].append(exc.code)
        except (ValueError,TypeError,KeyError,AttributeError):result['failures'].append('MODEL_RECEIPT_DOCUMENT_INVALID')
        return result

    def status(self):
        return {'version':VERSION,'http_receipts':self.store.query('SELECT COUNT(*) n FROM model_call_receipts')[0]['n'],
            'live_http_receipts':self.store.query("SELECT COUNT(*) n FROM model_call_receipts WHERE transport_mode='default_tls'")[0]['n'],
            'legacy_done_cache_rows':self.store.query('''SELECT COUNT(*) n FROM model_requests r
                LEFT JOIN model_call_receipts m ON m.request_key=r.request_key WHERE r.state='done' AND m.request_key IS NULL''')[0]['n'],
            'publishing_enabled_by_receipts':False}
