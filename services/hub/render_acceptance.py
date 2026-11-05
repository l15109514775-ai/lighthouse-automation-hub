"""Real-source/model/render acceptance with no publishing capability.

Only the authenticated OS operator invokes this helper. Acceptance jobs use a
separate platform and job type, so MachineTweetsV2._snapshot rejects them even
if an operator tries to pass the resulting render to the publisher. Old source
timestamps and owner-cleared production jobs are never modified.
"""
from pathlib import Path
import json,time,uuid
from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .extended_web_source import ExtendedWebSources
from .machine_tweets_v2 import MachineTweetsV2
from .model_contract import VERSION as MODEL_CONTRACT
from .model_revision import build_revision_context
from .model_receipts import ModelReceipts
from .source_receipts import digest
from .web_source import WebSources


class RenderAcceptance:
    def __init__(self,settings,store):self.settings,self.store=settings,store

    def run(self,discovery,record,model):
        validator=ExtendedWebSources(self.settings,self.store)
        acceptance_policy='machine-render-acceptance-v1:'+MODEL_CONTRACT
        source_id=record['payload']['post']['id']
        with self.store.transaction() as db:
            WebSources(self.settings,self.store)._parent(discovery,db)
            if db.execute("SELECT 1 FROM jobs WHERE source_platform='acceptance' AND source_id=? AND job_type='machine_render_acceptance' AND policy_version=?",
                (source_id,acceptance_policy)).fetchone():raise HubError('RENDER_ACCEPTANCE_ALREADY_RECORDED')
        ident=str(uuid.uuid4());folder=self.settings.root/'media'/ident;folder.mkdir(mode=0o700)
        file=folder/'acceptance-source-packet.json';WebSources._write_durable(file,encode(record).encode())
        WebSources._sync_dir(folder)
        checked=validator._verify(discovery['job_id'],record,file)
        now=time.time()
        with self.store.transaction() as db:
            WebSources(self.settings,self.store)._parent(discovery,db)
            db.execute('''INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,
                policy_version,payload,stage) VALUES(?,'acceptance',?,'machine_render_acceptance','tweet','upload_pending',?,?,?,?,?)''',
                (ident,source_id,now,now,acceptance_policy,encode(record['payload']),'render_only_acceptance'))
        return self._execute(self.store.get(ident),discovery['job_id'],record,file,checked,model)

    def resume_rejected(self,job_id,model):
        """OS-operator-only, one-time resume of a known original QA denial.

        The original source, model pair, policy pin and rejection remain. This
        does not claim a new capture or lease, and can never queue publication.
        """
        parent=self.store.get(job_id)
        if (not parent or parent['source_platform']!='acceptance' or parent['job_type']!='machine_render_acceptance' or
            parent['state']!='failed' or parent['error_code']!='MACHINE_TRANSLATION_CHECK_FAILED' or
            parent['result'].get('acceptance_resume')):raise HubError('RENDER_ACCEPTANCE_RESUME_FORBIDDEN')
        folder=self.settings.root/'media'/job_id;file=folder/'acceptance-source-packet.json'
        if file.is_symlink() or not file.resolve().is_relative_to(folder.resolve()):raise HubError('EXTENDED_SOURCE_FILE_INVALID')
        record=json.loads(file.read_text('utf-8'))
        if record['payload']!=parent['payload']:raise HubError('ACCEPTANCE_SOURCE_CHANGED')
        screenshot=Path(record['screenshot_file']).resolve()
        try:discovery_id=screenshot.relative_to((self.settings.root/'media').resolve()).parts[0]
        except (ValueError,IndexError):raise HubError('EXTENDED_SOURCE_FILE_INVALID') from None
        discovery=self.store.get(discovery_id)
        if not discovery or discovery['job_type']!='x_discovery':raise HubError('EXTENDED_SOURCE_FILE_INVALID')
        checked=ExtendedWebSources(self.settings,self.store)._verify(discovery_id,record,file)
        failure=parent['result'].get('machine_quality_failure',{})
        node=next((n for n in checked['nodes'] if n['role']==failure.get('role')),None)
        if not node:raise HubError('RENDER_ACCEPTANCE_RESUME_FORBIDDEN')
        child=self.store.get(failure['model_job_id'])
        if (not child or child['source_id']!=job_id+':'+node['role'] or child['payload']!=node['source']):
            raise HubError('RENDER_ACCEPTANCE_RESUME_FORBIDDEN')
        build_revision_context(self.store,child['job_id'],parent_job_id=job_id,role=node['role'],source=node['source'])
        proof=ModelReceipts(self.store).assess(child,failure['translation'])
        if not proof['live_transport_verified']:raise HubError('RENDER_ACCEPTANCE_RESUME_FORBIDDEN')
        marker={'version':'known-rejection-resume-v1','previous_result_sha256':digest(parent['result']),
            'previous_error_code':parent['error_code'],'original_rejection':failure,
            'source_packet_sha256':sha256_file(file),'discovery_job_id':discovery_id,'resumed_at':time.time(),
            'new_source_capture':False,'publication_authorized':False}
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
            if row['state']!='failed' or json.loads(row['result'])!=parent['result']:
                raise HubError('RENDER_ACCEPTANCE_RESUME_FORBIDDEN')
            db.execute("UPDATE jobs SET state='upload_pending',error_code=NULL,result=?,stage='known_rejection_resume',updated_at=? WHERE job_id=?",
                (encode({**parent['result'],'acceptance_resume':marker}),time.time(),job_id))
        self.store.audit('operator-acceptance','resume_known_rejection',job_id,'single_revision_same_source_no_publish')
        return self._execute(self.store.get(job_id),discovery_id,record,file,checked,model)

    def _execute(self,parent,discovery_id,record,file,checked,model):
        ident=parent['job_id'];folder=self.settings.root/'media'/ident
        validator=ExtendedWebSources(self.settings,self.store)
        service=MachineTweetsV2(self.settings,self.store);entries={}
        try:
            for node in checked['nodes']:
                entries[node['role']]=service.resolve_node(parent,node,model,require_fresh=False)
            # Re-verify source files after all paid calls and before rendering.
            after=validator._verify(discovery_id,record,file)
            if after!=checked:raise HubError('ACCEPTANCE_SOURCE_CHANGED')
            rendered=service.render_evidence(parent,{'nodes':checked['nodes']},entries)
            result={**parent['result'],'version':'real-machine-render-acceptance-v1','source_packet_file':str(file),'source_packet_sha256':sha256_file(file),
                'discovery_job_id':discovery_id,'source_created_at_unchanged':record['payload']['post']['created_at'],
                'source_proof':{'node_proof':checked['node_proof'],'reply_proof':checked['reply_proof']},
                'render':rendered['render'],'models':rendered['models'],'human_review':False,
                'publication_authorized':False,'publish_requests':0,'production_source_tombstones_modified':False,
                'note':'Age window is not a rendering restriction; this helper never creates a publish manifest, reservation or dispatch job.'}
            result_file=folder/'render-only-acceptance-result.json';service._write(result_file,result)
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET state='completed',stage='render_only_acceptance_complete',result=?,updated_at=? WHERE job_id=?",
                    (encode(result),time.time(),ident))
            self.store.audit('operator-acceptance','real_source_model_render',ident,'render_only_no_publication_permission')
            return {'job_id':ident,'result_file':str(result_file),'png':rendered['render']['png'],
                'png_sha256':rendered['render']['sha256'],'nodes':len(checked['nodes']),'publication_authorized':False}
        except BaseException as exc:
            with self.store.transaction() as db:
                db.execute("UPDATE jobs SET state='failed',error_code=?,updated_at=? WHERE job_id=?",
                    (exc.code if isinstance(exc,HubError) else 'RENDER_ACCEPTANCE_FAILED',time.time(),ident))
            raise
