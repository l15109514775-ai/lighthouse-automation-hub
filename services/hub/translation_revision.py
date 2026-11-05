"""An explicit, once-only contract upgrade for known failed translations; never publishing."""
import os
import time
import uuid
from .db import encode
from .errors import HubError
from .locks import exclusive
from .model_contract import VERSION
from .model_receipts import ModelReceipts,digest


def retranslate_failed(settings,store,job_id,principal):
    if principal!='maintenance':
        store.audit(principal,'retranslate_tweet',str(job_id),'denied')
        raise HubError('ACCESS_DENIED')
    try:
        if str(uuid.UUID(job_id))!=job_id:raise ValueError()
    except (ValueError,TypeError,AttributeError):raise HubError('INVALID_JOB_ID') from None
    try:
        with exclusive(settings.root/'data/locks/tweet'):
            job=store.get(job_id)
            if (not job or job['job_type']!='tweet_process' or job['queue']!='tweet' or
                    job['state']!='awaiting_review' or job['error_code']!='TRANSLATION_CHECK_FAILED' or
                    job['stage']!='translated' or job['result'].get('translation_contract_version')==VERSION):
                raise HubError('TRANSLATION_UPGRADE_NOT_ELIGIBLE',review=True)
            # A known paid outcome pins this UUID. Correcting a rejected pair
            # now requires the independently bound single-revision node; this
            # legacy maintenance action must not reset the original job.
            if store.query('SELECT 1 FROM model_call_receipts WHERE job_id=? LIMIT 1',(job_id,)):
                raise HubError('MODEL_CONTRACT_PIN_CHANGED',review=True)
            calls=store.query("SELECT state FROM api_calls WHERE job_id=? AND provider='model'",(job_id,))
            if not calls or any(c['state']!='done' for c in calls):
                raise HubError('MODEL_RESULT_UNKNOWN_REVIEW_REQUIRED',review=True)
            if store.query('SELECT 1 FROM publications WHERE job_id=?',(job_id,)):
                raise HubError('TRANSLATION_PUBLICATION_EXISTS',review=True)
            folder=settings.root/'media'/job_id
            names=('source.json','translation.json','machine-evidence-v1.json','model-receipt-v1.json')
            if (folder.is_symlink() or not folder.resolve().is_relative_to((settings.root/'media').resolve()) or
                    any((folder/n).is_symlink() or not (folder/n).is_file() for n in names)):
                raise HubError('TRANSLATION_HISTORY_SOURCE_INVALID',review=True)
            from .model_contract import strict_json
            source=strict_json((folder/'source.json').read_bytes())
            if source!=job['payload']:raise HubError('MODEL_JOB_INPUT_CHANGED',review=True)
            translated=strict_json((folder/'translation.json').read_bytes())['translation']
            proof=ModelReceipts(store).assess(job,translated)
            if not proof['proof_integrity_passed']:
                raise HubError('MODEL_RECEIPT_REQUIRED_FOR_UPGRADE',review=True)
            versions=store.query('SELECT DISTINCT contract_version FROM model_call_receipts WHERE job_id=?',(job_id,))
            if any(r['contract_version']==VERSION for r in versions):
                raise HubError('TRANSLATION_UPGRADE_ALREADY_ATTEMPTED',review=True)
            history=folder/'translation-history'/digest(job['result'])
            if history.is_symlink() or history.parent.is_symlink():
                raise HubError('TRANSLATION_HISTORY_SOURCE_INVALID',review=True)
            history.parent.mkdir(mode=0o700,exist_ok=True);history.mkdir(mode=0o700,exist_ok=True)
            files={n:(folder/n).read_bytes() for n in names}
            files['previous-job.json']=encode(job).encode()
            for name,data in files.items():
                target=history/name
                if target.exists():
                    if target.is_symlink() or target.read_bytes()!=data:raise HubError('TRANSLATION_HISTORY_CHANGED',review=True)
                else:
                    with target.open('xb') as stream:
                        os.chmod(stream.name,0o600);stream.write(data);stream.flush();os.fsync(stream.fileno())
            next_result={'translation_contract_version':VERSION,'translation_history':str(history.relative_to(settings.root)),
                         'previous_result_sha256':digest(job['result'])}
            with store.transaction() as db:
                current=db.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
                if (current['state']!='awaiting_review' or current['updated_at']!=job['updated_at'] or
                        current['result']!=encode(job['result']) or
                        db.execute('SELECT 1 FROM publications WHERE job_id=?',(job_id,)).fetchone()):
                    raise HubError('TRANSLATION_UPGRADE_NOT_ELIGIBLE',review=True)
                db.execute("UPDATE jobs SET state='pending',error_code=NULL,attempts=0,next_run=0,lease_owner=NULL,lease_until=NULL,stage='translation_contract_updated',result=?,updated_at=? WHERE job_id=?",
                    (encode(next_result),time.time(),job_id))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (time.time(),principal,'retranslate_tweet',job_id,'contract_upgrade_queued'))
            return {'job_id':job_id,'state':'pending','contract_version':VERSION,
                    'history':str(history.relative_to(settings.root)),'published':False}
    except OSError:raise HubError('TRANSLATION_UPGRADE_BUSY_OR_STORAGE_ERROR',review=True) from None
