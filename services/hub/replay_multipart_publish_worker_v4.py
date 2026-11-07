"""Ordinary Worker glue for the separately gated multipart native route."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import time
import uuid

from .errors import HubError, PublishUnknown
from .locks import resource_guard
from .outcomes import JobOutcome
from .replay_multipart_private_v4 import PrivateSession, safe
from .replay_multipart_video_v4 import MultipartReplayVideoWeb, WORKER_PRINCIPAL
from .replay_video_browser import replay_manifest, submit_rpc_budget
from .replay_video_cover import ensure_replay_cover


class MultipartReplaySession(PrivateSession):
    def __init__(self, settings, manifest, *, assert_lease, process_factory=subprocess.Popen):
        script = safe(settings.root / 'adapters/browser_multipart_replay_v4.mjs')
        if not script.is_file():
            raise HubError('REPLAY_MULTIPART_NATIVE_SOURCE_REQUIRED', review=True)
        # Reuse the bounded reader, narrow child environment and owned-PID
        # cleanup; the native entry is a fixed business route, never a caller
        # supplied command/module/path.
        def launch(arguments, **kwargs):
            if arguments != ['node', str(settings.root / 'adapters/browser_multipart_private_v4.mjs')]:
                raise HubError('REPLAY_MULTIPART_NATIVE_SOURCE_CHANGED', review=True)
            return process_factory(['node', str(script)], **kwargs)
        super().__init__(settings, manifest, assert_lease=assert_lease, process_factory=launch)

    def _operation(self, operation, **arguments):
        if operation not in ('upload','prepare','submit','lookup') or self.closed:
            raise HubError('REPLAY_MULTIPART_NATIVE_OPERATION_INVALID', review=True)
        if (operation == 'submit' and set(arguments) != {'reservation'} or
                operation == 'lookup' and set(arguments) - {'external_id'} or
                operation in ('upload','prepare') and arguments):
            raise HubError('REPLAY_MULTIPART_NATIVE_OPERATION_INVALID', review=True)
        began = time.monotonic()
        timeout = (submit_rpc_budget(arguments['reservation']) if operation == 'submit' else
            {'upload': 12*3600+120, 'prepare': 300, 'lookup': min(3800,180+len(self.manifest['parts'])*110)}[operation])
        ident = str(uuid.uuid4())
        self._send({'version':'replay-native-browser-request-v1','request_id':ident,'operation':operation,'arguments':arguments})
        if operation == 'submit':
            timeout -= time.monotonic() - began
        if timeout <= 0:
            raise HubError('REPLAY_BROWSER_RESPONSE_UNKNOWN', review=True)
        value = self._receive(timeout)
        if value.get('request_id') != ident or value.get('operation') != operation or value.get('ok') is not True or not isinstance(value.get('result'),dict):
            raise HubError(self._safe_code(value.get('error_code')), review=True)
        return value['result']


class MultipartReplayBrowser:
    def __init__(self, settings, *, session_factory=MultipartReplaySession):
        self.settings, self.session_factory = settings, session_factory

    @contextmanager
    def session(self, manifest, *, assert_lease, before_launch=None):
        locks = self.settings.root / 'data/locks'
        with resource_guard(locks/'bilibili-video-profile'):
            with resource_guard(locks/'heavy'):
                assert_lease()
                if before_launch is not None:
                    before_launch()  # real attempt reservation only after both locks
                session = self.session_factory(self.settings,manifest,assert_lease=assert_lease)
            try:
                yield session
            finally:
                session.close()


def multipart_replay_manifest(settings,store,job,binding,state,attempt_id,*,require_cover=True):
    # The old helper only packages the deterministic cover binding. It neither
    # validates single-file source state nor performs browser/network actions.
    base = replay_manifest(settings,job,binding,state,attempt_id,
        duration_seconds=binding['source_duration_seconds'],require_cover=require_cover)
    return {k:v for k,v in {**base,'version':'replay-multipart-native-browser-manifest-v4',
        'account_name':binding['account_name'],'source_duration_seconds':binding['source_duration_seconds'],
        'whole_plan_sha256':binding['whole_plan_sha256'],'parts_manifest_sha256':binding['parts_manifest_sha256'],
        'parts':binding['parts'],'processing_manifest':binding['processing_manifest'],
        'native_selection_policy':binding['native_selection_policy']}.items() if k not in ('file','duration_seconds')}


class MultipartReplayWorkerHandler:
    def __init__(self,settings,store,*,service=None,browser=None,cover_provider=None):
        self.settings,self.store = settings,store
        self.service = service or MultipartReplayVideoWeb(settings,store)
        self.browser = browser or MultipartReplayBrowser(settings)
        self.cover_provider = cover_provider or (lambda job,binding,attempt:ensure_replay_cover(settings,job,binding,attempt))

    def _outcome(self,job_id):
        job=self.store.get(job_id)
        state=job['result'][self.service.STATE]['state']
        target=state if state in ('completed','awaiting_moderation','failed') else 'upload_pending'
        return JobOutcome(target,job['result'],job.get('external_publish_id'),job.get('error_code'))

    def process(self,claimed):
        service=self.service
        with service.worker_context(claimed):
            job,binding,state=service.snapshot(claimed['job_id'],write=False)
            lookup_only=state['state'] in ('submitting','submit_unknown','awaiting_moderation','completed','failed')
            if state['state'] in ('uploading','upload_unknown'):
                # No implicit resumption of a failed batch/partial native
                # upload. A separate actually verified same-draft route must
                # establish that capability first.
                raise HubError('REPLAY_MULTIPART_UPLOAD_RECONCILIATION_REQUIRED',review=True)
            attempt=state.get('attempt_id') or str(uuid.uuid4())
            if not lookup_only:
                service.require_native_creation_capability()
                service._require_workflow_launch(service.policy())
                if state['state']=='prepared':
                    service.bind_browser_attempt(WORKER_PRINCIPAL,job['job_id'],attempt_id=attempt)
                self.cover_provider(job,binding,attempt)
                service.verify_for_publish(job['job_id'])
                job,binding,state=service.snapshot(job['job_id'],write=False)
            manifest=multipart_replay_manifest(self.settings,self.store,job,binding,state,attempt,require_cover=not lookup_only)
            with self.browser.session(manifest,assert_lease=service.assert_worker_lease) as session:
                if lookup_only:
                    previous=state.get('result_observation')
                    result=session.operation('lookup',**({'external_id':previous['external_id']} if previous else {}))
                else:
                    if state['state']=='prepared':
                        service.start_upload(WORKER_PRINCIPAL,job['job_id'],attempt_id=attempt,site_uid=binding['site_uid'])
                        try:
                            result=session.operation('upload')
                            service.record_upload(WORKER_PRINCIPAL,job['job_id'],result['upload_receipt'])
                        except Exception:
                            service.mark_unknown(WORKER_PRINCIPAL,job['job_id'])
                            raise
                    state=service.snapshot(job['job_id'],write=False)[2]
                    ticket=service.reserve_submit(WORKER_PRINCIPAL,job['job_id'],attempt_id=attempt,
                        site_uid=binding['site_uid'],upload_reference_sha256=state['upload_observation']['upload_reference_sha256'],
                        native_refresh=lambda:session.operation('prepare')['native_preparation'])
                    if ticket.get('can_click_submit') is not True:
                        raise PublishUnknown()
                    try:
                        result=session.operation('submit',reservation=ticket)
                    except Exception:
                        service.mark_unknown(WORKER_PRINCIPAL,job['job_id'])
                        raise PublishUnknown() from None
                observed=result.get('result_observation')
                if not observed or observed.get('state')=='unknown':
                    current=service.snapshot(job['job_id'],write=False)[2]
                    if current['state'] in ('submitting','submit_unknown'):
                        service.mark_unknown(WORKER_PRINCIPAL,job['job_id'])
                    raise PublishUnknown()
                service.record_result(WORKER_PRINCIPAL,job['job_id'],observed,lookup=True,result_reference=result.get('result_reference'))
                return self._outcome(job['job_id'])
