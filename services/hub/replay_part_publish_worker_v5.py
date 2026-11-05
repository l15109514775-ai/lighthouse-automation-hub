"""One ordinary native submission per real, registered V5 output.

The browser has no SQL or API credentials. A known/uncertain publication only
enters lookup; it never reaches upload or a second public-send reservation.
"""
from contextlib import contextmanager
import subprocess
import queue
import time
import uuid

from .errors import HubError,PublishUnknown
from .locks import resource_guard
from .outcomes import JobOutcome
from .replay_multipart_private_v4 import PrivateSession,safe
from .replay_parts_v5 import ReplayPartVideoWeb,WORKER_PRINCIPAL,index_from_job
from .replay_video_browser import replay_manifest,submit_rpc_budget
from .replay_video_cover import ensure_replay_cover


class PartReplaySession(PrivateSession):
    def __init__(self,settings,manifest,*,assert_lease,process_factory=subprocess.Popen,owner_handoff=None):
        from .replay_part_owner_handoff_v5 import existing_nonces
        self._handoff_existing=existing_nonces(settings,manifest)
        self._owner_handoff_callback=owner_handoff;self._rpc_context=None;self._handoff_seen=set()
        script=safe(settings.root/'adapters/browser_replay_part_v5.mjs')
        if not script.is_file():raise HubError('REPLAY_PART_BROWSER_SOURCE_REQUIRED',review=True)
        def launch(arguments,**kwargs):
            if arguments!=['node',str(settings.root/'adapters/browser_multipart_private_v4.mjs')]:
                raise HubError('REPLAY_PART_BROWSER_SOURCE_CHANGED',review=True)
            return process_factory(['node',str(script)],**kwargs)
        super().__init__(settings,manifest,assert_lease=assert_lease,process_factory=launch)

    def _receive(self,timeout):
        from .replay_part_owner_handoff_v5 import EVENT,acknowledge
        deadline=time.monotonic()+timeout;retained=False
        while True:
            self.assert_lease();remaining=deadline-time.monotonic()
            if remaining<=0:raise HubError('REPLAY_BROWSER_RESPONSE_UNKNOWN',review=True)
            try:value=self.messages.get(timeout=min(2,remaining))
            except queue.Empty:continue
            if isinstance(value,dict) and value.get('version')==EVENT:
                if retained or self._rpc_context is None:raise HubError('REPLAY_PART_OWNER_HANDOFF_UNSOLICITED',review=True)
                request_id,operation=self._rpc_context
                seconds=acknowledge(self.settings,self.manifest,value,request_id=request_id,operation=operation,
                    callback=self._owner_handoff_callback,seen=self._handoff_seen,assert_lease=self.assert_lease)
                self.assert_lease();deadline=time.monotonic()+seconds;retained=True
                continue
            if not isinstance(value,dict) or value.get('version')!='replay-native-browser-response-v1':raise HubError('REPLAY_BROWSER_RESPONSE_UNKNOWN',review=True)
            return value

    def close(self):
        if self.closed:return
        try:super().close()
        finally:
            from .replay_part_owner_handoff_v5 import cleanup_credentials
            remaining=self._live_owned()
            removed=cleanup_credentials(self.settings,self.manifest,self._handoff_existing) if not remaining else 0
            folder=self.settings.root/'media'/self.manifest['job_id']/'replay-browser-part-v5'/self.manifest['attempt_id']
            if folder.is_dir() and not folder.is_symlink() and folder.resolve()==folder:
                from .media_processing import atomic_json
                atomic_json(folder/('owned-process-cleanup-'+str(uuid.uuid4())+'.json'),{'version':'replay-part-owned-process-cleanup-v5',
                    'job_id':self.manifest['job_id'],'attempt_id':self.manifest['attempt_id'],'captured_processes':len(self._owned),
                    'remaining_owned_live_processes':len(remaining),'temporary_credentials_deleted':removed,
                    'broad_profile_or_worker_kill':False,'recorded_at':time.time()})

    def _operation(self,operation,**arguments):
        if (self.closed or operation not in ('upload','prepare','submit','lookup') or
                operation=='submit' and set(arguments)!={'reservation'} or
                operation=='lookup' and set(arguments)-{'external_id'} or operation in ('upload','prepare') and arguments):
            raise HubError('REPLAY_PART_BROWSER_OPERATION_INVALID',review=True)
        began=time.monotonic()
        budget=submit_rpc_budget(arguments['reservation']) if operation=='submit' else {'upload':12*3600+120,'prepare':300,'lookup':300}[operation]
        ident=str(uuid.uuid4());self._send({'version':'replay-native-browser-request-v1','request_id':ident,'operation':operation,'arguments':arguments})
        if operation=='submit':budget-=time.monotonic()-began
        if budget<=0:raise HubError('REPLAY_BROWSER_RESPONSE_UNKNOWN',review=True)
        self._rpc_context=(ident,operation)
        try:value=self._receive(budget)
        finally:self._rpc_context=None
        if (value.get('request_id')!=ident or value.get('operation')!=operation or value.get('ok') is not True or not isinstance(value.get('result'),dict)):
            raise HubError(self._safe_code(value.get('error_code')),review=True)
        return value['result']


class PartReplayBrowser:
    def __init__(self,settings,*,session_factory=PartReplaySession):
        self.settings,self.session_factory=settings,session_factory

    @contextmanager
    def session(self,manifest,*,assert_lease,before_launch=None,owner_handoff=None):
        locks=self.settings.root/'data/locks'
        with resource_guard(locks/'bilibili-video-profile'):
            with resource_guard(locks/'heavy'):
                assert_lease()
                if before_launch is not None:before_launch()
                options={'owner_handoff':owner_handoff} if owner_handoff is not None else {}
                session=self.session_factory(self.settings,manifest,assert_lease=assert_lease,**options)
            try:yield session
            finally:session.close()


