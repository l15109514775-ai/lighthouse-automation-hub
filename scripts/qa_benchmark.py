"""Frozen, one-shot OS-operator QA comparison. No publication capability."""
from pathlib import Path
import argparse,hashlib,json,os,time,uuid
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.model_contract import payload_for
from services.hub.evidence import sha256_file
from services.hub.web_source import WebSources

VERSION='qa-benchmark-v1'
CONTRACT='model-text-contract-v5'
PLAN_SHA='f01fc2787eceabf198090b1a6e99a04a12d440abfa58b2b449e1be8ca82c5bf8'
DOCUMENT_SHA='e72fe60a6ee139b77636d3d7c19507385c9b5aae8666b0bc85b7f5aff7111469'
NAMESPACE=uuid.UUID('ff82e934-48c9-49a7-9006-50ca15f492f9')

def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()

def selected_model(model):
    class Selection:
        def __enter__(self):
            self.old={k:os.environ.get(k) for k in ('OPENAI_QA_MODEL','QA_MODEL_NAME')}
            os.environ.update(OPENAI_QA_MODEL=model,QA_MODEL_NAME=model)
        def __exit__(self,*args):
            for k,v in self.old.items():
                if v is None:os.environ.pop(k,None)
                else:os.environ[k]=v
    return Selection()

