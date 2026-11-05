"""Bound native replay transport and ordinary Worker handler.

The child has four fixed native operations, an isolated browser environment,
and no account/API credentials in its environment. SQLite remains in Python.
"""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import uuid

from .errors import HubError, PublishUnknown
from .locks import resource_guard
from .outcomes import JobOutcome
from .youtube_download import digest

VERSION = 'replay-native-browser-manifest-v1'
PRINCIPAL = 'worker:replay-video-v1'
SHA = re.compile(r'[a-f0-9]{64}')


def submit_rpc_budget(reservation, *, now=None):
    """Wait for the existing ticket plus a result window, never extend it."""
    current = time.time() if now is None else now
    expires = reservation.get('expires_at') if isinstance(reservation, dict) else None
    if (type(expires) not in (int, float) or not math.isfinite(expires) or
            not 0 < expires-current <= 900):
        raise HubError('REPLAY_BROWSER_RESERVATION_REQUIRED', review=True)
    return min(600.0, expires-current+60.0)


class ReplayBrowserSession:
    def __init__(self, settings, manifest, *, assert_lease, process_factory=subprocess.Popen):
        self.settings, self.manifest, self.assert_lease = settings, manifest, assert_lease
        self.messages = queue.Queue()
        self.closed = False
        script = settings.root / 'adapters/browser_replay_video.mjs'
        if not script.is_file() or script.is_symlink() or script.resolve() != script:
            raise HubError('REPLAY_BROWSER_SOURCE_REQUIRED', review=True)
        env = {name: os.environ[name] for name in ('PATH', 'LANG', 'LC_ALL', 'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME') if name in os.environ}
        env.update(HUB_ROOT=str(settings.root), HUB_BROWSER_CHANNEL='chromium',
                   BILIBILI_EXPECTED_SITE_UID=manifest['target_site_uid'],
                   PLAYWRIGHT_BROWSERS_PATH=str(settings.root/'compositor/runtime/browsers'))
        self.process = process_factory(['node', str(script)], cwd=settings.root, env=env,
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self._send(manifest)
            ready = self._receive(75)
            if ready.get('state') != 'ready' or ready.get('job_id') != manifest['job_id'] or ready.get('attempt_id') != manifest['attempt_id']:
                raise HubError(self._safe_code(ready.get('error_code')), review=True)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _safe_code(code):
        return code if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}', code) else 'REPLAY_BROWSER_TRANSPORT_UNAVAILABLE'

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(1_048_577)
                if not line:
                    break
                if len(line.encode('utf-8')) > 1_048_576 or not line.endswith('\n'):
                    self.messages.put(None)
                    return
                try:
                    self.messages.put(json.loads(line))
                except (ValueError, TypeError):
                    self.messages.put(None)
                    return
        finally:
            self.messages.put(None)

    def _send(self, value):
        self.assert_lease()
        try:
            self.process.stdin.write(json.dumps(value, ensure_ascii=False, separators=(',', ':'))+'\n')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            raise HubError('REPLAY_BROWSER_TRANSPORT_UNAVAILABLE', review=True) from None

    def _receive(self, timeout):
        deadline = time.monotonic()+timeout
        while True:
            self.assert_lease()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise HubError('REPLAY_BROWSER_RESPONSE_UNKNOWN', review=True)
            try:
                value = self.messages.get(timeout=min(2, remaining))
            except queue.Empty:
                continue
            if not isinstance(value, dict) or value.get('version') != 'replay-native-browser-response-v1':
                raise HubError('REPLAY_BROWSER_RESPONSE_UNKNOWN', review=True)
            return value

    def operation(self, operation, **arguments):
        with resource_guard(self.settings.root/'data/locks/heavy'):
            return self._operation(operation, **arguments)

    def _operation(self, operation, **arguments):
        if operation not in ('upload', 'prepare', 'submit', 'lookup') or self.closed:
            raise HubError('REPLAY_BROWSER_OPERATION_INVALID', review=True)
        began = time.monotonic()
        timeout = (submit_rpc_budget(arguments.get('reservation')) if operation == 'submit' else
                   {'upload': 12*3600+120, 'prepare': 240, 'lookup': 120}[operation])
        ident = str(uuid.uuid4())
        self._send({'version': 'replay-native-browser-request-v1', 'request_id': ident,
                    'operation': operation, 'arguments': arguments})
        if operation == 'submit':
            timeout -= time.monotonic()-began
            if timeout <= 0:
                raise HubError('REPLAY_BROWSER_RESPONSE_UNKNOWN', review=True)
        value = self._receive(timeout)
        if value.get('request_id') != ident or value.get('operation') != operation or value.get('ok') is not True:
            raise HubError(self._safe_code(value.get('error_code')), review=True)
        result = value.get('result')
        if not isinstance(result, dict):
            raise HubError('REPLAY_BROWSER_RESPONSE_UNKNOWN', review=True)
        return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.process.stdin.close()
            self.process.wait(timeout=15)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)


