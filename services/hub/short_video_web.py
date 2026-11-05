"""Operator-only, one-short-replay website acceptance bookkeeping.

No browser launcher, credentials, network, scheduler, API, MCP or human-review
attestation. Upload and public submit are separate durable boundaries. The old
signed video-review and official-publishing routes retain their own meanings.
"""
from copy import deepcopy
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

from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .youtube_download import CHANNELS, STATE as DOWNLOAD_STATE, VERSION as DOWNLOAD_VERSION, digest

VERSION = 'short-replay-website-acceptance-v1'
POLICY = 'short-replay-website-policy-v1'
KIND = 'video_website_short_v1'
JOB_TYPE = 'short_video_publish'
STATE = 'short_video_web_v1'
PRINCIPAL = 'operator:ssh-sudo'
MAX_BYTES = 100_000_000
MAX_SECONDS = 600
BV = re.compile(r'BV[A-Za-z0-9]{10}')


def _reference(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2048


class ShortVideoWeb:
    VERSION = VERSION
    POLICY = POLICY
    KIND = KIND
    JOB_TYPE = JOB_TYPE
    STATE = STATE
    MAX_BYTES = MAX_BYTES
    MAX_SECONDS = MAX_SECONDS
    POLICY_FILE = 'short-video-web-policy.toml'
    PUBLIC_MODE = 'per_artifact_owner_acceptance'
    DOWNLOAD_STATE = DOWNLOAD_STATE
    DOWNLOAD_VERSION = DOWNLOAD_VERSION
    DOWNLOAD_JOB_TYPE = 'youtube_download'
    DOWNLOAD_DIRECTORY = 'youtube-download-v1'
    RESULT_OBSERVATION_VERSION = 'short-video-browser-result-v1'

    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def _operator(self, principal):
        if principal != PRINCIPAL:
            self.store.audit(principal, 'short_video_write', 'short-replay', 'denied')
            raise HubError('ACCESS_DENIED')

    def policy(self):
        path = self.settings.root / 'config' / self.POLICY_FILE
        try:
            if (path.is_symlink() or not path.is_file() or path.stat().st_size > 16384 or
                    os.name != 'nt' and path.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
                raise ValueError()
            raw = path.read_bytes(); value = tomllib.loads(raw.decode('utf-8-sig'))
            if (value.get('version') != self.POLICY or value.get('enabled') is not True or
                    value.get('target_site_uid') != self.settings.config['target_bilibili_site_uid'] or
                    value.get('target_account_name') != self.settings.config['target_bilibili_account_name'] or
                    value.get('public_submission_mode') != self.PUBLIC_MODE or
                    not _reference(value.get('owner_authorization_reference')) or
                    not _reference(value.get('owner_permission_reference')) or
                    not isinstance(value.get('acceptance_video_ids'), list) or len(value['acceptance_video_ids']) != 1 or
                    not re.fullmatch(r'[A-Za-z0-9_-]{11}', value['acceptance_video_ids'][0])):
                raise ValueError()
            when = datetime.fromisoformat(value['activated_at'].replace('Z', '+00:00'))
            if when.tzinfo is None or when.timestamp() > time.time():
                raise ValueError()
            return {**value, '_sha256': hashlib.sha256(raw).hexdigest()}
        except (OSError, KeyError, ValueError, TypeError, AttributeError):
            raise HubError('SHORT_VIDEO_POLICY_REQUIRED', review=True) from None

    def _file(self, name, job_id):
        try:
            uuid.UUID(job_id)
            path = Path(name)
            base = self.settings.root / 'media' / job_id
            if (not path.is_absolute() or not path.resolve().is_relative_to(base.resolve()) or
                    any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file()):
                raise ValueError()
            return path
        except (OSError, ValueError, TypeError, AttributeError):
            raise HubError('SHORT_VIDEO_ARTIFACT_INVALID', review=True) from None

    def _source(self, download_job_id):
        return self._verified_source(download_job_id, verify_artifact_hash=True)

    def _verified_source(self, download_job_id, *, verify_artifact_hash):
        """Read the real source/receipt; optionally defer only the MP4 SHA.

        The default _source route always hashes the entire artifact. Native
        replay QA may defer that duplicate read to its immediately following
        trusted MachineChecker, which independently hashes the current file.
        No expected hash is substituted for an observed hash here.
        """
        job = self.store.get(download_job_id)
        try:
            if type(verify_artifact_hash) is not bool:
                raise ValueError()
            state = job['result'][self.DOWNLOAD_STATE]
            binding, asset = state['binding'], state['source_asset']
            if (job['source_platform'] != 'youtube' or job['job_type'] != self.DOWNLOAD_JOB_TYPE or job['state'] != 'completed' or
                    state['version'] != self.DOWNLOAD_VERSION or state['state'] != 'verified' or
                    binding['job_id'] != job['job_id'] or binding['video_id'] != job['source_id'] or
                    binding['channel_id'] not in CHANNELS or binding['channel_id'] != job['payload']['channel_id'] or
                    binding['source_payload_sha256'] != digest(job['payload']) or
                    job['payload']['video_id'] != job['source_id'] or
                    job['payload']['url'] != 'https://www.youtube.com/watch?v=' + job['source_id'] or
                    job['result']['download_performed'] is not True or job['result']['publish_performed'] is not False or
                    any(job['result'].get(k) is not False for k in ('human_review', 'publication_authorized')) or
                    any(state.get(k) is not False for k in ('human_review', 'publication_authorized')) or
                    type(asset['bytes']) is not int or not 0 < asset['bytes'] <= self.MAX_BYTES or
                    type(asset['duration']) not in (int, float) or not math.isfinite(asset['duration']) or
                    not 0 < asset['duration'] <= self.MAX_SECONDS or abs(asset['duration'] - binding['duration']) > 2):
                raise ValueError()
            file = self._file(asset['file'], job['job_id'])
            receipt_file = self._file(file.parent / 'verified.json', job['job_id'])
            if file != self.settings.root / 'media' / job['job_id'] / self.DOWNLOAD_DIRECTORY / 'original.mp4':
                raise ValueError()
            if receipt_file.stat().st_size > 262144:
                raise ValueError()
            receipt = json.loads(receipt_file.read_bytes())
            if (receipt['version'] != self.DOWNLOAD_VERSION or receipt['binding_sha256'] != digest(binding) or
                    receipt['source_asset'] != asset or receipt['receipt'] != state['receipt'] or
                    any(receipt.get(k) is not False for k in ('human_review', 'publication_authorized', 'creator_original_bytes_verified')) or
                    file.stat().st_size != asset['bytes'] or
                    (verify_artifact_hash and sha256_file(file) != asset['sha256']) or
                    not receipt['receipt'].get('source_to_merged_packet_coverage_verified') or
                    sorted(asset.get('stream_types', {}).values()) != ['audio', 'video']):
                raise ValueError()
            return job, asset, receipt_file
        except HubError:
            raise
        except (KeyError, TypeError, ValueError, OSError, AttributeError):
            raise HubError('SHORT_VIDEO_VERIFIED_DOWNLOAD_REQUIRED', review=True) from None

    @staticmethod
    def _metadata(metadata, url):
        try:
            if not isinstance(metadata, dict):
                raise ValueError()
            common = {'title', 'description', 'tags', 'copyright', 'source'}
            if set(metadata) == common | {'category_id'}:
                category_valid = type(metadata['category_id']) is int and 0 < metadata['category_id'] < 100000
            elif set(metadata) == common | {'category_label'}:
                label = metadata['category_label']
                category_valid = isinstance(label, str) and bool(label.strip()) and len(label) <= 40
            else:
                raise ValueError()
            if (not category_valid or
                    not isinstance(metadata['title'], str) or not 0 < len(metadata['title'].strip()) <= 80 or
                    not isinstance(metadata['description'], str) or not 0 < len(metadata['description']) <= 2000 or
                    url not in metadata['description'] or metadata['source'] != url or
                    type(metadata['copyright']) is not int or metadata['copyright'] != 2 or
                    not isinstance(metadata['tags'], list) or not 1 <= len(metadata['tags']) <= 10 or
                    len(set(metadata['tags'])) != len(metadata['tags']) or
                    any(not isinstance(t, str) or not 0 < len(t.strip()) <= 20 or ',' in t for t in metadata['tags'])):
                raise ValueError()
            return deepcopy(metadata)
        except (KeyError, TypeError, ValueError):
            raise HubError('SHORT_VIDEO_METADATA_INVALID', review=True) from None

    def _source_scope(self, source, policy):
        if source['source_id'] not in policy['acceptance_video_ids']:
            raise HubError('SHORT_VIDEO_NOT_IN_ACCEPTANCE_SCOPE', review=True)

    def create_acceptance(self, principal, download_job_id, metadata):
        self._operator(principal); policy = self.policy()
        source, asset, receipt = self._source(download_job_id)
        self._source_scope(source, policy)
        meta = self._metadata(metadata, source['payload']['url'])
        binding = {'version': self.VERSION, 'download_job_id': source['job_id'], 'source_id': source['source_id'],
                   'source_url': source['payload']['url'], 'channel_id': source['payload']['channel_id'],
                   'artifact_sha256': asset['sha256'], 'file_bytes': asset['bytes'], 'file': asset['file'],
                   'download_receipt_sha256': sha256_file(receipt), 'metadata': meta, 'metadata_sha256': digest(meta),
                   'site_uid': policy['target_site_uid'], 'account_name': policy['target_account_name'],
                   'policy_sha256': policy['_sha256'], 'human_review': False}
        key = digest(['bilibili', 'youtube', source['source_id'], 'site_uid:' + binding['site_uid'], self.KIND])
        now = time.time(); ident = str(uuid.uuid4())
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type=? AND policy_version=?",
                                  (source['source_id'], self.JOB_TYPE, self.POLICY)).fetchone()
            if existing:
                if json.loads(existing['payload']) != binding:
                    raise HubError('SHORT_VIDEO_ACCEPTANCE_INPUT_CHANGED', review=True)
                return existing['job_id']
            # Global source check also catches another website/official policy.
            if db.execute("SELECT 1 FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=?",
                          (source['source_id'],)).fetchone():
                raise HubError('SHORT_VIDEO_EXISTING_PUBLICATION_REQUIRES_LOOKUP', review=True)
            result = {self.STATE: {'version': self.VERSION, 'state': 'prepared', 'key': key, 'human_review': False,
                              'public_submission_approved': False}, 'download_performed': False, 'publish_performed': False}
            db.execute("""INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,
                       policy_version,payload,result,stage) VALUES(?,'youtube',?,?,'video','awaiting_review',?,?,?,?,?,'short_video_prepared')""",
                       (ident, source['source_id'], self.JOB_TYPE, now, now, self.POLICY, encode(binding), encode(result)))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (now, principal, 'short_video_prepare', ident, 'verified_short_download_no_public_submit_authorized'))
        return ident

    def snapshot(self, job_id, *, write=True):
        job = self.store.get(job_id)
        if not job or job['job_type'] != self.JOB_TYPE or job['source_platform'] != 'youtube' or job['policy_version'] != self.POLICY:
            raise HubError('SHORT_VIDEO_JOB_REQUIRED', review=True)
        binding = job['payload']; state = job['result'].get(self.STATE, {})
        if state.get('version') != self.VERSION or state.get('key') != digest(['bilibili', 'youtube', job['source_id'], 'site_uid:' + binding.get('site_uid', ''), self.KIND]):
            raise HubError('SHORT_VIDEO_CHECKPOINT_INVALID', review=True)
        if write:
            self._validate_snapshot_binding(job, binding, verify_artifact_hash=True)
        return job, binding, state

    def _validate_snapshot_binding(self, job, binding, *, verify_artifact_hash):
        """Share every source/binding predicate, including dynamic scope.

        Only the explicit False native-QA path omits the redundant whole-MP4
        read. Receipt bytes/SHA, real stat, policy, metadata and scope remain
        identical to snapshot(write=True); the caller must still run its full
        trusted machine acceptance before any native checkpoint/reservation.
        """
        policy = self.policy()
        source, asset, receipt = (self._source(binding['download_job_id']) if verify_artifact_hash is True else
            self._verified_source(binding['download_job_id'], verify_artifact_hash=verify_artifact_hash))
        self._source_scope(source, policy)
        if (binding.get('policy_sha256') != policy['_sha256'] or source['source_id'] != job['source_id'] or
                binding.get('channel_id') != source['payload']['channel_id'] or
                binding.get('site_uid') != policy['target_site_uid'] or binding.get('account_name') != policy['target_account_name'] or
                binding.get('artifact_sha256') != asset['sha256'] or binding.get('file_bytes') != asset['bytes'] or
                binding.get('file') != asset['file'] or binding.get('download_receipt_sha256') != sha256_file(receipt) or
                binding.get('human_review') is not False or
                digest(self._metadata(binding['metadata'], source['payload']['url'])) != binding.get('metadata_sha256')):
            raise HubError('SHORT_VIDEO_BINDING_CHANGED', review=True)
        return policy

    def _checkpoint_job_state(self, job, requested):
        return requested

    def _checkpoint_matches(self, current, job):
        return current is not None and current['updated_at'] == job['updated_at']

    def _checkpoint_committed(self, job_id):
        pass

    def _update(self, principal, job, state, *, job_state, stage, code=None, publication=None):
        job_state = self._checkpoint_job_state(job, job_state)
        now = time.time(); result = {**job['result'], self.STATE: state}
        if publication:
            result['publish_performed'] = True
        with self.store.transaction() as db:
            current = db.execute('SELECT * FROM jobs WHERE job_id=?', (job['job_id'],)).fetchone()
            if not self._checkpoint_matches(current, job):
                raise HubError('SHORT_VIDEO_JOB_CHANGED', review=True)
            if publication:
                known = db.execute('SELECT * FROM publications WHERE idempotency_key=? AND job_id=?',
                                   (state['key'], job['job_id'])).fetchone()
                if not known or known['state'] not in ('dispatching', 'unknown', 'awaiting_moderation', 'completed', 'failed'):
                    raise HubError('SHORT_VIDEO_DISPATCH_RESERVATION_REQUIRED', review=True)
                if known['external_id'] and known['external_id'] != publication['external_id']:
                    raise HubError('SHORT_VIDEO_BV_BINDING_MISMATCH', review=True)
                db.execute('UPDATE publications SET state=?,external_id=?,external_url=?,updated_at=? WHERE idempotency_key=?',
                           (publication['state'], publication['external_id'], publication['url'], now, state['key']))
            db.execute('UPDATE jobs SET state=?,stage=?,result=?,error_code=?,external_publish_id=COALESCE(?,external_publish_id),updated_at=? WHERE job_id=?',
                       (job_state, stage, encode(result), code, publication['external_id'] if publication else None, now, job['job_id']))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (now, principal, 'short_video_checkpoint', job['job_id'], state['state']))
        self._checkpoint_committed(job['job_id'])
        return {'job_id': job['job_id'], 'state': job_state, 'checkpoint': state['state']}

    def start_upload(self, principal, job_id, *, attempt_id, site_uid):
        self._operator(principal); job, binding, state = self.snapshot(job_id)
        try:
            if str(uuid.UUID(attempt_id)) != attempt_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise HubError('SHORT_VIDEO_ATTEMPT_INVALID') from None
        if site_uid != binding['site_uid']:
            raise HubError('TARGET_ACCOUNT_IDENTITY_MISMATCH', review=True)
        if state['state'] != 'prepared':
            raise HubError('SHORT_VIDEO_UPLOAD_RECONCILE_REQUIRED', review=True)
        selections = self.store.query("SELECT COUNT(*) n FROM audit WHERE target=? AND action='short_video_checkpoint' AND outcome='uploading'", (job_id,))[0]['n']
        if (selections >= 2 or state.get('private_upload_selections', selections) != selections or
                any(entry.get('attempt_id') == attempt_id for entry in state.get('private_upload_history', []))):
            raise HubError('SHORT_VIDEO_PRIVATE_UPLOAD_LIMIT', review=True)
        if self.store.query("SELECT 1 FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=?", (job['source_id'],)):
            raise HubError('SHORT_VIDEO_EXISTING_PUBLICATION_REQUIRES_LOOKUP', review=True)
        return self._update(principal, job, {**state, 'state': 'uploading', 'attempt_id': attempt_id, 'private_upload_selections': selections + 1},
                            job_state='upload_pending', stage='short_video_uploading')

    def operator_retry_private_upload(self, principal, job_id, actual_operator_reference):
        """One audited private-only retry; never evidence that an old blob is absent."""
        self._operator(principal); job, _, state = self.snapshot(job_id)
        if (job['state'] != 'awaiting_review' or state['state'] != 'upload_unknown' or
                state.get('public_submission_approved') is not False or not _reference(actual_operator_reference)):
            raise HubError('SHORT_VIDEO_PRIVATE_RETRY_NOT_ELIGIBLE', review=True)
        try:
            if str(uuid.UUID(state['attempt_id'])) != state['attempt_id']:
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HubError('SHORT_VIDEO_PRIVATE_RETRY_NOT_ELIGIBLE', review=True) from None
        def submission_evidence(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('owner_acceptance', 'result_observation', 'observed_submission', 'submission_receipt'):
                        return True
                    if key in ('public_submission_approved', 'owner_approved', 'public_submit', 'publish_performed') and item is True:
                        return True
                    if key == 'state' and item in ('submitting', 'submit_unknown', 'awaiting_moderation', 'completed'):
                        return True
                    if submission_evidence(item):
                        return True
            elif isinstance(value, list):
                return any(submission_evidence(item) for item in value)
            elif isinstance(value, str):
                return BV.search(value) is not None
            return False
        if job.get('external_publish_id') or submission_evidence(job['result']):
            raise HubError('SHORT_VIDEO_PRIVATE_RETRY_SUBMISSION_EVIDENCE', review=True)
        history = state.get('private_upload_history', [])
        if not isinstance(history, list) or history:
            raise HubError('SHORT_VIDEO_PRIVATE_UPLOAD_LIMIT', review=True)
        now = time.time()
        with self.store.transaction() as db:
            current = db.execute('SELECT updated_at FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not current or current['updated_at'] != job['updated_at']:
                raise HubError('SHORT_VIDEO_JOB_CHANGED', review=True)
            if db.execute("SELECT 1 FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=?", (job['source_id'],)).fetchone():
                raise HubError('SHORT_VIDEO_PRIVATE_RETRY_SUBMISSION_EVIDENCE', review=True)
            audits = db.execute('SELECT action,outcome FROM audit WHERE target=?', (job_id,)).fetchall()
            if any('reserve_submit' in row['action'] or row['outcome'] in ('submitting', 'submit_unknown', 'awaiting_moderation', 'completed') for row in audits):
                raise HubError('SHORT_VIDEO_PRIVATE_RETRY_SUBMISSION_EVIDENCE', review=True)
            count = sum(row['action'] == 'short_video_checkpoint' and row['outcome'] == 'uploading' for row in audits)
            if count != 1 or state.get('private_upload_selections', count) != count:
                raise HubError('SHORT_VIDEO_PRIVATE_UPLOAD_LIMIT', review=True)
            entry = {'version': 'short-video-private-upload-history-v1', 'attempt_id': state['attempt_id'],
                     'state': 'upload_unknown', 'previous_checkpoint': deepcopy(state),
                     'actual_operator_reference': actual_operator_reference,
                     'recorded_at': datetime.now(timezone.utc).isoformat(),
                     'orphan_private_blob_unknown': True, 'public_submit': False}
            updated = {**state, 'state': 'prepared', 'private_upload_history': [entry],
                       'private_upload_selections': 1, 'orphan_private_blob_unknown': True, 'public_submit': False}
            updated.pop('attempt_id', None)
            result = {**job['result'], self.STATE: updated, 'orphan_private_blob_unknown': True, 'public_submit': False}
            db.execute("UPDATE jobs SET state='awaiting_review',stage='short_video_private_retry_prepared',result=?,error_code='SHORT_VIDEO_PRIVATE_RETRY_PREPARED',updated_at=? WHERE job_id=?",
                       (encode(result), now, job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (now, principal, 'short_video_private_upload_retry', job_id, 'one_bounded_private_retry_old_blob_unknown_no_public_submit'))
        return {'job_id': job_id, 'state': 'awaiting_review', 'checkpoint': 'prepared',
                'private_upload_selections': 1, 'remaining_private_selections': 1,
                'orphan_private_blob_unknown': True, 'public_submit': False, 'old_attempt_evidence_preserved': True}

    def operator_amend_category_v1(self, principal, job_id, *, expected_metadata_sha256,
                                   actual_operator_reference, actual_owner_source_confirmation,
                                   source_confirmation_reference):
        """One local category revision; neither native synchronization nor public approval.

        The owner's source-characteristic statement is transcribed as a statement,
        not a claim of independent original-byte verification or human review.
        Existing attempt files, selection counts and unknown history are untouched.
        """
        self._operator(principal); job, binding, state = self.snapshot(job_id)
        if (job['state'] != 'upload_pending' or state['state'] != 'uploaded' or
                state.get('public_submission_approved') is not False or
                not isinstance(expected_metadata_sha256, str) or
                not re.fullmatch('[a-f0-9]{64}', expected_metadata_sha256) or
                binding['metadata_sha256'] != expected_metadata_sha256 or
                any(not _reference(value) for value in (actual_operator_reference,
                    actual_owner_source_confirmation, source_confirmation_reference)) or
                state.get('metadata_revision_history') or
                binding['metadata'].get('category_label') != '虚拟UP主'):
            raise HubError('SHORT_VIDEO_CATEGORY_AMENDMENT_NOT_ELIGIBLE', review=True)

        def public_evidence(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('owner_acceptance', 'result_observation', 'observed_submission', 'submission_receipt'):
                        return True
                    if key in ('public_submission_approved', 'owner_approved', 'public_submit', 'publish_performed') and item is True:
                        return True
                    if key == 'state' and item in ('submitting', 'submit_unknown', 'awaiting_moderation', 'completed'):
                        return True
                    if public_evidence(item):
                        return True
            elif isinstance(value, list):
                return any(public_evidence(item) for item in value)
            elif isinstance(value, str):
                return BV.search(value) is not None
            return False

        observation = state.get('upload_observation', {})
        observation_fields = {'version', 'attempt_id', 'site_uid', 'artifact_sha256', 'file_bytes',
                              'upload_reference_sha256', 'same_attempt_observed', 'server_file_hash_verified'}
        if (job.get('external_publish_id') or public_evidence(job['result']) or
                not isinstance(observation, dict) or set(observation) != observation_fields or
                observation.get('version') != 'short-video-browser-upload-v1' or
                observation.get('attempt_id') != state.get('attempt_id') or
                not re.fullmatch('[a-f0-9]{64}', str(observation.get('upload_reference_sha256', ''))) or
                any(observation.get(key) != binding[key] for key in ('site_uid', 'artifact_sha256', 'file_bytes')) or
                observation.get('same_attempt_observed') is not True or
                observation.get('server_file_hash_verified') is not False):
            raise HubError('SHORT_VIDEO_CATEGORY_AMENDMENT_SUBMISSION_OR_UPLOAD_EVIDENCE', review=True)
        metadata = {**deepcopy(binding['metadata']), 'category_label': '娱乐'}
        metadata = self._metadata(metadata, binding['source_url'])
        revised = {**binding, 'metadata': metadata, 'metadata_sha256': digest(metadata)}
        when = datetime.now(timezone.utc).isoformat(); now = time.time(); revision = str(uuid.uuid4())
        confirmation = {'version': 'operator-transcribed-source-characteristic-v1',
                        'characteristic': 'original_video_is_black',
                        'actual_owner_response': actual_owner_source_confirmation,
                        'reference': source_confirmation_reference, 'recorded_at': when,
                        'human_review': False, 'creator_original_bytes_verified': False,
                        'public_submission_authorized': False}
        history = {'version': 'short-video-category-metadata-revision-v1', 'revision_id': revision,
                   'previous_binding': deepcopy(binding), 'previous_binding_sha256': digest(binding),
                   'previous_metadata_sha256': binding['metadata_sha256'],
                   'new_metadata_sha256': revised['metadata_sha256'], 'new_binding_sha256': digest(revised),
                   'changed_fields': ['category_label'], 'previous_category_label': '虚拟UP主',
                   'new_category_label': '娱乐', 'attempt_id': state['attempt_id'],
                   'actual_operator_reference': actual_operator_reference, 'recorded_at': when,
                   'source_characteristic_confirmation': confirmation,
                   'remote_metadata_synchronized': False, 'public_submit': False}
        updated = {**state, 'metadata_revision_history': [history], 'active_metadata_revision_id': revision,
                   'remote_metadata_synchronized': False, 'source_characteristic_confirmation': confirmation,
                   'public_submission_approved': False}
        with self.store.transaction() as db:
            current = db.execute('SELECT updated_at FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not current or current['updated_at'] != job['updated_at']:
                raise HubError('SHORT_VIDEO_JOB_CHANGED', review=True)
            if db.execute("SELECT 1 FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=?", (job['source_id'],)).fetchone():
                raise HubError('SHORT_VIDEO_CATEGORY_AMENDMENT_SUBMISSION_OR_UPLOAD_EVIDENCE', review=True)
            audits = db.execute('SELECT action,outcome FROM audit WHERE target=?', (job_id,)).fetchall()
            if any('reserve_submit' in row['action'] or row['outcome'] in ('submitting', 'submit_unknown', 'awaiting_moderation', 'completed') for row in audits):
                raise HubError('SHORT_VIDEO_CATEGORY_AMENDMENT_SUBMISSION_OR_UPLOAD_EVIDENCE', review=True)
            count = sum(row['action'] == 'short_video_checkpoint' and row['outcome'] == 'uploading' for row in audits)
            if not 1 <= count <= 2 or state.get('private_upload_selections') != count:
                raise HubError('SHORT_VIDEO_PRIVATE_UPLOAD_LIMIT', review=True)
            db.execute('UPDATE jobs SET payload=?,result=?,stage=?,updated_at=? WHERE job_id=?',
                       (encode(revised), encode({**job['result'], self.STATE: updated}),
                        'short_video_metadata_revision_pending_native_sync', now, job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (now, principal, 'short_video_amend_category_v1', job_id,
                        'local_category_revision_no_native_sync_no_public_authorization'))
        return {'job_id': job_id, 'state': job['state'], 'checkpoint': 'uploaded',
                'metadata_revision_id': revision, 'metadata_sha256': revised['metadata_sha256'],
                'previous_metadata_sha256': binding['metadata_sha256'], 'binding_sha256': digest(revised),
                'private_upload_selections': count, 'remote_metadata_synchronized': False,
                'public_submission_approved': False, 'public_submit': False,
                'source_characteristic_is_owner_statement': True, 'old_attempt_evidence_preserved': True}

    def operator_remove_public_permission_line_v1(self, principal, job_id, *, expected_metadata_sha256,
                                                  actual_owner_response, owner_response_reference,
                                                  actual_operator_reference):
        """Remove one exact public footer; retain internal rights and all evidence.

        Only the existing single category revision may precede this local
        revision. No native synchronization, approval, submit or deletion is
        performed by this method.
        """
        self._operator(principal); job, binding, state = self.snapshot(job_id)
        history = state.get('metadata_revision_history')
        if (job['state'] != 'upload_pending' or state['state'] != 'uploaded' or
                state.get('public_submission_approved') is not False or
                not isinstance(expected_metadata_sha256, str) or
                not re.fullmatch('[a-f0-9]{64}', expected_metadata_sha256) or
                binding['metadata_sha256'] != expected_metadata_sha256 or
                any(not _reference(value) for value in (actual_owner_response,
                    owner_response_reference, actual_operator_reference)) or
                not isinstance(history, list) or len(history) != 1):
            raise HubError('SHORT_VIDEO_PERMISSION_LINE_REMOVAL_NOT_ELIGIBLE', review=True)
        try:
            first = history[0]
            fields = {'version', 'revision_id', 'previous_binding', 'previous_binding_sha256',
                      'previous_metadata_sha256', 'new_metadata_sha256', 'new_binding_sha256',
                      'changed_fields', 'previous_category_label', 'new_category_label', 'attempt_id',
                      'actual_operator_reference', 'recorded_at', 'source_characteristic_confirmation',
                      'remote_metadata_synchronized', 'public_submit'}
            previous = first['previous_binding']; confirmation = first['source_characteristic_confirmation']
            previous_metadata = self._metadata(previous['metadata'], previous['source_url'])
            folded_metadata = self._metadata({**deepcopy(previous_metadata), 'category_label': '娱乐'}, binding['source_url'])
            folded = {**deepcopy(previous), 'metadata': folded_metadata, 'metadata_sha256': digest(folded_metadata)}
            if (set(first) != fields or first['version'] != 'short-video-category-metadata-revision-v1' or
                    str(uuid.UUID(first['revision_id'])) != first['revision_id'] or
                    state.get('active_metadata_revision_id') != first['revision_id'] or
                    first['previous_binding_sha256'] != digest(previous) or
                    previous['metadata_sha256'] != digest(previous_metadata) or
                    first['previous_metadata_sha256'] != previous['metadata_sha256'] or
                    previous_metadata.get('category_label') != '虚拟UP主' or
                    first['changed_fields'] != ['category_label'] or first['previous_category_label'] != '虚拟UP主' or
                    first['new_category_label'] != '娱乐' or first['attempt_id'] != state['attempt_id'] or
                    folded != binding or first['new_binding_sha256'] != digest(binding) or
                    first['new_metadata_sha256'] != binding['metadata_sha256'] or
                    first['public_submit'] is not False or first['remote_metadata_synchronized'] is not False or
                    not _reference(first['actual_operator_reference']) or not _reference(first['recorded_at']) or
                    set(confirmation) != {'version', 'characteristic', 'actual_owner_response', 'reference', 'recorded_at',
                                          'human_review', 'creator_original_bytes_verified', 'public_submission_authorized'} or
                    confirmation['version'] != 'operator-transcribed-source-characteristic-v1' or
                    confirmation['characteristic'] != 'original_video_is_black' or
                    any(confirmation.get(k) is not False for k in ('human_review', 'creator_original_bytes_verified', 'public_submission_authorized')) or
                    any(not _reference(confirmation.get(k)) for k in ('actual_owner_response', 'reference', 'recorded_at')) or
                    state.get('source_characteristic_confirmation') != confirmation):
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            raise HubError('SHORT_VIDEO_PERMISSION_LINE_REMOVAL_HISTORY_BINDING_REQUIRED', review=True) from None

        def public_evidence(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ('owner_acceptance', 'result_observation', 'observed_submission', 'submission_receipt'):
                        return True
                    if key in ('public_submission_approved', 'owner_approved', 'public_submit', 'publish_performed') and item is True:
                        return True
                    if key == 'state' and item in ('submitting', 'submit_unknown', 'awaiting_moderation', 'completed'):
                        return True
                    if public_evidence(item):
                        return True
            elif isinstance(value, list):
                return any(public_evidence(item) for item in value)
            elif isinstance(value, str):
                return BV.search(value) is not None
            return False

        observation = state.get('upload_observation', {})
        observation_fields = {'version', 'attempt_id', 'site_uid', 'artifact_sha256', 'file_bytes',
                              'upload_reference_sha256', 'same_attempt_observed', 'server_file_hash_verified'}
        intent = self.settings.root/'media'/job_id/'recovered-submit-intent.json'
        def has_intent():
            return intent.exists() or any(p.is_symlink() for p in (intent, *intent.parents))
        if (job.get('external_publish_id') or public_evidence(job['result']) or has_intent() or
                not isinstance(observation, dict) or set(observation) != observation_fields or
                observation.get('version') != 'short-video-browser-upload-v1' or
                observation.get('attempt_id') != state.get('attempt_id') or
                not re.fullmatch('[a-f0-9]{64}', str(observation.get('upload_reference_sha256', ''))) or
                any(observation.get(key) != binding[key] for key in ('site_uid', 'artifact_sha256', 'file_bytes')) or
                observation.get('same_attempt_observed') is not True or
                observation.get('server_file_hash_verified') is not False):
            raise HubError('SHORT_VIDEO_PERMISSION_LINE_REMOVAL_SUBMISSION_OR_UPLOAD_EVIDENCE', review=True)
        removed = '转载许可编号：114514（字幕组提供的许可范围）。'
        suffix = '\n' + removed
        description = binding['metadata']['description']
        if not description.endswith(suffix) or description.endswith('\r' + suffix):
            raise HubError('SHORT_VIDEO_PERMISSION_LINE_REMOVAL_EXACT_LAST_LINE_REQUIRED', review=True)
        metadata = self._metadata({**deepcopy(binding['metadata']), 'description': description[:-len(suffix)]}, binding['source_url'])
        revised = {**deepcopy(binding), 'metadata': metadata, 'metadata_sha256': digest(metadata)}
        when = datetime.now(timezone.utc).isoformat(); now = time.time(); revision = str(uuid.uuid4())
        entry = {'version': 'public-permission-line-removal-v1', 'revision_id': revision,
                 'previous_binding': deepcopy(binding), 'previous_binding_sha256': digest(binding),
                 'previous_metadata_sha256': binding['metadata_sha256'],
                 'new_metadata_sha256': revised['metadata_sha256'], 'new_binding_sha256': digest(revised),
                 'changed_fields': ['description'], 'removed_last_line': removed,
                 'previous_revision_id': first['revision_id'], 'attempt_id': state['attempt_id'],
                 'actual_owner_response': actual_owner_response, 'owner_response_reference': owner_response_reference,
                 'actual_operator_reference': actual_operator_reference, 'recorded_at': when,
                 'public_submit': False, 'remote_metadata_synchronized': False, 'internal_rights_records_preserved': True}
        updated = {**state, 'metadata_revision_history': [deepcopy(first), entry],
                   'active_metadata_revision_id': revision, 'remote_metadata_synchronized': False}
        with self.store.transaction() as db:
            current = db.execute('SELECT updated_at FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not current or current['updated_at'] != job['updated_at']:
                raise HubError('SHORT_VIDEO_JOB_CHANGED', review=True)
            if (has_intent() or db.execute("SELECT 1 FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=?", (job['source_id'],)).fetchone()):
                raise HubError('SHORT_VIDEO_PERMISSION_LINE_REMOVAL_SUBMISSION_OR_UPLOAD_EVIDENCE', review=True)
            audits = db.execute('SELECT action,outcome FROM audit WHERE target=?', (job_id,)).fetchall()
            if any('reserve_submit' in row['action'] or row['outcome'] in ('submitting', 'submit_unknown', 'awaiting_moderation', 'completed') for row in audits):
                raise HubError('SHORT_VIDEO_PERMISSION_LINE_REMOVAL_SUBMISSION_OR_UPLOAD_EVIDENCE', review=True)
            count = sum(row['action'] == 'short_video_checkpoint' and row['outcome'] == 'uploading' for row in audits)
            if not 1 <= count <= 2 or state.get('private_upload_selections') != count:
                raise HubError('SHORT_VIDEO_PRIVATE_UPLOAD_LIMIT', review=True)
            db.execute('UPDATE jobs SET payload=?,result=?,stage=?,updated_at=? WHERE job_id=?',
                       (encode(revised), encode({**job['result'], self.STATE: updated}),
                        'short_video_metadata_revision_pending_native_sync', now, job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (now, principal, 'short_video_remove_public_permission_line_v1', job_id,
                        'public_description_footer_removed_internal_rights_preserved_no_public_authorization'))
        return {'job_id': job_id, 'state': job['state'], 'checkpoint': 'uploaded',
                'metadata_revision_id': revision, 'previous_revision_id': first['revision_id'],
                'metadata_sha256': revised['metadata_sha256'], 'previous_metadata_sha256': binding['metadata_sha256'],
                'binding_sha256': digest(revised), 'private_upload_selections': count,
                'remote_metadata_synchronized': False, 'public_submission_approved': False, 'public_submit': False,
                'internal_rights_records_preserved': True, 'old_attempt_evidence_preserved': True}

    def record_upload(self, principal, job_id, observation):
        self._operator(principal); job, binding, state = self.snapshot(job_id)
        required = {'version', 'attempt_id', 'site_uid', 'artifact_sha256', 'file_bytes', 'upload_reference_sha256', 'same_attempt_observed', 'server_file_hash_verified'}
        if (state['state'] != 'uploading' or not isinstance(observation, dict) or set(observation) != required or
                observation.get('version') != 'short-video-browser-upload-v1' or observation.get('attempt_id') != state['attempt_id'] or
                any(observation.get(k) != binding[k] for k in ('site_uid', 'artifact_sha256', 'file_bytes')) or
                observation.get('same_attempt_observed') is not True or observation.get('server_file_hash_verified') is not False or
                not re.fullmatch('[a-f0-9]{64}', str(observation.get('upload_reference_sha256', '')))):
            raise HubError('SHORT_VIDEO_UPLOAD_OBSERVATION_INVALID', review=True)
        return self._update(principal, job, {**state, 'state': 'uploaded', 'upload_observation': deepcopy(observation)},
                            job_state='upload_pending', stage='short_video_uploaded')

    def approve(self, principal, job_id, *, binding_sha256, actual_owner_response, approval_reference):
        """Transcribe a real explicit one-artifact acceptance; never human review."""
        self._operator(principal); job, binding, state = self.snapshot(job_id)
        if state['state'] not in ('prepared', 'uploaded') or binding_sha256 != digest(binding) or not _reference(actual_owner_response) or not _reference(approval_reference):
            raise HubError('SHORT_VIDEO_EXPLICIT_ARTIFACT_ACCEPTANCE_REQUIRED', review=True)
        approval = {'version': 'operator-transcribed-short-video-owner-acceptance-v1',
                    'binding_sha256': binding_sha256, 'actual_owner_response': actual_owner_response,
                    'approval_reference': approval_reference, 'recorded_at': datetime.now(timezone.utc).isoformat(),
                    'cryptographically_signed_by_owner': False, 'human_review': False, 'automatic_other_video_publication_authorized': False}
        return self._update(principal, job, {**state, 'public_submission_approved': True, 'owner_acceptance': approval},
                            job_state=job['state'], stage=job['stage'])

    def reserve_submit(self, principal, job_id, *, attempt_id, site_uid, upload_reference_sha256):
        self._operator(principal); job, binding, state = self.snapshot(job_id)
        approval = state.get('owner_acceptance', {})
        if (state['state'] != 'uploaded' or state.get('public_submission_approved') is not True or
                approval.get('binding_sha256') != digest(binding) or state['attempt_id'] != attempt_id or
                site_uid != binding['site_uid'] or upload_reference_sha256 != state['upload_observation']['upload_reference_sha256']):
            raise HubError('SHORT_VIDEO_EXPLICIT_ARTIFACT_ACCEPTANCE_REQUIRED', review=True)
        return self._reserve_submit_once(principal, job, binding, state, site_uid=site_uid)

    def _reserve_submit_once(self, principal, job, binding, state, *, site_uid):
        """Persist one public-send intent after the caller's own authorization gate.

        This helper grants no approval. Callers must validate their versioned
        authorization before entry; the source-wide publication check remains
        atomic across website/official modes and policy versions.
        """
        job_id = job['job_id']
        job_state = self._checkpoint_job_state(job, 'publish_unknown')
        now = time.time()
        with self.store.transaction() as db:
            current = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if not self._checkpoint_matches(current, job):
                raise HubError('SHORT_VIDEO_JOB_CHANGED', review=True)
            rows = db.execute("SELECT * FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=?", (job['source_id'],)).fetchall()
            if rows:
                return {'can_click_submit': False, 'state': rows[0]['state'], 'reconcile_required': True}
            db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (state['key'], job_id, 'bilibili', 'youtube', job['source_id'], 'site_uid:' + site_uid, self.KIND,
                        'dispatching', None, None, now))
            updated = {**state, 'state': 'submitting'}
            db.execute("UPDATE jobs SET state=?,stage='short_video_submission_reserved',error_code='RECONCILE_BEFORE_RETRY',result=?,updated_at=? WHERE job_id=?",
                       (job_state, encode({**job['result'], self.STATE: updated}), now, job_id))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                       (now, principal, 'short_video_reserve_submit', job_id, 'one_artifact_one_click_reserved'))
        self._checkpoint_committed(job_id)
        return {'can_click_submit': True, 'state': 'dispatching', 'idempotency_key': state['key']}

    def mark_unknown(self, principal, job_id):
        self._operator(principal); job, _, state = self.snapshot(job_id, write=False)
        if state['state'] not in ('uploading', 'submitting', 'upload_unknown', 'submit_unknown'):
            raise HubError('SHORT_VIDEO_UNRESOLVED_ATTEMPT_REQUIRED', review=True)
        submitted = state['state'] in ('submitting', 'submit_unknown')
        code = 'PUBLISH_UNKNOWN' if submitted else 'SHORT_VIDEO_UPLOAD_RESULT_UNKNOWN'
        out = self._update(principal, job, {**state, 'state': 'submit_unknown' if submitted else 'upload_unknown'},
                           job_state='publish_unknown' if submitted else 'awaiting_review', stage='short_video_reconcile_required', code=code)
        if submitted:
            with self.store.transaction() as db:
                db.execute("UPDATE publications SET state='unknown',updated_at=? WHERE idempotency_key=? AND state='dispatching'", (time.time(), state['key']))
        return out

    def _result_extra_state(self, job, binding, state, observation, *, lookup):
        return {}

    def record_result(self, principal, job_id, observation, *, lookup=False):
        """Same-attempt BV evidence, or a lookup bound to an already recorded BV."""
        self._operator(principal); job, binding, state = self.snapshot(job_id, write=False)
        try:
            if (state['state'] not in ('submitting', 'submit_unknown', 'awaiting_moderation', 'completed', 'failed') or
                    not isinstance(observation, dict) or observation.get('version') != self.RESULT_OBSERVATION_VERSION or
                    observation.get('site_uid') != binding['site_uid'] or observation.get('source_url') != binding['source_url'] or
                    observation.get('metadata_sha256') != binding['metadata_sha256'] or
                    not BV.fullmatch(observation.get('external_id', '')) or
                    observation.get('url') != 'https://www.bilibili.com/video/' + observation['external_id'] or
                    observation.get('state') not in ('awaiting_moderation', 'completed', 'failed') or
                    observation['state'] == 'failed' and observation.get('error_code') != 'BILIBILI_MODERATION_REJECTED'):
                raise ValueError()
            previous = state.get('result_observation')
            if lookup:
                if not previous or previous['external_id'] != observation['external_id']:
                    raise ValueError()
            elif observation.get('attempt_id') != state['attempt_id'] or observation.get('same_submit_attempt_observed') is not True:
                raise ValueError()
            if previous and previous['external_id'] != observation['external_id']:
                raise ValueError()
            # Whitelist persisted fields; no native uploader IDs or arbitrary page state.
            safe = {k: observation[k] for k in ('version', 'attempt_id', 'site_uid', 'source_url', 'metadata_sha256', 'external_id', 'url', 'state', 'error_code') if k in observation}
            if observation.get('public_accessible') is True:
                if not lookup or observation['state'] != 'completed':
                    raise ValueError()
                when = datetime.fromisoformat(observation['public_lookup_observed_at'].replace('Z', '+00:00'))
                if when.tzinfo is None or not 0 <= time.time() - when.timestamp() <= 300:
                    raise ValueError()
                safe.update(public_accessible=True, public_lookup_observed_at=when.isoformat())
            extra = self._result_extra_state(job,binding,state,observation,lookup=lookup)
            result = {k: observation[k] for k in ('external_id', 'url', 'state')}
            return self._update(principal, job, {**state, 'state': observation['state'], 'result_observation': safe, **extra},
                                job_state=observation['state'], stage='short_video_result_recorded', code=observation.get('error_code'), publication=result)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HubError('SHORT_VIDEO_RESULT_CAUSAL_BINDING_REQUIRED', review=True) from None

    def set_test_retention_policy(self, principal, job_id, *, actual_owner_response, reference):
        """Keep the remote test post; retain local tests until formal workflow launch."""
        self._operator(principal)
        if not _reference(actual_owner_response) or not _reference(reference):
            raise HubError('SHORT_VIDEO_RETENTION_OWNER_REQUEST_REQUIRED', review=True)
        job, binding, state = self.snapshot(job_id)
        existing = state.get('test_retention_policy')
        hold = {'version': 'short-video-test-retention-policy-v1',
                'job_id': job_id, 'download_job_id': binding['download_job_id'],
                'source_id': binding['source_id'], 'artifact_sha256': binding['artifact_sha256'],
                'actual_owner_response': actual_owner_response, 'reference': reference,
                'remote_video_deletion_requires_new_owner_confirmation': True,
                'local_test_cleanup_requires_formal_workflow_launch': True}
        if existing is not None:
            if existing != hold:
                raise HubError('SHORT_VIDEO_RETENTION_HOLD_ALREADY_EXISTS', review=True)
            return {'held': True, 'hold': existing, 'delete_performed': False}
        self._update(principal, job, {**state, 'test_retention_policy': hold},
                     job_state=job['state'], stage='short_video_test_retention_policy')
        return {'held': True, 'hold': hold, 'delete_performed': False}

    def cleanup_eligibility(self, job_id):
        """Read-only exact candidate; never deletes or infers visibility from BV alone."""
        job, binding, state = self.snapshot(job_id, write=False)
        if 'test_retention_policy' in state:
            return {'eligible': False, 'reason': 'LOCAL_TESTS_RETAINED_UNTIL_FORMAL_WORKFLOW_LAUNCH',
                    'delete_performed': False}
        observation = state.get('result_observation', {})
        if (job['state'] != 'completed' or state['state'] != 'completed' or observation.get('state') != 'completed' or
                observation.get('public_accessible') is not True):
            return {'eligible': False, 'reason': 'PUBLIC_COMPLETION_AND_FRESH_LOOKUP_REQUIRED', 'delete_performed': False}
        try:
            when = datetime.fromisoformat(observation['public_lookup_observed_at'].replace('Z', '+00:00'))
            if when.tzinfo is None or not 0 <= time.time() - when.timestamp() <= 86400:
                raise ValueError()
            rows = self.store.query('SELECT * FROM publications WHERE idempotency_key=? AND job_id=?',
                                    (state['key'], job_id))
            if (len(rows) != 1 or rows[0]['state'] != 'completed' or rows[0]['external_id'] != observation['external_id'] or
                    rows[0]['external_url'] != observation['url'] or rows[0]['external_id'] != job['external_publish_id'] or
                    rows[0]['account_id'] != 'site_uid:' + binding['site_uid'] or rows[0]['kind'] != self.KIND or
                    rows[0]['source_id'] != binding['source_id'] or observation['site_uid'] != binding['site_uid'] or
                    observation['source_url'] != binding['source_url'] or observation['metadata_sha256'] != binding['metadata_sha256']):
                raise ValueError()
            source, asset, receipt = self._source(binding['download_job_id'])
            if (source['source_id'] != binding['source_id'] or asset['file'] != binding['file'] or
                    asset['sha256'] != binding['artifact_sha256'] or asset['bytes'] != binding['file_bytes'] or
                    sha256_file(receipt) != binding['download_receipt_sha256']):
                raise ValueError()
            return {'eligible': True, 'delete_performed': False, 'job_id': job_id, 'download_job_id': source['job_id'],
                    'publication_key': state['key'], 'external_id': observation['external_id'], 'url': observation['url'],
                    'candidate': {'file': asset['file'], 'bytes': asset['bytes'], 'sha256': asset['sha256']},
                    'scope': 'exact_verified_mp4_only_other_files_and_receipts_retained'}
        except (OSError, KeyError, TypeError, ValueError, AttributeError, HubError):
            return {'eligible': False, 'reason': 'PUBLIC_COMPLETION_FILE_OR_BINDING_CHANGED', 'delete_performed': False}
