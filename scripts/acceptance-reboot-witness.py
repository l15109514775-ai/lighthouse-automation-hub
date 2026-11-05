"""Claim a real local backup job, leaving a processing checkpoint for reboot recovery."""
from pathlib import Path
import json
import time
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.locks import exclusive
from services.hub.maintenance import Maintenance

settings=Settings.load()
store=Store(settings.db_path)
store.init()
maintenance=Maintenance(settings,store)
handlers={'maintenance':maintenance.monitor,'backup':maintenance.backup_job,'cleanup':maintenance.cleanup}
with exclusive(settings.root / 'data/locks/system'):
    ident=store.enqueue('synthetic_acceptance','backup-reboot-witness-v1','backup','system',
                        {'acceptance_only':True},settings.config['policy_version'])
    while True:
        job=store.claim('system',600,settings.config['max_attempts'])
        if not job:
            raise SystemExit('NO_BACKUP_JOB_CLAIMED')
        if job['job_id']==ident:
            break
        result=handlers[job['job_type']](job)
        store.finish(job,'completed',result=result)
    evidence={'job_id':ident,'before_reboot_state':job['state'],'attempts':job['attempts'],
              'prepared_at':time.time(),'method':'real Store.claim; execution intentionally held until host reboot'}
    file=settings.root / 'media/synthetic-acceptance/reboot-witness.json'
    file.write_text(json.dumps(evidence,indent=2),'utf-8')
    print(json.dumps(evidence),flush=True)
    time.sleep(600)