class ReplayVideoBrowser:
    def __init__(self, settings, *, session_factory=ReplayBrowserSession):
        self.settings, self.session_factory = settings, session_factory

    @contextmanager
    def session(self, manifest, *, assert_lease):
        # This is the same authenticated native profile used by the proven
        # short flow, never a parallel profile copy or Cookie export.
        locks = self.settings.root / 'data/locks'
        with resource_guard(locks/'bilibili-video-profile'):
            with resource_guard(locks/'heavy'):
                session = self.session_factory(self.settings, manifest, assert_lease=assert_lease)
            try:
                yield session
            finally:
                session.close()


def replay_manifest(settings, job, binding, state, attempt_id, *, duration_seconds, require_cover=True):
    provenance = settings.root/'media'/job['job_id']/'cover-assets-v4/provenance.json'
    try:
        cover=None;derivation=None
        if require_cover:
            if provenance.resolve() != provenance or provenance.is_symlink() or provenance.stat().st_size > 32768:
                raise ValueError()
            body = json.loads(provenance.read_bytes())
            output = body['output']
            cover = {key: output[key] for key in ('file', 'bytes', 'sha256')}
            cover['source_url'] = body['input']['source_url']
            if body['version'] != 'lighthouse-derived-cover-v4' or body['policy_version'] != 'top-bottom-blur-v1':
                raise ValueError()
            derivation={'version': 'derived-cover-binding-v2', 'provenance_file': str(provenance),
                    'provenance_sha256': hashlib.sha256(provenance.read_bytes()).hexdigest(), 'provenance': body}
        return {'version': VERSION, 'job_id': job['job_id'], 'download_job_id': binding['download_job_id'],
                'attempt_id': attempt_id, 'target_site_uid': binding['site_uid'],
                'source_id': binding['source_id'], 'source_url': binding['source_url'], 'channel_id': binding['channel_id'],
                'artifact_sha256': binding['artifact_sha256'], 'file_bytes': binding['file_bytes'], 'file': binding['file'],
                'duration_seconds': duration_seconds, 'metadata': binding['metadata'],
                'metadata_sha256': binding['metadata_sha256'], 'binding_sha256': digest(binding), 'policy_sha256': binding['policy_sha256'],
                'cover': cover, 'cover_derivation': derivation,
                'human_review': False, 'per_artifact_owner_acceptance': False}
    except (OSError, KeyError, ValueError, TypeError):
        raise HubError('REPLAY_BROWSER_DERIVED_COVER_REQUIRED', review=True) from None