def part_replay_manifest(settings,store,job,binding,state,attempt_id,*,require_cover=True):
    base=replay_manifest(settings,job,binding,state,attempt_id,duration_seconds=binding['duration_seconds'],require_cover=require_cover)
    return {**base,'version':'replay-part-native-browser-manifest-v5','parent_job_id':binding['parent_job_id'],
        'account_name':binding['account_name'],'part_index':binding['part_index'],'part_count':binding['part_count'],
        'part_idempotency_key':binding['part_idempotency_key'],'whole_plan_sha256':binding['whole_plan_sha256'],
        'parts_manifest_sha256':binding['parts_manifest_sha256'],'source_duration_seconds':binding['source_duration_seconds'],
        'source_contract':binding['source_contract'],'processing_manifest':binding['source_contract']['delivery_manifest']}


class PartReplayWorkerHandler:
    def __init__(self,settings,store,*,part_index=0,service=None,browser=None,cover_provider=None):
        self.settings,self.store=settings,store
        self.service=service or ReplayPartVideoWeb(settings,store,part_index=part_index)
        self.browser=browser or PartReplayBrowser(settings)
        self.cover_provider=cover_provider or (lambda job,binding,attempt:ensure_replay_cover(settings,job,binding,attempt))

    def process(self,claimed):
        service=self.service
        if index_from_job(claimed)!=service.part_index:raise HubError('REPLAY_PART_JOB_ORDINAL_MISMATCH',review=True)
        with service.worker_context(claimed):
            job,binding,state=service.snapshot(claimed['job_id'],write=False)
            lookup_only=state['state'] in ('submitting','submit_unknown','awaiting_moderation','completed','failed')
            if state['state'] in ('uploading','upload_unknown'):
                raise HubError('REPLAY_PART_PRIVATE_UPLOAD_RECONCILIATION_REQUIRED',review=True)
            attempt=state.get('attempt_id') or str(uuid.uuid4())
            if not lookup_only:
                service._require_workflow_launch(service.policy())
                if state['state']=='prepared':service.bind_browser_attempt(WORKER_PRINCIPAL,job['job_id'],attempt_id=attempt)
                self.cover_provider(job,binding,attempt)
                service.verify_for_publish(job['job_id'])
                job,binding,state=service.snapshot(job['job_id'],write=False)
            manifest=part_replay_manifest(self.settings,self.store,job,binding,state,attempt,require_cover=not lookup_only)
            options={}
            if (self.settings.root/'config/bilibili-video-handoff-v5.json').exists():
                from .replay_part_owner_handoff_v5 import record_worker_handoff
                options['owner_handoff']=lambda body,ref:record_worker_handoff(service,job['job_id'],body,ref)
            with self.browser.session(manifest,assert_lease=service.assert_worker_lease,**options) as session:
                if lookup_only:
                    old=state.get('result_observation')
                    result=session.operation('lookup',**({'external_id':old['external_id']} if old else {}))
                else:
                    if state['state']=='prepared':
                        service.start_upload(WORKER_PRINCIPAL,job['job_id'],attempt_id=attempt,site_uid=binding['site_uid'])
                        try:
                            upload=session.operation('upload')
                            service.record_upload(WORKER_PRINCIPAL,job['job_id'],upload['upload_observation'])
                        except Exception:
                            service.mark_unknown(WORKER_PRINCIPAL,job['job_id']);raise
                    state=service.snapshot(job['job_id'],write=False)[2]
                    ticket=service.reserve_submit(WORKER_PRINCIPAL,job['job_id'],attempt_id=attempt,site_uid=binding['site_uid'],
                        upload_reference_sha256=state['upload_observation']['upload_reference_sha256'],
                        native_refresh=lambda:session.operation('prepare')['native_preparation'])
                    if ticket.get('can_click_submit') is not True:raise PublishUnknown()
                    try:result=session.operation('submit',reservation=ticket)
                    except Exception:
                        service.mark_unknown(WORKER_PRINCIPAL,job['job_id']);raise PublishUnknown() from None
                observed=result.get('result_observation')
                if not observed or observed.get('state')!='completed':
                    current=service.snapshot(job['job_id'],write=False)[2]
                    if current['state'] in ('submitting','submit_unknown'):service.mark_unknown(WORKER_PRINCIPAL,job['job_id'])
                    raise PublishUnknown()
                try:service.record_result(WORKER_PRINCIPAL,job['job_id'],observed,lookup=True,result_reference=result.get('result_reference'))
                except HubError as exc:
                    current=service.snapshot(job['job_id'],write=False)[2]
                    committed=current['state']=='completed' and current.get('result_observation')==observed
                    if current['state'] in ('submitting','submit_unknown'):service.mark_unknown(WORKER_PRINCIPAL,job['job_id'])
                    with self.store.transaction() as db:
                        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                            (time.time(),WORKER_PRINCIPAL,'replay_part_group_aggregation_pending' if committed else 'replay_part_result_unverified',job['job_id'],exc.code))
                    if committed:
                        # The child-result transaction already verified and
                        # committed; only subsequent group aggregation failed.
                        # Its separate local tick can recover without sending.
                        current_job=self.store.get(job['job_id'])
                        return JobOutcome('completed',current_job['result'],current_job['external_publish_id'],None)
                    raise PublishUnknown() from None
                job=self.store.get(job['job_id']);return JobOutcome(job['result'][service.STATE]['state'],job['result'],job.get('external_publish_id'),job.get('error_code'))
