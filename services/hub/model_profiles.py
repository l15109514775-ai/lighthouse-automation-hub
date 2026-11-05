"""Durable request identity per model job and phase; never stores credentials.

Changing environment selection affects new jobs only.  Existing identities use
their original parameters, and a changed source/candidate cannot buy a second
request in the same phase.  ModelReceipts and historical wire contracts retain
their original schemas and assessments.
"""
import json
import time
from .errors import HubError
from .model_receipts import digest


FIELDS = ('job_id','phase','request_key','request_sha256','source_sha256',
          'contract_version','protocol','endpoint','requested_model','max_output_tokens','legacy_key')
RECEIPT_FIELDS = tuple(k for k in FIELDS if k != 'legacy_key')


class ModelProfiles:
    def __init__(self,store):self.store=store

    @staticmethod
    def require_clear(db,job_id):
        # A timed-out charge belongs to the job even after endpoint/model/phase
        # changes.  Profiles also cover crashes before HTTP budget reservation.
        if (db.execute("SELECT 1 FROM api_calls WHERE job_id=? AND provider='model' AND state IN ('reserved','unknown') LIMIT 1",
                       (job_id,)).fetchone() or
            db.execute("""SELECT 1 FROM model_request_profiles p JOIN model_requests r USING(request_key)
                WHERE p.job_id=? AND r.state IN ('dispatching','unknown') LIMIT 1""",(job_id,)).fetchone()):
            raise HubError('MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED',review=True)

    def parameters(self,job_id,phase):
        rows=self.store.query('SELECT * FROM model_request_profiles WHERE job_id=? AND phase=?',(job_id,phase))
        if rows:return rows[0]
        fields=','.join(RECEIPT_FIELDS)
        rows=self.store.query('SELECT DISTINCT '+fields+' FROM model_call_receipts WHERE job_id=? AND phase=?',(job_id,phase))
        if len(rows)>1:raise HubError('MODEL_REQUEST_PROFILE_AMBIGUOUS',review=True)
        return {**rows[0],'origin':'existing_receipt'} if rows else None

    @staticmethod
    def values(spec):
        return {k:spec[{'requested_model':'model','max_output_tokens':'maximum'}.get(k,k)] for k in FIELDS}

    def bind(self,db,spec):
        values=self.values(spec)
        # Recheck whole-node pins under the dispatch transaction as well as in
        # _spec: two differently prepared phases must not race a contract edit.
        current=db.execute('SELECT payload,result FROM jobs WHERE job_id=?',(spec['job_id'],)).fetchone()
        if not current or digest(json.loads(current['payload']))!=spec['source_sha256']:
            raise HubError('MODEL_JOB_INPUT_CHANGED',review=True)
        pin=json.loads(current['result']).get('translation_contract_version')
        contracts=db.execute('''SELECT contract_version FROM model_call_receipts WHERE job_id=?
            UNION SELECT contract_version FROM model_request_profiles WHERE job_id=?''',
            (spec['job_id'],spec['job_id'])).fetchall()
        if (pin is not None and pin!=spec['contract_version'] or
            any(row['contract_version']!=spec['contract_version'] for row in contracts)):
            raise HubError('MODEL_CONTRACT_PIN_CHANGED',review=True)
        existing=db.execute('SELECT 1 FROM model_request_profiles WHERE job_id=? AND phase=?',
                            (spec['job_id'],spec['phase'])).fetchone()
        origin=spec['_profile_origin']
        if not existing:
            history=db.execute('SELECT DISTINCT '+','.join(RECEIPT_FIELDS)+
                               ' FROM model_call_receipts WHERE job_id=? AND phase=?',
                               (spec['job_id'],spec['phase'])).fetchall()
            if len(history)>1:raise HubError('MODEL_REQUEST_PROFILE_AMBIGUOUS',review=True)
            if history:
                if any(history[0][k]!=values[k] for k in RECEIPT_FIELDS):
                    raise HubError('MODEL_REQUEST_PROFILE_CHANGED',review=True)
                origin='existing_receipt'
        # The insert and comparison share the transaction which marks the
        # request dispatching.  Concurrent different selections cannot both pay.
        db.execute('INSERT OR IGNORE INTO model_request_profiles('+','.join(FIELDS)+',bound_at,origin) VALUES('
                   +','.join('?' for _ in range(len(FIELDS)+2))+')',
                   (*values.values(),time.time(),origin))
        row=db.execute('SELECT * FROM model_request_profiles WHERE job_id=? AND phase=?',
                       (spec['job_id'],spec['phase'])).fetchone()
        if not row or any(row[k]!=values[k] for k in FIELDS):
            raise HubError('MODEL_REQUEST_PROFILE_CHANGED',review=True)