class ReplayVideoWorkerHandler:
    """One durable replay job, with no CLI/operator command loop in its child.

    Admission and workflow authorization are separate coordinator operations.
    This handler never manufactures either user authorization or human review.
    """
    def __init__(self, settings, store, *, service=None, browser=None, cover_provider=None, manifest_builder=replay_manifest):
        from .replay_video_web import ReplayVideoWeb
        self.settings, self.store = settings, store
        self.service = service or ReplayVideoWeb(settings, store)
        self.browser = browser or ReplayVideoBrowser(settings)
        self.manifest_builder = manifest_builder
        if cover_provider is None:
            from .replay_video_cover import ensure_replay_cover
            cover_provider = lambda job, binding, attempt: ensure_replay_cover(settings, job, binding, attempt)
        self.cover_provider = cover_provider

    def _outcome(self, job_id):
        job = self.store.get(job_id)
        state = job['result'][self.service.STATE]['state']
        if state not in ('completed', 'awaiting_moderation', 'failed'):
            state = 'upload_pending'
        return JobOutcome(state, job['result'], job.get('external_publish_id'), job.get('error_code'))

    def process(self, claimed_job):
        service = self.service
        with service.worker_context(claimed_job):
            service.assert_worker_lease()
            job, binding, state = service.snapshot(claimed_job['job_id'],write=False)
            lookup_only=state['state'] in ('uploading','upload_unknown','submitting','submit_unknown','awaiting_moderation','completed','failed')
            if not lookup_only:
                job,binding,state=service.snapshot(claimed_job['job_id'])
            attempt = state.get('attempt_id') or str(uuid.uuid4())
            if state['state']=='prepared':
                service.bind_browser_attempt(PRINCIPAL,job['job_id'],attempt_id=attempt)
            if not lookup_only and self.cover_provider is not None:
                self.cover_provider(job, binding, attempt)
            source = self.store.get(binding['download_job_id'])
            duration = source['result'][service.DOWNLOAD_STATE]['binding']['duration']
            manifest = self.manifest_builder(self.settings, job, binding, state, attempt, duration_seconds=duration,require_cover=not lookup_only)
            # Preflight is local source/file/media/policy verification, not an
            # assertion that a native form or remote file has been verified.
            if not lookup_only:
                service.verify_for_publish(job['job_id'], cover_provenance_file=manifest['cover_derivation']['provenance_file'])
            with self.browser.session(manifest, assert_lease=service.assert_worker_lease) as session:
                if state['state'] in ('uploading', 'upload_unknown'):
                    session.operation('lookup')
                    raise HubError('REPLAY_PRIVATE_UPLOAD_RECONCILE_REQUIRED', review=True)
                if state['state'] in ('submitting', 'submit_unknown', 'awaiting_moderation', 'completed', 'failed'):
                    previous = state.get('result_observation')
                    result = session.operation('lookup', **({'external_id': previous['external_id']} if previous else {}))
                    if result.get('result_observation') and previous:
                        service.record_result(PRINCIPAL, job['job_id'], result['result_observation'], lookup=True)
                        return self._outcome(job['job_id'])
                    # A newly found candidate BV after a lost send must not be
                    # labelled as a same-attempt observation by this process.
                    raise PublishUnknown()
                if state['state'] == 'prepared':
                    service.assert_worker_lease()
                    service.start_upload(PRINCIPAL, job['job_id'], attempt_id=attempt, site_uid=binding['site_uid'])
                    try:
                        uploaded = session.operation('upload')
                        service.assert_worker_lease()
                        service.record_upload(PRINCIPAL, job['job_id'], uploaded['upload_observation'])
                    except (HubError,KeyError,ValueError,TypeError):
                        service.mark_unknown(PRINCIPAL, job['job_id'])
                        raise HubError('REPLAY_BROWSER_UPLOAD_RESPONSE_UNKNOWN',review=True) from None
                elif state['state'] != 'uploaded':
                    raise HubError('REPLAY_BROWSER_CHECKPOINT_REQUIRED', review=True)
                service.assert_worker_lease()
                prepared = session.operation('prepare')
                service.record_native_prepare(PRINCIPAL, job['job_id'], prepared['native_preparation'])
                policy=service.policy()
                if policy['source_scope']!='pilot_single' and policy['automation_enabled'] is not True:
                    return self._outcome(job['job_id'])
                _, _, latest = service.snapshot(job['job_id'], write=False)
                service.assert_worker_lease()
                reservation = service.reserve_submit(PRINCIPAL, job['job_id'], attempt_id=attempt,
                    site_uid=binding['site_uid'], upload_reference_sha256=latest['upload_observation']['upload_reference_sha256'])
                if reservation.get('can_click_submit') is not True:
                    session.operation('lookup')
                    raise PublishUnknown()
                ticket = {'version': getattr(service,'NATIVE_RESERVATION_VERSION','replay-native-reservation-v1'), 'job_id': job['job_id'], 'attempt_id': attempt,
                          'binding_sha256': digest(binding), 'metadata_sha256': binding['metadata_sha256'],
                          'can_click_submit': True, 'state': reservation['state'], 'idempotency_key': reservation['idempotency_key'],
                          'machine_evidence_sha256': reservation['machine_evidence_sha256'], 'expires_at': time.time()+300}
                try:
                    result = session.operation('submit', reservation=ticket)
                    observation = result.get('result_observation')
                    if not observation:
                        raise PublishUnknown()
                    service.record_result(PRINCIPAL, job['job_id'], observation)
                    if observation['state'] == 'awaiting_moderation':
                        # A follow-up Worker invocation performs the public
                        # lookup. An uploaded BV is not public-complete yet.
                        return self._outcome(job['job_id'])
                    return self._outcome(job['job_id'])
                except HubError:
                    service.mark_unknown(PRINCIPAL, job['job_id'])
                    raise PublishUnknown() from None


def replay_worker_handlers(settings, store):
    """Explicit runtime registration; no schedule or policy is enabled here."""
    from .replay_video_web import JOB_TYPE
    handler = ReplayVideoWorkerHandler(settings, store)
    return {JOB_TYPE: handler.process}
