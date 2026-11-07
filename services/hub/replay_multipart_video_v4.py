"""Whole-replay multipart coordinator, separate from the old single-file routes.

Publication authority comes from the real workflow policy/launch receipt.
Per-job evidence is recomputed from files, qualified packet checkpoints and
native observations. A caller-supplied passing boolean is never an authority.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import time
import tomllib
import uuid

from adapters.media_tools import empty_evidence, inspect_media
from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .locks import resource_guard
from .media_processing import atomic_json
from .replay_fifo_v4 import VERSION as FIFO_POLICY, DOWNLOAD_JOB, PUBLISH_JOB, CUTOFF
from .replay_machine_checks import ReplayMachineChecks, read_checked
from .replay_multipart_v4 import validate_whole_replay_plan, PROCESSING_VERSION
from .replay_natural_video_v3 import natural_metadata
from .replay_video_web import ReplayVideoWeb, WORKER_PRINCIPAL, _timestamp
from .short_video_web import _reference
from .youtube_download import CHANNELS, digest, duration_seconds
from .youtube_download_v4 import VERSION as DOWNLOAD_VERSION, STATE as DOWNLOAD_STATE

VERSION = 'replay-multipart-video-website-v4'
POLICY = 'replay-multipart-video-policy-v4'
KIND = 'video_website_replay_multipart_v4'
JOB_TYPE = PUBLISH_JOB
STATE = 'replay_multipart_video_v4'
ADMISSION = 'system:replay-multipart-admission-v4'
NATIVE_SELECTION_POLICY = 'first-then-native-add-part-v1'
BV = re.compile('BV[A-Za-z0-9]{10}')


def fail(code):
    raise HubError(code, review=True)


def read_document(root, file, *, maximum=1_000_000, check=lambda: None):
    reference, raw = read_checked(root, file, maximum=maximum, check=check)
    try:
        return json.loads(raw), reference
    except (ValueError, TypeError):
        fail('REPLAY_MULTIPART_DOCUMENT_INVALID')


def wire_seconds(value):
    """JSON canonical number equality across Python and native JavaScript.

    Preserve the measured value and the original processing manifest. Only the
    new wire binding spells an integral float as an integer (3.0 -> 3).
    """
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        fail('REPLAY_MULTIPART_DURATION_INVALID')
    return int(value) if float(value).is_integer() else value


class MultipartMachineChecks:
    def __init__(self, service):
        self.service = service

    def verify(self, job, binding, state, policy, *, cover_provenance_file=None, check=lambda: None):
        service = self.service
        root = service.settings.root
        with resource_guard(root / 'data/locks/heavy', root / 'data/locks' / ('multipart-check-' + job['job_id'])):
            check()
            service._validate_snapshot_binding(job, binding, verify_artifact_hash=True)
            source, asset, receipt_file = service._verified_source(binding['download_job_id'], verify_artifact_hash=False)
            binary = shutil.which('ffmpeg')
            if not binary:
                fail('REPLAY_MACHINE_FFMPEG_MISSING')
            cover = ReplayMachineChecks(service.settings, service.store)._cover(root, job['job_id'], binding['source_id'],
                cover_provenance_file, binary, check, attempt_id=state.get('attempt_id'))
            body = {'version': service.MACHINE_PROOF_VERSION, 'job_id': job['job_id'], 'binding_sha256': digest(binding),
                'policy_sha256': policy['_sha256'], 'created_at': datetime.now(timezone.utc).isoformat(),
                'external_requests': 0, 'human_review': False, 'per_artifact_owner_acceptance': False,
                'publication_authorized': False, 'checks': {'cover': cover,
                    'source': {'file': asset['file'], 'bytes': asset['bytes'], 'sha256': asset['sha256'],
                        'download_receipt_sha256': sha256_file(receipt_file), 'whole_plan_sha256': binding['whole_plan_sha256']},
                    'parts': deepcopy(asset['_parts']), 'processing_manifest': deepcopy(asset['_processing_manifest']),
                    'packet_coverage': 'qualified_checkpoint_chain_bound_to_current_full_file_hashes',
                    'source_and_parts_current_full_sha_verified': True}}
            folder = root / 'media' / job['job_id'] / service.MACHINE_PROOF_DIRECTORY / str(uuid.uuid4())
            folder.mkdir(parents=True, mode=0o700)
            file = folder / 'receipt.json'
            atomic_json(file, body)
            file.chmod(0o600)
            observed, reference = read_document(root, file, check=check)
            if observed != body:
                fail('REPLAY_MULTIPART_MACHINE_RECEIPT_CHANGED')
            check()
            return {**body, 'evidence_file': reference['file'], 'evidence_sha256': reference['sha256'], 'evidence_bytes': reference['bytes']}


class MultipartReplayVideoWeb(ReplayVideoWeb):
    VERSION, POLICY, KIND, JOB_TYPE, STATE = VERSION, POLICY, KIND, JOB_TYPE, STATE
    DOWNLOAD_STATE, DOWNLOAD_VERSION, DOWNLOAD_JOB_TYPE = DOWNLOAD_STATE, DOWNLOAD_VERSION, DOWNLOAD_JOB
    DOWNLOAD_DIRECTORY = 'youtube-whole-v4'
    MAX_BYTES, MAX_SECONDS = 32 * 14_000_000_000, 32 * 36000
    POLICY_FILE = 'replay-multipart-video-policy-v4.toml'
    PUBLIC_MODE = 'workflow_authorization_and_multipart_machine_acceptance_v4'
    WORKFLOW_LAUNCH_RECEIPT = Path('/etc/lighthouse-hub/replay-multipart-workflow-launch-v4.json')
    WORKFLOW_LAUNCH_VERSION = 'replay-multipart-workflow-launch-acceptance-v4'
    WORKFLOW_AUTHORIZATION_VERSION = 'replay-multipart-workflow-authorization-v4'
    MACHINE_PROOF_VERSION = 'verified-replay-multipart-machine-checks-v4'
    MACHINE_PROOF_DIRECTORY = 'replay-multipart-machine-checks-v4'
    NATIVE_DIRECTORY = 'replay-browser-multipart-v4'
    NATIVE_PREPARATION_VERSION = 'replay-multipart-native-preparation-v4'
    NATIVE_FORM_VERSION = 'replay-multipart-native-form-observation-v4'
    RESULT_OBSERVATION_VERSION = 'replay-multipart-browser-result-v4'
    NATIVE_RESERVATION_VERSION = 'replay-multipart-native-reservation-v4'
    NATIVE_CREATION_RECEIPT = Path('/etc/lighthouse-hub/native-multipart-creation-capability-v4.json')

    def __init__(self, settings, store, *, machine_acceptance_verifier=None):
        super().__init__(settings, store, machine_acceptance_verifier=machine_acceptance_verifier or MultipartMachineChecks(self))
        self._admitting = False

    def _operator(self, principal):
        if principal == ADMISSION and self._admitting:
            return
        return super()._operator(principal)

    def policy(self):
        try:
            file = self.settings.root / 'config' / self.POLICY_FILE
            if file.resolve() != file or any(p.is_symlink() for p in (file, *file.parents)) or os.name == 'posix' and file.stat().st_mode & 0o022:
                raise ValueError()
            raw = file.read_bytes()
            if not 0 < len(raw) <= 16384:
                raise ValueError()
            value = tomllib.loads(raw.decode('utf-8-sig'))
            scope, ids = value['source_scope'], value['acceptance_video_ids']
            if (value['version'] != self.POLICY or value['enabled'] is not True or
                    value['target_site_uid'] != self.settings.config['target_bilibili_site_uid'] or
                    value['target_account_name'] != self.settings.config['target_bilibili_account_name'] or
                    value['public_submission_mode'] != self.PUBLIC_MODE or value['source_not_before'] != CUTOFF or
                    set(value['allowed_channel_ids']) != CHANNELS or len(value['allowed_channel_ids']) != 2 or
                    type(value['automation_enabled']) is not bool or not isinstance(ids, list) or
                    scope not in ('pilot_single', 'all_eligible_ended_since_operating_cutoff') or
                    scope == 'pilot_single' and (len(ids) != 1 or not re.fullmatch('[A-Za-z0-9_-]{11}', ids[0])) or
                    scope != 'pilot_single' and ids or _timestamp(value['activated_at']) > time.time() or
                    any(not _reference(value.get(k)) for k in ('owner_authorization_reference', 'owner_permission_reference', 'workflow_authorization_reference'))):
                raise ValueError()
            return {**value, '_sha256': hashlib.sha256(raw).hexdigest(), '_activated_epoch': _timestamp(value['activated_at'])}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            fail('REPLAY_MULTIPART_POLICY_REQUIRED')

    def _source_scope(self, source, policy):
        try:
            raw = source['payload']['raw']
            if (source['source_id'] != raw['id'] or source['payload']['source_api'] != 'youtube-data-v3-official' or
                    raw['snippet']['channelId'] != source['payload']['channel_id'] or source['payload']['channel_id'] not in CHANNELS or
                    raw['status']['privacyStatus'] != 'public' or
                    not _timestamp(CUTOFF) <= _timestamp(raw['liveStreamingDetails']['actualStartTime']) <= _timestamp(raw['liveStreamingDetails']['actualEndTime']) <= time.time() + 30 or
                    scope_is_pilot(policy) and policy['acceptance_video_ids'] != [source['source_id']]):
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            fail('REPLAY_MULTIPART_SOURCE_SCOPE_REQUIRED')

    def require_native_creation_capability(self):
        """A real saved-and-restored new draft, not an AddP button or boolean.

        The failed initial-batch probe cannot satisfy this contract. Only the
        separately qualified first-P1/native-AddP route may eventually produce
        this root-committed technical receipt after current-account UI checks.
        """
        try:
            raw = self._root_readonly_file(self.NATIVE_CREATION_RECEIPT, maximum=65536)
            cap = json.loads(raw)
            if (set(cap) != {'version','target_site_uid','native_selection_policy','probe_job_id',
                    'draft_evidence_sha256','restored_evidence_sha256','recorded_at'} or
                    cap['version'] != 'bilibili-native-multipart-creation-capability-v4' or
                    cap['target_site_uid'] != self.settings.config['target_bilibili_site_uid'] or
                    cap['native_selection_policy'] != NATIVE_SELECTION_POLICY or _timestamp(cap['recorded_at']) > time.time()):
                raise ValueError()
            probe = self.store.get(cap['probe_job_id'])
            state = probe['result']['multipart_private_v4b']
            if (probe['job_type'] != 'replay_multipart_private_probe_v4b' or probe['policy_version'] != 'replay-multipart-private-probe-v4b' or
                    probe['source_platform'] != 'maintenance' or probe['state'] != 'completed' or state['state'] != 'private_draft_saved' or
                    probe['payload']['target_site_uid'] != cap['target_site_uid'] or
                    probe['payload']['native_selection_policy'] != NATIVE_SELECTION_POLICY or
                    probe['payload']['publication_authorized'] is not False or
                    self.store.query('SELECT 1 FROM publications WHERE job_id=?',(probe['job_id'],))):
                raise ValueError()
            draft = state['draft_receipt']
            restored = [item for item in state.get('inspections',[]) if item.get('recovered_without_modification') is True]
            if not restored:
                raise ValueError()
            restored = restored[-1]
            for item, expected_sha in ((draft,cap['draft_evidence_sha256']),(restored,cap['restored_evidence_sha256'])):
                ref = item['evidence']
                if ref['sha256'] != expected_sha:
                    raise ValueError()
                body, actual = read_document(self.settings.root,ref['file'])
                if actual != ref or body != {k:v for k,v in item.items() if k!='evidence'}:
                    raise ValueError()
                surface = body['submission_surface']
                if (body['native_selection_policy'] != NATIVE_SELECTION_POLICY or body['single_submission_count'] != 1 or
                        body['native_part_count'] != 2 or len(surface['outer_submissions']) != 1 or len(surface['inner_parts']) != 2 or
                        body['metadata_verified'] is not True or body['publication_authorized'] is not False or body['human_review'] is not False):
                    raise ValueError()
            if draft['private_draft_saved'] is not True:
                raise ValueError()
            return cap
        except HubError:
            fail('REPLAY_MULTIPART_NATIVE_CREATION_CAPABILITY_REQUIRED')
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            fail('REPLAY_MULTIPART_NATIVE_CREATION_CAPABILITY_REQUIRED')

    def _verified_source(self, download_job_id, *, verify_artifact_hash):
        """Bind byte-exact parts to the real producer's cumulative packet proof."""
        check = self.assert_worker_lease
        try:
            source = self.store.get(download_job_id)
            state = source['result'][self.DOWNLOAD_STATE]
            download, asset = state['binding'], deepcopy(state['source_asset'])
            plan = validate_whole_replay_plan(source['payload']['whole_replay_plan'], download['planning_policy'], official_raw=source['payload']['raw'])
            if (source['source_platform'] != 'youtube' or source['job_type'] != DOWNLOAD_JOB or source['policy_version'] != FIFO_POLICY or source['state'] != 'completed' or
                    state['version'] != self.DOWNLOAD_VERSION or state['state'] != 'parts_verified' or
                    download['job_id'] != source['job_id'] or download['video_id'] != source['source_id'] or
                    download['source_payload_sha256'] != digest(source['payload']) or download['channel_id'] not in CHANNELS or
                    source['payload']['video_id'] != source['source_id'] or source['payload']['url'] != 'https://www.youtube.com/watch?v=' + source['source_id'] or
                    download['channel_id'] != source['payload']['channel_id'] or download['whole_replay_plan'] != plan or
                    digest(plan) != source['payload']['whole_replay_plan_sha256'] or source['result'].get('download_performed') is not True or
                    any(source['result'].get(k) is not False for k in ('human_review', 'publication_authorized', 'publish_performed')) or
                    type(asset['bytes']) is not int or type(asset['duration']) not in (int, float) or
                    not math.isfinite(asset['duration']) or not 0 < asset['bytes'] <= self.MAX_BYTES or not 0 < asset['duration'] <= self.MAX_SECONDS or
                    abs(asset['duration'] - plan['duration']) > 2):
                raise ValueError()
            file = self._file(asset['file'], source['job_id'])
            if file != self.settings.root / 'media' / source['job_id'] / self.DOWNLOAD_DIRECTORY / 'original.mp4':
                raise ValueError()
            verified, verified_ref = read_document(self.settings.root, file.parent / 'verified.json', maximum=262144, check=check)
            if (verified['version'] != self.DOWNLOAD_VERSION or verified['binding_sha256'] != digest(download) or
                    verified['source_asset'] != asset or verified['receipt'] != state['receipt'] or
                    verified['receipt'].get('source_to_merged_packet_coverage_verified') is not True or
                    any(verified.get(k) is not False for k in ('human_review', 'publication_authorized', 'creator_original_bytes_verified'))):
                raise ValueError()
            if verify_artifact_hash:
                current, _ = read_checked(self.settings.root, file, maximum=self.MAX_BYTES, check=check, collect=False)
                if current['bytes'] != asset['bytes'] or current['sha256'] != asset['sha256']:
                    raise ValueError()
            elif file.stat().st_size != asset['bytes']:
                raise ValueError()
            prepared = state['parts']
            if Path(prepared['manifest']) != file.parent / 'parts-v4/manifest.json':
                raise ValueError()
            manifest, manifest_ref = read_document(self.settings.root, prepared['manifest'], check=check)
            checkpoint = source['result']['replay_multipart_processing_v4']
            originals = prepared['parts']
            if (manifest['version'] != PROCESSING_VERSION or manifest['source_sha256'] != asset['sha256'] or
                    manifest['whole_plan_sha256'] != digest(plan) or manifest['parts_prepared'] != originals or
                    manifest['parts'] != checkpoint['parts'] or checkpoint['state'] != 'completed' or
                    checkpoint['recipe_sha256'] != digest(manifest['recipe']) or checkpoint['source_sha256'] != asset['sha256'] or
                    manifest['source_packets'] != asset['packet_evidence'] or checkpoint['source_packets'] != asset['packet_evidence'] or
                    not 1 <= len(originals) <= plan['split_policy']['max_parts'] or len(originals) != len(manifest['parts'])):
                raise ValueError()
            actual_rows = self.store.query('SELECT * FROM replay_multipart_parts_v4 WHERE job_id=? ORDER BY part_index', (source['job_id'],))
            if len(actual_rows) != len(originals):
                raise ValueError()
            normalized, combined = [], empty_evidence()
            for index, (part, packet, row) in enumerate(zip(originals, manifest['parts'], actual_rows)):
                target = self._file(part['file'], source['job_id'])
                if (part['index'] != index or row['part_index'] != index or json.loads(row['record']) != part or
                        row['idempotency_key'] != part['part_idempotency_key'] or row['whole_plan_sha256'] != digest(plan) or
                        row['part_sha256'] != part['sha256'] or part['state'] != 'prepared' or row['state'] != 'prepared' or
                        row['external_publish_id'] is not None or row['external_part_id'] is not None or
                        target != file.parent / 'parts-v4' / f'part-{index:03d}.mp4' or
                        not 0 < part['bytes'] <= plan['split_policy']['max_part_bytes'] or
                        not 0 < part['duration'] <= plan['split_policy']['max_part_seconds'] or
                        packet['index'] != index or packet['sha256'] != part['sha256'] or packet['bytes'] != part['bytes'] or
                        packet['duration'] != part['duration'] or packet['packet_before'] != combined):
                    raise ValueError()
                combined = packet['packet_after']
                if verify_artifact_hash:
                    current, _ = read_checked(self.settings.root, target, maximum=plan['split_policy']['max_part_bytes'], check=check, collect=False)
                    if current['sha256'] != part['sha256'] or current['bytes'] != part['bytes']:
                        raise ValueError()
                    details = inspect_media(target, include_frame_rates=True, check=check)
                    if (abs(details['duration'] - part['duration']) > .05 or details['stream_config'] != asset['stream_config']):
                        raise ValueError()
                elif target.stat().st_size != part['bytes']:
                    raise ValueError()
                normalized.append({'index': index + 1, 'part_index': index, 'file': str(target), 'filename': target.name,
                    'bytes': part['bytes'], 'sha256': part['sha256'], 'duration_seconds': wire_seconds(part['duration']),
                    'part_idempotency_key': part['part_idempotency_key']})
            if combined != asset['packet_evidence']:
                raise ValueError()
            asset.update(_parts=normalized, _parts_manifest_sha256=digest(normalized), _whole_plan_sha256=digest(plan), _processing_manifest=manifest_ref)
            return source, asset, Path(verified_ref['file'])
        except HubError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
            fail('REPLAY_MULTIPART_VERIFIED_SOURCE_REQUIRED')

    def _binding(self, source, asset, receipt, metadata, policy):
        meta = self._metadata(metadata, source['payload']['url'])
        if meta.get('category_label') != '娱乐':
            fail('REPLAY_MULTIPART_METADATA_INVALID')
        return {'version': self.VERSION, 'download_job_id': source['job_id'], 'source_id': source['source_id'],
            'source_url': source['payload']['url'], 'channel_id': source['payload']['channel_id'], 'artifact_sha256': asset['sha256'],
            'file_bytes': asset['bytes'], 'file': asset['file'], 'source_duration_seconds': wire_seconds(asset['duration']),
            'download_receipt_sha256': sha256_file(receipt), 'metadata': meta, 'metadata_sha256': digest(meta),
            'site_uid': policy['target_site_uid'], 'account_name': policy['target_account_name'], 'policy_sha256': policy['_sha256'],
            'native_selection_policy': NATIVE_SELECTION_POLICY,
            'whole_plan_sha256': asset['_whole_plan_sha256'], 'parts_manifest_sha256': asset['_parts_manifest_sha256'],
            'parts': deepcopy(asset['_parts']), 'processing_manifest': deepcopy(asset['_processing_manifest']), 'human_review': False}

    def _validate_snapshot_binding(self, job, binding, *, verify_artifact_hash):
        policy = self.policy()
        source, asset, receipt = self._verified_source(binding['download_job_id'], verify_artifact_hash=verify_artifact_hash)
        self._source_scope(source, policy)
        if (job['source_id'] != source['source_id'] or binding != self._binding(source, asset, receipt, binding['metadata'], policy) or
                binding['metadata'] != natural_metadata(self.settings, source) or
                self.store.query("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=?", (source['source_id'],))):
            fail('REPLAY_MULTIPART_BINDING_CHANGED')
        return policy

    def _create(self, principal, download_job_id, metadata, *, authorization=None):
        self._operator(principal)
        policy = self.policy()
        source, asset, receipt = self._source(download_job_id)
        self._source_scope(source, policy)
        if metadata != natural_metadata(self.settings, source):
            fail('REPLAY_MULTIPART_METADATA_SOURCE_REQUIRED')
        binding = self._binding(source, asset, receipt, metadata, policy)
        key = digest(['bilibili', 'youtube', source['source_id'], 'site_uid:' + binding['site_uid'], self.KIND])
        now, ident = time.time(), str(uuid.uuid4())
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type=? AND policy_version=?",
                (source['source_id'], self.JOB_TYPE, self.POLICY)).fetchone()
            if existing:
                if json.loads(existing['payload']) != binding:
                    fail('REPLAY_MULTIPART_EXISTING_BINDING_CHANGED')
                return existing['job_id']
            if (db.execute("SELECT 1 FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=?", (source['source_id'],)).fetchone() or
                    db.execute("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=?", (source['source_id'],)).fetchone() or
                    db.execute("SELECT 1 FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type IN (?,?,?,?,?)",
                        (source['source_id'], 'short_video_publish', 'replay_video_publish', 'replay_highest_video_publish_v2', 'replay_natural_video_publish_v3', self.JOB_TYPE)).fetchone()):
                fail('REPLAY_MULTIPART_SOURCE_REQUIRES_LOOKUP')
            state = {'version': self.VERSION, 'state': 'prepared', 'key': key, 'human_review': False, 'public_submission_approved': False}
            if authorization is not None:
                if any(not _reference(authorization.get(k)) for k in ('actual_owner_workflow_response', 'owner_response_reference')):
                    fail('REPLAY_MULTIPART_WORKFLOW_AUTHORIZATION_REQUIRED')
                state['automation_authorization'] = {'version': self.WORKFLOW_AUTHORIZATION_VERSION,
                    'binding_sha256': digest(binding), 'workflow_authorization_reference': policy['workflow_authorization_reference'],
                    'actual_owner_workflow_response': authorization['actual_owner_workflow_response'],
                    'owner_response_reference': authorization['owner_response_reference'],
                    'recorded_at': datetime.now(timezone.utc).isoformat(), 'human_review': False,
                    'per_artifact_owner_acceptance': False, 'machine_acceptance_verified': False}
            result = {self.STATE: state, 'download_performed': False, 'publish_performed': False, 'human_review': False}
            db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,
                policy_version,payload,result,stage) VALUES(?,'youtube',?,?,'video',?,?,?,?,?,?,'multipart_prepared')""",
                (ident, source['source_id'], self.JOB_TYPE, 'pending' if authorization else 'awaiting_review',
                 now, now, self.POLICY, encode(binding), encode(result)))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (now, principal, 'replay_multipart_prepare', ident, encode({'workflow_authorized': authorization is not None,
                    'human_review': False, 'public_submission_approved': False, 'part_count': len(binding['parts'])})))
        return ident

    def create_candidate(self, principal, download_job_id, metadata):
        return self._create(principal, download_job_id, metadata)

    def queue_verified_download(self, download_job_id):
        policy = self.policy()
        self.require_native_creation_capability()
        grant = self._require_workflow_launch(policy)
        with resource_guard(self.settings.root / 'data/locks/replay-multipart-admission-v4', self.settings.root / 'data/locks/heavy'):
            source = self.store.get(download_job_id)
            authorization = {'actual_owner_workflow_response': policy.get('actual_owner_workflow_response') if grant is None else grant['actual_owner_launch_response'],
                'owner_response_reference': policy.get('owner_response_reference') if grant is None else grant['owner_response_reference']}
            self._admitting = True
            try:
                return self._create(ADMISSION, download_job_id, natural_metadata(self.settings, source), authorization=authorization)
            finally:
                self._admitting = False

    def _native_base(self, job, binding, state):
        return {'job_id': job['job_id'], 'attempt_id': state['attempt_id'], 'site_uid': binding['site_uid'],
            'source_id': binding['source_id'], 'source_url': binding['source_url'], 'artifact_sha256': binding['artifact_sha256'],
            'file_bytes': binding['file_bytes'], 'metadata_sha256': binding['metadata_sha256'], 'binding_sha256': digest(binding),
            'policy_sha256': binding['policy_sha256'], 'whole_plan_sha256': binding['whole_plan_sha256'],
            'parts_manifest_sha256': binding['parts_manifest_sha256'], 'native_selection_policy': binding['native_selection_policy']}

    def _native_order(self, binding, ordered, *, metadata=False, streams=True):
        try:
            if not isinstance(ordered, list) or len(ordered) != len(binding['parts']):
                raise ValueError()
            for index, (native, part) in enumerate(zip(ordered, binding['parts'])):
                if streams and any(native.get(k) != v or type(native.get(k)) is not type(v) for k, v in part.items()):
                    raise ValueError()
                names = [part['filename'], Path(part['filename']).stem]
                if metadata and index == 0:
                    names.append(binding['metadata']['title'])
                if (native.get('index') != index + 1 or type(native.get('index')) is not int or
                        native.get('native_filename') not in names or native.get('native_upload_complete') is not True or
                        streams and (native.get('whole_file_streamed') is not True or native.get('streamed_bytes') != part['bytes'] or
                                     native.get('streamed_sha256') != part['sha256'])):
                    raise ValueError()
            return deepcopy(ordered)
        except (ValueError, KeyError, TypeError, AttributeError):
            fail('REPLAY_MULTIPART_NATIVE_PARTS_REQUIRED')

    def record_upload(self, principal, job_id, observation):
        self._operator(principal)
        job, binding, state = self.snapshot(job_id, write=False)
        expected = self._native_base(job, binding, state)
        folder = self.settings.root / 'media' / job_id / self.NATIVE_DIRECTORY / state['attempt_id']
        body, reference = read_document(self.settings.root, folder / 'upload-receipt.json', check=self.assert_worker_lease)
        try:
            if (state['state'] != 'uploading' or body != observation or body['version'] != 'replay-multipart-upload-v4' or
                    any(body.get(k) != v or type(body.get(k)) is not type(v) for k, v in expected.items()) or
                    body.get('source_duration_seconds') != binding['source_duration_seconds'] or
                    body.get('single_submission_count') != 1 or type(body.get('single_submission_count')) is not int or
                    body.get('native_part_count') != len(binding['parts']) or
                    body.get('server_file_hash_verified') is not False or body.get('human_review') is not False):
                raise ValueError()
            self._native_order(binding, body['ordered_parts'])
            intent, actual_ref = read_document(self.settings.root, self.settings.root / 'media' / job_id / 'replay-native-intents/video-selection.json', check=self.assert_worker_lease)
            if (body['video_selection_intent'] != actual_ref or intent['version'] != 'replay-multipart-one-use-intent-v4' or
                    intent['kind'] != 'video-selection' or intent['parts'] != binding['parts'] or
                    any(intent.get(k) != v for k, v in expected.items()) or
                    body.get('processing_manifest') != binding['processing_manifest'] or
                    body['upload_reference_sha256'] != digest({k: v for k, v in body.items() if k != 'upload_reference_sha256'})):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            fail('REPLAY_MULTIPART_UPLOAD_EVIDENCE_REQUIRED')
        return self._update(principal, job, {**state, 'state': 'uploaded', 'upload_observation': body,
            'upload_evidence': reference}, job_state='awaiting_review', stage='multipart_uploaded')

    def _native_preparation(self, job, binding, state, observation, proof):
        try:
            expected = {**self._native_base(job, binding, state), 'source_duration_seconds': binding['source_duration_seconds'],
                'ordered_parts': state['upload_observation']['ordered_parts'], 'native_part_count': len(binding['parts']),
                'single_submission_count': 1,
                'cover_sha256': proof['checks']['cover']['output']['sha256'],
                'cover_provenance_sha256': proof['checks']['cover']['provenance']['sha256'],
                'upload_reference_sha256': state['upload_observation']['upload_reference_sha256'],
                'title': binding['metadata']['title'], 'description': binding['metadata']['description'],
                'creation_statement': '内容为转载', 'source': binding['source_url'], 'category': '娱乐',
                'metadata_verified': True, 'cover_applied': True, 'server_file_hash_verified': False,
                'human_review': False, 'per_artifact_owner_acceptance': False}
            fields = set(expected) | {'version', 'tags', 'native_rows', 'observed_at', 'private_draft_saved', 'pre_submit_screenshot', 'native_observation_file'}
            if (not isinstance(observation, dict) or set(observation) != fields or observation['version'] != self.NATIVE_PREPARATION_VERSION or
                    any(observation[k] != v or type(observation[k]) is not type(v) for k, v in expected.items()) or
                    observation['private_draft_saved'] is not True or len(observation['tags']) != len(binding['metadata']['tags']) or
                    set(observation['tags']) != set(binding['metadata']['tags']) or not 0 <= time.time() - _timestamp(observation['observed_at']) <= 300):
                raise ValueError()
            self._native_order(binding, observation['native_rows'], metadata=True, streams=False)
            folder = self.settings.root / 'media' / job['job_id'] / self.NATIVE_DIRECTORY / state['attempt_id']
            bodies = {}
            for field, name, maximum in (('pre_submit_screenshot', 'pre-submit.png', 20_000_000),
                    ('native_observation_file', 'native-preparation-observation.json', 1_000_000)):
                ref = observation[field]
                if not isinstance(ref, dict) or set(ref) != {'file', 'bytes', 'sha256'} or ref['file'] != str(folder / name):
                    raise ValueError()
                actual, raw = read_checked(self.settings.root, ref['file'], maximum=maximum, check=self.assert_worker_lease)
                if actual != ref:
                    raise ValueError()
                bodies[field] = raw
            image = bodies['pre_submit_screenshot']
            if len(image) < 33 or image[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR':
                raise ValueError()
            inner = {k: deepcopy(v) for k, v in observation.items() if k not in ('private_draft_saved', 'pre_submit_screenshot', 'native_observation_file')}
            inner['version'] = self.NATIVE_FORM_VERSION
            if json.loads(bodies['native_observation_file']) != inner:
                raise ValueError()
            self.assert_worker_lease()
            return deepcopy(observation)
        except HubError:
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            fail('REPLAY_MULTIPART_NATIVE_PREPARATION_REQUIRED')

    def reserve_submit(self, principal, job_id, *, attempt_id, site_uid, upload_reference_sha256,
                       machine_acceptance_receipt_sha256=None, native_refresh=None):
        self._operator(principal)
        self.require_native_creation_capability()
        job, binding, state = self.snapshot(job_id, write=False)
        policy = self._validate_snapshot_binding(job, binding, verify_artifact_hash=False)
        self._require_workflow_launch(policy)
        auth = state.get('automation_authorization', {})
        if (state['state'] != 'uploaded' or state.get('public_submission_approved') is not False or 'owner_acceptance' in state or
                auth.get('version') != self.WORKFLOW_AUTHORIZATION_VERSION or auth.get('binding_sha256') != digest(binding) or
                auth.get('workflow_authorization_reference') != policy['workflow_authorization_reference'] or
                auth.get('human_review') is not False or auth.get('per_artifact_owner_acceptance') is not False or
                any(not _reference(auth.get(k)) for k in ('actual_owner_workflow_response', 'owner_response_reference')) or
                attempt_id != state.get('attempt_id') or site_uid != binding['site_uid'] or
                upload_reference_sha256 != state.get('upload_observation', {}).get('upload_reference_sha256')):
            fail('REPLAY_MULTIPART_WORKFLOW_AUTHORIZATION_REQUIRED')
        proof = self._machine_acceptance(job, binding, state, policy, machine_acceptance_receipt_sha256)
        if native_refresh is not None:
            if self._worker_job is None or not callable(native_refresh):
                fail('REPLAY_MULTIPART_NATIVE_REFRESH_WORKER_REQUIRED')
            self.assert_worker_lease()
            observed = native_refresh()  # trusted parent callback; no caller-supplied passing JSON
            self.assert_worker_lease()
            prepared = self._native_preparation(job, binding, state, observed, proof)
            self._update(principal, job, {**state, 'native_preparation': prepared}, job_state=job['state'], stage='multipart_native_refreshed')
            job, binding, state = self.snapshot(job_id, write=False)
        else:
            self._native_preparation(job, binding, state, state.get('native_preparation'), proof)
        reservation = self._reserve_submit_once(principal, job, binding, {**state, 'machine_acceptance': proof}, site_uid=site_uid)
        if reservation.get('can_click_submit') is not True:
            return reservation
        issued = time.time()
        ticket = {**reservation, **self._native_base(job, binding, state), 'version': self.NATIVE_RESERVATION_VERSION,
            'ticket_id': str(uuid.uuid4()), 'issued_at': issued, 'expires_at': issued + 120,
            'machine_evidence_sha256': proof['evidence_sha256'], 'upload_reference_sha256': upload_reference_sha256}
        folder = self.settings.root / 'media' / job_id / self.NATIVE_DIRECTORY / attempt_id
        file = folder / 'reservation.json'
        atomic_json(file, ticket)
        file.chmod(0o600)
        with self.store.transaction() as db:
            for part in binding['parts']:
                if db.execute("""UPDATE replay_multipart_parts_v4 SET state='dispatching' WHERE job_id=? AND part_index=?
                    AND idempotency_key=? AND state='prepared' AND external_publish_id IS NULL AND external_part_id IS NULL""",
                    (binding['download_job_id'], part['part_index'], part['part_idempotency_key'])).rowcount != 1:
                    fail('REPLAY_MULTIPART_PART_RESERVATION_CHANGED')
        return ticket


def scope_is_pilot(policy):
    return policy['source_scope'] == 'pilot_single'
