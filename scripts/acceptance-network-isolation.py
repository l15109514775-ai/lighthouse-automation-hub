"""Run with Docker --network none; exercise real connection failure and bounded retries."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
from adapters.http import ProviderHTTP
from services.hub.budget import Budget
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.worker import Worker

current=Settings.load()
with tempfile.TemporaryDirectory(prefix='lighthouse-network-') as folder:
    settings=Settings(Path(folder),deepcopy(current.config))
    settings.prepare()
    store=Store(settings.db_path)
    store.init()
    ident=store.enqueue('synthetic_acceptance','network-none','network_probe','system',{},'acceptance-v1')
    client=ProviderHTTP(Budget(store,settings.config['budgets']))
    def unavailable(job):
        return client.request('youtube','GET','https://example.invalid/',job_id=job['job_id'])
    worker=Worker(settings,store,{'network_probe':unavailable})
    for attempt in range(settings.config['max_attempts']):
        worker.once('system')
        with store.transaction() as db:
            db.execute('UPDATE jobs SET next_run=0 WHERE job_id=?',(ident,))
    job=store.get(ident)
    assert job['state']=='failed' and job['attempts']==settings.config['max_attempts']
    assert job['error_code']=='NETWORK_ERROR'
    assert not worker.once('system')
    print(json.dumps({'isolated_network':True,'state':job['state'],'attempts':job['attempts'],
                      'error_code':job['error_code'],'billable_model_calls':0}))
