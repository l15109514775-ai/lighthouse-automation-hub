"""Download-bound replay coordinator; browser/scheduler launch is not connected.

Reuse the short acceptance state machine without widening its limits or its
one-artifact approval. Replay publication has an independent workflow grant
and a trusted local file/media/cover checker. Neither a policy boolean nor a
JSON claim of passing tests enables all-new-replay automatic publication.
"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import tomllib
import uuid

from .errors import HubError
from .short_video_web import ShortVideoWeb, _reference
from .youtube_download import CHANNELS, digest, duration_seconds

VERSION = 'replay-video-website-v1'
POLICY = 'replay-video-website-policy-v1'
KIND = 'video_website_replay_v1'
JOB_TYPE = 'replay_video_publish'
STATE = 'replay_video_web_v1'
WORKER_PRINCIPAL = 'worker:replay-video-v1'


def _timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or not math.isfinite(parsed.timestamp()):
        raise ValueError()
    return parsed.timestamp()


class ReplayVideoWeb(ShortVideoWeb):
    VERSION, POLICY, KIND, JOB_TYPE, STATE = VERSION, POLICY, KIND, JOB_TYPE, STATE
    MAX_BYTES = 3_800_000_000
    MAX_SECONDS = 86400
    POLICY_FILE = 'replay-video-web-policy.toml'
    PUBLIC_MODE = 'workflow_authorization_and_machine_acceptance_v1'
    WORKFLOW_LAUNCH_RECEIPT = Path('/etc/lighthouse-hub/replay-workflow-launch-v1.json')
    WORKFLOW_LAUNCH_VERSION = 'replay-workflow-launch-acceptance-v1'
    WORKFLOW_AUTHORIZATION_VERSION = 'replay-workflow-authorization-v1'
    MACHINE_PROOF_VERSION = 'verified-replay-machine-checks-v1'
    MACHINE_PROOF_DIRECTORY = 'replay-machine-checks-v1'
    NATIVE_PREPARATION_VERSION = 'replay-native-preparation-v1'
    NATIVE_FORM_VERSION = 'replay-native-form-observation-v1'
    NATIVE_DIRECTORY = 'replay-browser'

    def __init__(self, settings, store, *, machine_acceptance_verifier=None):
        super().__init__(settings, store)
        if machine_acceptance_verifier is None:
            from .replay_machine_checks import ReplayMachineChecks
            machine_acceptance_verifier = ReplayMachineChecks(settings, store)
        self.machine_acceptance_verifier = machine_acceptance_verifier
        self._worker_job = None

    @contextmanager
    def worker_context(self, job):
        """Bind this service invocation to the Worker-owned, renewable lease."""
        if self._worker_job is not None:
            raise HubError('REPLAY_WORKER_CONTEXT_ALREADY_BOUND', review=True)
        self._worker_job = job
        self._worker_identity = (job.get('job_id'), job.get('lease_owner'), job.get('attempts'), digest(job.get('payload')))
        try:
            self.assert_worker_lease()
            yield self
        finally:
            self._worker_job = None
            self._worker_identity = None

    def assert_worker_lease(self):
        if self._worker_job is None:
            return
        ident, owner, attempts, payload = self._worker_identity
        current = self.store.get(ident)
        if (not owner or not current or current['job_type'] != self.JOB_TYPE or
                current['state'] != 'processing' or current['lease_owner'] != owner or
                current['attempts'] != attempts or digest(current['payload']) != payload or
                (current.get('lease_until') or 0) <= time.time()):
            raise HubError('REPLAY_WORKER_LEASE_LOST', review=True)

    def _operator(self, principal):
        if principal == WORKER_PRINCIPAL:
            if self._worker_job is None:
                raise HubError('REPLAY_WORKER_CONTEXT_REQUIRED', review=True)
            self.assert_worker_lease()
            return
        super()._operator(principal)

    def _checkpoint_job_state(self, job, requested):
        if job['state'] == 'processing':
            if self._worker_job is None:
                raise HubError('REPLAY_WORKER_CONTEXT_REQUIRED', review=True)
            self.assert_worker_lease()
            return 'processing'
        return requested

    def _checkpoint_matches(self, current, job):
        if job['state'] != 'processing':
            return super()._checkpoint_matches(current, job)
        if current is None or self._worker_job is None:
            return False
        current = dict(current)
        for key in ('payload', 'result'):
            current[key] = json.loads(current[key])
        ident, owner, attempts, payload = self._worker_identity
        return (current['job_id'] == ident and current['state'] == 'processing' and current['lease_owner'] == owner and
                current['attempts'] == attempts and digest(current['payload']) == payload and
                (current['lease_until'] or 0) > time.time() and
                all(current[key] == value for key, value in job.items() if key not in ('updated_at', 'lease_until')))

    def _checkpoint_committed(self, job_id):
        # Worker.finish's exception path defaults to its original job.result.
        # Keep that same object current so it cannot overwrite a durable
        # upload_unknown/submitting checkpoint with a stale prepared result.
        if self._worker_job is not None and self._worker_job['job_id'] == job_id:
            current = self.store.get(job_id)
            self.assert_worker_lease()
            self._worker_job.update(current)

    def policy(self):
        path = self.settings.root / 'config' / self.POLICY_FILE
        try:
            if (path.is_symlink() or not path.is_file() or path.stat().st_size > 16384 or
                    os.name != 'nt' and path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise ValueError()
            raw = path.read_bytes(); value = tomllib.loads(raw.decode('utf-8-sig'))
            scope, ids = value.get('source_scope'), value.get('acceptance_video_ids')
            channels = value.get('allowed_channel_ids')
            if (value.get('version') != self.POLICY or value.get('enabled') is not True or
                    value.get('target_site_uid') != self.settings.config['target_bilibili_site_uid'] or
                    value.get('target_account_name') != self.settings.config['target_bilibili_account_name'] or
                    value.get('public_submission_mode') != self.PUBLIC_MODE or
                    any(not _reference(value.get(key)) for key in ('owner_authorization_reference',
                        'owner_permission_reference', 'workflow_authorization_reference')) or
                    not isinstance(channels, list) or len(channels) != 2 or set(channels) != CHANNELS or
                    type(value.get('automation_enabled', False)) is not bool or
                    scope not in ('pilot_single', 'automatic_new_only') or not isinstance(ids, list) or
                    scope == 'pilot_single' and (len(ids) != 1 or not isinstance(ids[0], str) or
                        not re.fullmatch(r'[A-Za-z0-9_-]{11}', ids[0])) or
                    scope == 'automatic_new_only' and ids):
                raise ValueError()
            activated = _timestamp(value['activated_at'])
            if activated > time.time():
                raise ValueError()
            return {**value, 'automation_enabled': value.get('automation_enabled', False),
                    '_sha256': hashlib.sha256(raw).hexdigest(), '_activated_epoch': activated}
        except (OSError, KeyError, ValueError, TypeError, AttributeError, OverflowError):
            raise HubError('REPLAY_VIDEO_POLICY_REQUIRED', review=True) from None

    def _source_scope(self, source, policy):
        try:
            payload = source['payload']; raw = payload['raw']
            live, snippet = raw['liveStreamingDetails'], raw['snippet']
            started, ended = _timestamp(live['actualStartTime']), _timestamp(live['actualEndTime'])
            if (source['source_id'] != raw['id'] or payload['source_api'] != 'youtube-data-v3-official' or
                    snippet['channelId'] != payload['channel_id'] or payload['channel_id'] not in CHANNELS or
                    raw['status']['privacyStatus'] != 'public' or not 0 <= started <= ended <= time.time() + 30 or
                    raw['contentDetails']['duration'] != payload['duration'] or
                    abs(duration_seconds(payload['duration']) - source['result'][self.DOWNLOAD_STATE]['binding']['duration']) > 2 or
                    policy['source_scope'] == 'pilot_single' and source['source_id'] not in policy['acceptance_video_ids'] or
                    policy['source_scope'] == 'automatic_new_only' and ended < policy['_activated_epoch']):
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            raise HubError('REPLAY_VIDEO_SOURCE_SCOPE_REQUIRED', review=True) from None

    @staticmethod
    def _metadata(metadata, url):
        value = ShortVideoWeb._metadata(metadata, url)
        if '转载许可编号' in value['description']:
            raise HubError('REPLAY_VIDEO_PUBLIC_PERMISSION_LINE_FORBIDDEN', review=True)
        return value

    def create_candidate(self, principal, download_job_id, metadata):
        """Prepare only. This does not queue Worker, launch a browser or approve."""
        return self.create_acceptance(principal, download_job_id, metadata)

    def approve(self, principal, job_id, **kwargs):
        self._operator(principal)
        raise HubError('REPLAY_VIDEO_USE_SEPARATE_WORKFLOW_AUTHORIZATION', review=True)

    def record_automation_authorization(self, principal, job_id, *, actual_owner_workflow_response,
                                        owner_response_reference, workflow_authorization_reference):
        self._operator(principal); job, binding, state = self.snapshot(job_id); policy = self.policy()
        if (binding['policy_sha256'] != policy['_sha256'] or state['state'] not in ('prepared', 'uploaded') or
                state.get('public_submission_approved') is not False or 'owner_acceptance' in state or
                any(not _reference(value) for value in (actual_owner_workflow_response,
                    owner_response_reference, workflow_authorization_reference)) or
                workflow_authorization_reference != policy['workflow_authorization_reference']):
            raise HubError('REPLAY_VIDEO_WORKFLOW_AUTHORIZATION_REQUIRED', review=True)
        authorization = {'version': self.WORKFLOW_AUTHORIZATION_VERSION, 'binding_sha256': digest(binding),
            'workflow_authorization_reference': workflow_authorization_reference,
            'actual_owner_workflow_response': actual_owner_workflow_response,
            'owner_response_reference': owner_response_reference,
            'recorded_at': datetime.now(timezone.utc).isoformat(), 'human_review': False,
            'per_artifact_owner_acceptance': False, 'machine_acceptance_verified': False}
        return self._update(principal, job, {**state, 'automation_authorization': authorization},
                            job_state=job['state'], stage=job['stage'])

    def _root_readonly_file(self, file, *, maximum):
        """Only the global workflow-launch grant needs a root write boundary."""
        if os.name != 'posix':
            raise HubError('REPLAY_MACHINE_ROOT_EVIDENCE_REQUIRED', review=True)
        try:
            if (not file.is_absolute() or file.resolve() != file or not file.is_file() or
                    file.stat().st_size > maximum or file.stat().st_size == 0):
                raise ValueError()
            for entry in (file, *file.parents):
                st = entry.lstat()
                if stat.S_ISLNK(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
                    raise ValueError()
            return file.read_bytes()
        except (OSError, ValueError):
            raise HubError('REPLAY_MACHINE_ROOT_EVIDENCE_REQUIRED', review=True) from None

    def _require_workflow_launch(self, policy):
        if policy['source_scope'] == 'pilot_single':
            return None  # Explicit one-source pilot policy is independently scoped.
        if policy['automation_enabled'] is not True:
            raise HubError('REPLAY_WORKFLOW_LAUNCH_ACCEPTANCE_REQUIRED', review=True)
        try:
            raw = self._root_readonly_file(self.WORKFLOW_LAUNCH_RECEIPT, maximum=65536)
            grant = json.loads(raw)
            fields = {'version', 'policy_sha256', 'workflow_authorization_reference', 'target_site_uid',
                'allowed_channel_ids', 'acceptance_job_id', 'pilot_machine_evidence_sha256',
                'pilot_public_result_sha256', 'actual_owner_launch_response', 'owner_response_reference', 'accepted_at'}
            if (set(grant) != fields or grant['version'] != self.WORKFLOW_LAUNCH_VERSION or
                    grant['policy_sha256'] != policy['_sha256'] or
                    grant['workflow_authorization_reference'] != policy['workflow_authorization_reference'] or
                    grant['target_site_uid'] != policy['target_site_uid'] or
                    set(grant['allowed_channel_ids']) != CHANNELS or len(grant['allowed_channel_ids']) != 2 or
                    any(not _reference(grant[key]) for key in ('actual_owner_launch_response', 'owner_response_reference')) or
                    _timestamp(grant['accepted_at']) > time.time()):
                raise ValueError()
            pilot = self.store.get(grant['acceptance_job_id'])
            previous = pilot['result'][self.STATE]; observation = previous['result_observation']
            if (pilot['job_type'] != self.JOB_TYPE or pilot['state'] != 'completed' or previous['state'] != 'completed' or
                    pilot['payload']['site_uid'] != policy['target_site_uid'] or observation.get('public_accessible') is not True or
                    previous['machine_acceptance']['evidence_sha256'] != grant['pilot_machine_evidence_sha256'] or
                    digest(observation) != grant['pilot_public_result_sha256']):
                raise ValueError()
            rows = self.store.query('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',
                                    (pilot['job_id'], previous['key']))
            if (len(rows) != 1 or rows[0]['state'] != 'completed' or rows[0]['external_id'] != observation['external_id'] or
                    rows[0]['account_id'] != 'site_uid:' + policy['target_site_uid']):
                raise ValueError()
            return grant
        except (HubError, OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise HubError('REPLAY_WORKFLOW_LAUNCH_ACCEPTANCE_REQUIRED', review=True) from None

    def queue_pilot(self, principal, download_job_id, metadata, *, actual_owner_workflow_response, owner_response_reference):
        self._operator(principal); policy = self.policy()
        if policy['source_scope'] != 'pilot_single':
            raise HubError('REPLAY_PILOT_SCOPE_REQUIRED', review=True)
        job_id = self.create_candidate(principal, download_job_id, metadata)
        job, _, state = self.snapshot(job_id)
        if job['state'] != 'awaiting_review' or state['state'] != 'prepared':
            return job_id  # Stable dedup; never reset a live/unknown/complete attempt.
        self.record_automation_authorization(principal, job_id,
            actual_owner_workflow_response=actual_owner_workflow_response,
            owner_response_reference=owner_response_reference,
            workflow_authorization_reference=policy['workflow_authorization_reference'])
        job, _, state = self.snapshot(job_id)
        self._update(principal, job, state, job_state='pending', stage='replay_video_queued')
        return job_id

    def verify_for_publish(self, job_id, *, cover_provenance_file=None):
        self.assert_worker_lease()
        job, binding, state = self.snapshot(job_id); policy = self.policy()
        return self._machine_acceptance(job, binding, state, policy, None, cover_provenance_file=cover_provenance_file)

    def bind_browser_attempt(self, principal, job_id, *, attempt_id):
        """Persist an attempt identity before cover acquisition, not a selection."""
        self._operator(principal); job, _, state = self.snapshot(job_id)
        try:
            if str(uuid.UUID(attempt_id)) != attempt_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise HubError('SHORT_VIDEO_ATTEMPT_INVALID', review=True) from None
        if state['state'] != 'prepared' or state.get('attempt_id', attempt_id) != attempt_id:
            raise HubError('REPLAY_BROWSER_ATTEMPT_RECONCILE_REQUIRED', review=True)
        if state.get('attempt_id') == attempt_id:
            return {'job_id': job_id, 'state': job['state'], 'checkpoint': 'prepared', 'attempt_id': attempt_id,
                    'new_file_selection': False}
        return {**self._update(principal, job, {**state, 'attempt_id': attempt_id},
                              job_state=job['state'], stage='replay_browser_attempt_bound'),
                'attempt_id': attempt_id, 'new_file_selection': False}

    def _machine_acceptance(self, job, binding, state, policy, receipt_sha256=None, *, cover_provenance_file=None):
        try:
            verified = self.machine_acceptance_verifier.verify(job, binding, state, policy,
                cover_provenance_file=cover_provenance_file, check=self.assert_worker_lease)
            if (not isinstance(verified, dict) or verified.get('version') != self.MACHINE_PROOF_VERSION or
                    verified.get('job_id') != job['job_id'] or verified.get('binding_sha256') != digest(binding) or
                    verified.get('policy_sha256') != policy['_sha256'] or
                    any(verified.get(key) is not False for key in ('human_review', 'per_artifact_owner_acceptance', 'publication_authorized'))):
                raise ValueError()
            file = Path(verified['evidence_file']); parent = self.settings.root / 'media' / job['job_id'] / self.MACHINE_PROOF_DIRECTORY
            if (file.name != 'receipt.json' or file.parent.parent != parent or str(uuid.UUID(file.parent.name)) != file.parent.name or
                    file.resolve() != file or any(p.is_symlink() for p in (file, *file.parents)) or not file.is_file() or
                    type(verified['evidence_bytes']) is not int or not 0 < verified['evidence_bytes'] <= 1_000_000):
                raise ValueError()
            raw = file.read_bytes()
            if (len(raw) != verified['evidence_bytes'] or hashlib.sha256(raw).hexdigest() != verified['evidence_sha256'] or
                    receipt_sha256 is not None and receipt_sha256 != verified['evidence_sha256'] or
                    json.loads(raw) != {k: v for k, v in verified.items() if k not in ('evidence_file', 'evidence_sha256', 'evidence_bytes')}):
                raise ValueError()
            self.assert_worker_lease()
            return verified
        except HubError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise HubError('REPLAY_MACHINE_ACCEPTANCE_REQUIRED', review=True) from None

    def _native_preparation(self, job, binding, state, observation, proof):
        try:
            expected = {'job_id': job['job_id'], 'attempt_id': state['attempt_id'], 'site_uid': binding['site_uid'],
                'source_id': binding['source_id'], 'source_url': binding['source_url'],
                'artifact_sha256': binding['artifact_sha256'], 'file_bytes': binding['file_bytes'],
                'metadata_sha256': binding['metadata_sha256'], 'binding_sha256': digest(binding),
                'cover_sha256': proof['checks']['cover']['output']['sha256'],
                'cover_provenance_sha256': proof['checks']['cover']['provenance']['sha256'],
                'upload_reference_sha256': state['upload_observation']['upload_reference_sha256'],
                'title': binding['metadata']['title'], 'description': binding['metadata']['description'],
                'creation_statement': '内容为转载', 'source': binding['source_url'],
                'category': binding['metadata']['category_label'], 'metadata_verified': True, 'cover_applied': True,
                'server_file_hash_verified': False, 'human_review': False, 'per_artifact_owner_acceptance': False}
            fields = set(expected) | {'version', 'tags', 'observed_at', 'private_draft_saved',
                                      'pre_submit_screenshot', 'native_observation_file'}
            if (not isinstance(observation, dict) or set(observation) != fields or
                    observation['version'] != self.NATIVE_PREPARATION_VERSION or
                    any(observation[key] != value for key, value in expected.items()) or
                    any(observation[key] is not value for key, value in expected.items() if type(value) is bool) or
                    observation['private_draft_saved'] is not True or
                    not isinstance(observation['tags'], list) or len(observation['tags']) != len(binding['metadata']['tags']) or
                    set(observation['tags']) != set(binding['metadata']['tags']) or
                    not 0 <= time.time() - _timestamp(observation['observed_at']) <= 300):
                raise ValueError()
            folder = self.settings.root / 'media' / job['job_id'] / self.NATIVE_DIRECTORY / state['attempt_id']
            bodies = {}
            for key, name, maximum in (('pre_submit_screenshot', 'pre-submit.png', 20_000_000),
                                       ('native_observation_file', 'native-preparation-observation.json', 262144)):
                reference = observation[key]; file = folder / name
                if (not isinstance(reference, dict) or set(reference) != {'file', 'bytes', 'sha256'} or
                        reference['file'] != str(file) or type(reference['bytes']) is not int or
                        not 0 < reference['bytes'] <= maximum or file.resolve() != file or
                        any(p.is_symlink() for p in (file, *file.parents)) or not file.is_file() or
                        file.stat().st_size != reference['bytes']):
                    raise ValueError()
                body = file.read_bytes()
                if len(body) != reference['bytes'] or hashlib.sha256(body).hexdigest() != reference['sha256']:
                    raise ValueError()
                bodies[key] = body
            screenshot = bodies['pre_submit_screenshot']
            if (len(screenshot) < 33 or screenshot[:8] != b'\x89PNG\r\n\x1a\n' or screenshot[8:16] != b'\x00\x00\x00\rIHDR' or
                    not 0 < int.from_bytes(screenshot[16:20], 'big') <= 10000 or
                    not 0 < int.from_bytes(screenshot[20:24], 'big') <= 10000):
                raise ValueError()
            inner = {key: deepcopy(value) for key, value in observation.items()
                     if key not in ('pre_submit_screenshot', 'native_observation_file', 'private_draft_saved')}
            inner['version'] = self.NATIVE_FORM_VERSION
            if json.loads(bodies['native_observation_file']) != inner:
                raise ValueError()
            self.assert_worker_lease()
            return deepcopy(observation)
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            raise HubError('REPLAY_NATIVE_PREPARATION_REQUIRED', review=True) from None

    def record_native_prepare(self, principal, job_id, observation):
        self._operator(principal); job, binding, state = self.snapshot(job_id, write=False)
        policy = self._validate_snapshot_binding(job, binding, verify_artifact_hash=False)
        if state['state'] != 'uploaded' or state.get('public_submission_approved') is not False:
            raise HubError('REPLAY_NATIVE_PREPARATION_REQUIRED', review=True)
        proof = self._machine_acceptance(job, binding, state, policy)
        safe = self._native_preparation(job, binding, state, observation, proof)
        return self._update(principal, job, {**state, 'native_preparation': safe},
                            job_state=job['state'], stage='replay_native_prepared')

    def reserve_submit(self, principal, job_id, *, attempt_id, site_uid, upload_reference_sha256,
                       machine_acceptance_receipt_sha256=None):
        self._operator(principal); job, binding, state = self.snapshot(job_id, write=False)
        policy = self._validate_snapshot_binding(job, binding, verify_artifact_hash=False)
        self._require_workflow_launch(policy)
        authorization = state.get('automation_authorization', {})
        if (binding['policy_sha256'] != policy['_sha256'] or state['state'] != 'uploaded' or
                state.get('public_submission_approved') is not False or 'owner_acceptance' in state or
                authorization.get('version') != self.WORKFLOW_AUTHORIZATION_VERSION or
                authorization.get('binding_sha256') != digest(binding) or
                authorization.get('workflow_authorization_reference') != policy['workflow_authorization_reference'] or
                authorization.get('human_review') is not False or authorization.get('per_artifact_owner_acceptance') is not False or
                any(not _reference(authorization.get(key)) for key in ('actual_owner_workflow_response', 'owner_response_reference')) or
                state.get('attempt_id') != attempt_id or site_uid != binding['site_uid'] or
                upload_reference_sha256 != state.get('upload_observation', {}).get('upload_reference_sha256')):
            raise HubError('REPLAY_VIDEO_AUTOMATION_AUTHORIZATION_REQUIRED', review=True)
        proof = self._machine_acceptance(job, binding, state, policy, machine_acceptance_receipt_sha256)
        self._native_preparation(job, binding, state, state.get('native_preparation'), proof)
        # Keep the machine proof separate from the old owner-acceptance flag.
        reservation = self._reserve_submit_once(principal, job, binding,
            {**state, 'machine_acceptance': proof}, site_uid=site_uid)
        return {**reservation, 'machine_evidence_sha256': proof['evidence_sha256']} if reservation.get('can_click_submit') is True else reservation

    def cleanup_eligibility(self, job_id):
        """Pilot test material stays local until a separately accepted launch.

        No remote deletion API is provided, and this version has no operation
        that releases a pilot hold. Formal launch/release must be implemented
        and accepted separately; a BV or a passing-test flag cannot release it.
        """
        _, binding, _ = self.snapshot(job_id, write=False)
        try:
            policy = self.policy()
            if binding['policy_sha256'] != policy['_sha256']:
                raise ValueError()
            if policy['source_scope'] == 'pilot_single':
                return {'eligible': False, 'reason': 'PILOT_TESTS_RETAINED_UNTIL_FORMAL_WORKFLOW_LAUNCH',
                        'delete_performed': False}
        except (HubError, KeyError, ValueError):
            return {'eligible': False, 'reason': 'REPLAY_VIDEO_POLICY_BINDING_CHANGED', 'delete_performed': False}
        return super().cleanup_eligibility(job_id)