class QABenchmark:
    def __init__(self,settings,store,model_factory):self.settings,self.store,self.model_factory=settings,store,model_factory

    def _set(self,job_id,state,result,code=None):
        with self.store.transaction() as db:
            db.execute('UPDATE jobs SET state=?,result=?,error_code=?,updated_at=? WHERE job_id=?',
                (state,encode(result),code,time.time(),job_id))

    def _insert(self,db,ident,source_id,kind,payload,result):
        now=time.time()
        db.execute("INSERT OR IGNORE INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload,result) VALUES(?,'qa_benchmark',?,?,'system','upload_pending',?,?,?,?,?)",
            (ident,source_id,kind,now,now,VERSION,encode(payload),encode(result)))
        row=db.execute('SELECT * FROM jobs WHERE job_id=?',(ident,)).fetchone()
        if (not row or row['source_platform']!='qa_benchmark' or row['source_id']!=source_id or
            row['job_type']!=kind or row['policy_version']!=VERSION or json.loads(row['payload'])!=payload):
            raise HubError('QA_BENCHMARK_PLAN_CHANGED')

    def run(self,file,*,catalog_job_id):
        raw=Path(file).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=PLAN_SHA:raise HubError('QA_BENCHMARK_PLAN_CHANGED')
        config=json.loads(raw);catalog=self.store.get(catalog_job_id)
        if (not catalog or catalog['state']!='completed' or catalog['job_type']!='model_catalog_check' or
            not set(config['models'])<=set(catalog['result'].get('ids',[]))):raise HubError('QA_BENCHMARK_MODEL_NOT_LISTED')
        style=os.getenv('MODEL_API_STYLE','openai_responses');maximum=int(os.getenv('MODEL_MAX_OUTPUT_TOKENS','4096'))
        base=os.environ['MODEL_BASE_URL'].rstrip('/')
        # Frozen request bytes exclude labels and include the unchanged V5 rubric.
        cells=[];parent_id=str(uuid.uuid5(NAMESPACE,VERSION+':'+PLAN_SHA))
        for case in config['cases']:
            for model in config['models']:
                ident=str(uuid.uuid5(NAMESPACE,VERSION+':'+case['id']+':'+model))
                body=payload_for('quality',case['source'],case['translation'],style,model,maximum,contract=CONTRACT)
                cells.append({'job_id':ident,'case_id':case['id'],'model':model,'source':case['source'],
                    'translation':case['translation'],'expected':case['expected'],'request_payload':body,
                    'request_sha256':digest(body),'source_sha256':digest(case['source'])})
        plan={'version':VERSION,'config_sha256':PLAN_SHA,'document_sha256':DOCUMENT_SHA,'contract':CONTRACT,'style':style,'base_url':base,
            'max_output_tokens':maximum,'catalog_job_id':catalog_job_id,'max_http_dispatches':16,
            'labels_sent_to_model':False,'publication_authorized':False,'human_review':False,'cells':cells}
        with resource_guard(self.settings.root/'data/locks/qa-benchmark-v1'):
            folder=self.settings.root/'media'/parent_id;folder.mkdir(mode=0o700,exist_ok=True)
            manifest=folder/'benchmark-plan.json'
            if manifest.exists():
                if manifest.is_symlink() or manifest.read_bytes()!=encode(plan).encode():raise HubError('QA_BENCHMARK_PLAN_CHANGED')
            else:WebSources._write_durable(manifest,encode(plan).encode());WebSources._sync_dir(folder)
            with self.store.transaction() as db:
                self._insert(db,parent_id,VERSION,'qa_benchmark_plan',plan,{'plan_sha256':digest(plan),'publication_authorized':False,'human_review':False})
                for cell in cells:
                    self._insert(db,cell['job_id'],VERSION+':'+cell['case_id']+':'+cell['model'],'qa_benchmark_cell',cell['source'],
                        {'translation_contract_version':CONTRACT,'cell_state':'prepared','plan_job_id':parent_id,
                        'request_sha256':cell['request_sha256'],'publication_authorized':False,'human_review':False})
            parent=self.store.get(parent_id)
            if parent['state']=='failed':raise HubError('QA_BENCHMARK_TERMINAL_FAILURE')
            results=[]
            try:
                for cell in cells:
                    child=self.store.get(cell['job_id']);saved=child['result']
                    if saved.get('request_sha256')!=cell['request_sha256'] or saved.get('plan_job_id')!=parent_id:
                        raise HubError('QA_BENCHMARK_PLAN_CHANGED')
                    with selected_model(cell['model']):
                        api=self.model_factory()
                        spec,_=api._spec(child,'quality',cell['source'],cell['translation'])
                        if spec['request_sha256']!=cell['request_sha256']:raise HubError('QA_BENCHMARK_REQUEST_CHANGED')
                        cached=self.store.query("SELECT 1 FROM model_requests WHERE request_key=? AND state='done'",(spec['request_key'],))
                        if child['state'] in ('failed','completed') or saved.get('cell_state')=='dispatching':
                            if child['state']=='failed' or not cached:raise HubError('QA_BENCHMARK_DISPATCH_ALREADY_CONSUMED')
                        else:self._set(child['job_id'],'upload_pending',{**saved,'cell_state':'dispatching','dispatch_at':time.time()})
                        value=api.invoke(child,'quality',cell['source'],cell['translation'])
                        # verify() reads actual raw/cache/API records, without a second request.
                        parsed,receipt=api.receipts.verify(spec)
                        if parsed!=value:raise HubError('QA_BENCHMARK_RECEIPT_CHANGED')
                        shape=(value['passed'] is True and not value['issues']) or (value['passed'] is False and bool(value['issues']))
                        if not shape:raise HubError('QA_BENCHMARK_INCONSISTENT_VERDICT')
                        scored={'case_id':cell['case_id'],'model':cell['model'],'expected':cell['expected'],'quality':value,
                            'verdict_matches':shape and value['passed'] is cell['expected'],'raw_call_id':receipt['call_id'],
                            'raw_response_sha256':receipt['response_wire_sha256'],'request_key':receipt['request_key'],
                            'transport_mode':receipt['transport_mode'],'semantic_issue_coverage':'pending_independent_assessment',
                            'human_review':False,'publication_authorized':False}
                        self._set(child['job_id'],'completed',{**self.store.get(child['job_id'])['result'],'cell_state':'completed',**scored})
                        results.append(scored)
                result={'version':VERSION,'plan_sha256':digest(plan),'plan_file':str(manifest),'plan_file_sha256':sha256_file(manifest),
                    'results':results,'per_model_verdicts':{m:sum(x['verdict_matches'] for x in results if x['model']==m) for m in config['models']},
                    'model_promotion_authorized':False,'publication_authorized':False,'human_review':False}
                self._set(parent_id,'completed',result)
                return {'job_id':parent_id,**result}
            except BaseException as exc:
                code=getattr(exc,'code','QA_BENCHMARK_FAILED')
                if 'child' in locals() and self.store.get(child['job_id'])['state']!='completed':
                    self._set(child['job_id'],'failed',{**self.store.get(child['job_id'])['result'],'cell_state':'failed','error_code':code},code)
                self._set(parent_id,'failed',{**parent['result'],'completed_cells':results,'error_code':code,'model_promotion_authorized':False},code)
                raise

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--execute',action='store_true');args=parser.parse_args()
    if not args.execute:raise SystemExit('QA_BENCHMARK_EXPLICIT_EXECUTION_REQUIRED')
    from services.hub.config import Settings
    from services.hub.db import Store
    from services.hub.budget import Budget
    from adapters.http import ProviderHTTP
    from adapters.model import ModelAPI
    s=Settings.load();d=Store(s.db_path)
    catalog=json.loads((s.root/'backups/relay-model-catalog-20261101.json').read_text('utf-8'))
    api=lambda:ModelAPI(ProviderHTTP(Budget(d,s.config['budgets'])),d,settings=s)
    result=QABenchmark(s,d,api).run(s.root/'config/qa-benchmark-v1.json',catalog_job_id=catalog['job_id'])
    out=s.root/'backups/qa-benchmark-v1-results-20261101.json';WebSources._write_durable(out,encode(result).encode())
    print(encode(result))

if __name__=='__main__':main()
