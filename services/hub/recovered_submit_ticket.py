"""Coordinator-backed recovered-editor tickets; no browser, upload or file writer.

The operator must transcribe a real, exact-artifact owner response. This function
does not supply one, write root-owned mounts, click, or mark publication complete.
After reservation or any interrupted issuance, reconcile the durable publication;
never reset its state to mint another ticket.
"""
from copy import deepcopy
import hashlib
import json
import time
import uuid

from .errors import HubError
from .short_video_web import PRINCIPAL, STATE
from .youtube_download import digest

FIELDS = ('job_id', 'attempt_id', 'recovery_id', 'target_site_uid', 'source_url', 'source_id',
          'download_job_id', 'file', 'file_bytes', 'artifact_sha256', 'metadata_sha256',
          'upload_metadata_sha256', 'binding_sha256', 'metadata_revision_id', 'upload_reference_sha256')
PUBLIC_PERMISSION_LINE = '转载许可编号：114514（字幕组提供的许可范围）。'


def _wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _reference(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 2048


def _validate_history(binding, state, spec):
    """Fold only the existing category revision and one exact public-line removal.

    The immutable original upload metadata remains the first hash, not the
    second record's intermediate hash. A caller cannot hide a second revision
    behind the old single-record browser schema.
    """
    history = state['metadata_revision_history']
    if not isinstance(history, list) or len(history) not in (1, 2):
        raise ValueError()
    if ('metadata_revision_history' in spec and spec['metadata_revision_history'] != history or
            len(history) == 2 and 'metadata_revision_history' not in spec or
            'metadata_revision' in spec and spec['metadata_revision'] != history[-1]):
        raise ValueError()
    folded = deepcopy(history[0]['previous_binding'])
    if (not isinstance(folded, dict) or folded.get('human_review') is not False or
            folded['metadata_sha256'] != spec['upload_metadata_sha256'] or
            digest(folded['metadata']) != spec['upload_metadata_sha256']):
        raise ValueError()
    for index, revision in enumerate(history):
        if (str(uuid.UUID(revision['revision_id'])) != revision['revision_id'] or
                revision['attempt_id'] != spec['attempt_id'] or revision['previous_binding'] != folded or
                revision['previous_binding_sha256'] != digest(folded) or
                revision['previous_metadata_sha256'] != digest(folded['metadata']) or
                revision.get('public_submit') is not False or revision.get('remote_metadata_synchronized') is not False):
            raise ValueError()
        metadata = deepcopy(folded['metadata'])
        if index == 0:
            fields = {'version', 'revision_id', 'previous_binding', 'previous_binding_sha256',
                      'previous_metadata_sha256', 'new_metadata_sha256', 'new_binding_sha256',
                      'changed_fields', 'previous_category_label', 'new_category_label', 'attempt_id',
                      'actual_operator_reference', 'recorded_at', 'source_characteristic_confirmation',
                      'remote_metadata_synchronized', 'public_submit'}
            confirmation = revision['source_characteristic_confirmation']
            if (set(revision) != fields or revision['version'] != 'short-video-category-metadata-revision-v1' or
                    revision['changed_fields'] != ['category_label'] or metadata.get('category_label') != '虚拟UP主' or
                    revision['previous_category_label'] != '虚拟UP主' or revision['new_category_label'] != '娱乐' or
                    not all(_reference(revision.get(key)) for key in ('actual_operator_reference', 'recorded_at')) or
                    set(confirmation) != {'version', 'characteristic', 'actual_owner_response', 'reference', 'recorded_at',
                                          'human_review', 'creator_original_bytes_verified', 'public_submission_authorized'} or
                    confirmation['version'] != 'operator-transcribed-source-characteristic-v1' or
                    confirmation['characteristic'] != 'original_video_is_black' or
                    any(confirmation.get(key) is not False for key in
                        ('human_review', 'creator_original_bytes_verified', 'public_submission_authorized')) or
                    not all(_reference(confirmation.get(key)) for key in ('actual_owner_response', 'reference', 'recorded_at')) or
                    state.get('source_characteristic_confirmation') != confirmation):
                raise ValueError()
            metadata['category_label'] = '娱乐'
        else:
            suffix = '\n' + PUBLIC_PERMISSION_LINE
            fields = {'version', 'revision_id', 'previous_binding', 'previous_binding_sha256',
                      'previous_metadata_sha256', 'new_metadata_sha256', 'new_binding_sha256',
                      'changed_fields', 'removed_last_line', 'previous_revision_id', 'attempt_id',
                      'actual_owner_response', 'owner_response_reference', 'actual_operator_reference',
                      'recorded_at', 'public_submit', 'remote_metadata_synchronized', 'internal_rights_records_preserved'}
            if (set(revision) != fields or revision['version'] != 'public-permission-line-removal-v1' or
                    revision['changed_fields'] != ['description'] or
                    revision['previous_revision_id'] != history[index-1]['revision_id'] or
                    revision['removed_last_line'] != PUBLIC_PERMISSION_LINE or
                    not isinstance(metadata.get('description'), str) or not metadata['description'].endswith(suffix) or
                    metadata['description'].endswith('\r'+suffix) or
                    revision.get('internal_rights_records_preserved') is not True or
                    revision.get('actual_owner_response') != '转载许可编号删掉' or
                    not all(_reference(revision.get(key)) for key in ('owner_response_reference', 'actual_operator_reference'))):
                raise ValueError()
            metadata['description'] = metadata['description'][:-len(suffix)]
        folded = {**folded, 'metadata': metadata, 'metadata_sha256': digest(metadata)}
        if revision['new_metadata_sha256'] != digest(metadata) or revision['new_binding_sha256'] != digest(folded):
            raise ValueError()
    if (folded != binding or state['active_metadata_revision_id'] != history[-1]['revision_id'] or
            spec['metadata_revision_id'] != history[-1]['revision_id']):
        raise ValueError()


def issue_recovered_submit_ticket(service, job_id, spec, *, principal, actual_owner_response, approval_reference,
                                 ttl_seconds=900, clock=time.time):
    """Return exact ticket bytes only after real approve + one-use reserve_submit.

    A privileged caller may install these returned bytes, unchanged, as
    /run/short-video-submission/{approval,reservation}.json in a root-owned RO
    directory. App-written JSON is rejected by the native gate.
    """
    service._operator(principal)
    if type(ttl_seconds) is not int or not 0 < ttl_seconds <= 900 or not isinstance(spec, dict):
        raise HubError('RECOVERED_SUBMIT_TICKET_INPUT_INVALID')
    try:
        for key in ('job_id', 'attempt_id', 'uploaded_attempt_id', 'recovery_id', 'download_job_id', 'metadata_revision_id'):
            if str(uuid.UUID(spec[key])) != spec[key]:
                raise ValueError()
        if spec['job_id'] != job_id or spec['uploaded_attempt_id'] != spec['attempt_id']:
            raise ValueError()
        job, binding, state = service.snapshot(job_id)
        expected = {'job_id': job_id, 'attempt_id': state['attempt_id'], 'recovery_id': spec['recovery_id'],
                    'target_site_uid': binding['site_uid'], 'source_url': binding['source_url'],
                    'source_id': job['source_id'], 'download_job_id': binding['download_job_id'],
                    'file': binding['file'], 'file_bytes': binding['file_bytes'], 'artifact_sha256': binding['artifact_sha256'],
                    'metadata_sha256': binding['metadata_sha256'], 'upload_metadata_sha256': spec['upload_metadata_sha256'],
                    'binding_sha256': digest(binding), 'metadata_revision_id': state['active_metadata_revision_id'],
                    'upload_reference_sha256': state['upload_observation']['upload_reference_sha256']}
        if (state['state'] != 'uploaded' or {key: spec[key] for key in FIELDS} != expected or
                spec.get('metadata') != binding['metadata'] or digest(spec['metadata']) != binding['metadata_sha256']):
            raise ValueError()
        _validate_history(binding, state, spec)
        intent = service.settings.root / 'media' / job_id / 'recovered-submit-intent.json'
        if intent.exists() or intent.is_symlink():
            raise ValueError()
        existing = service.store.query(
            "SELECT 1 FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=?",
            (job['source_id'],))
        if existing:
            raise ValueError()
        if service.store.query("SELECT 1 FROM audit WHERE target=? AND "
                "(action LIKE '%reserve_submit%' OR outcome IN ('submitting','submit_unknown','awaiting_moderation','completed')) "
                "LIMIT 1", (job_id,)):
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError, StopIteration):
        raise HubError('RECOVERED_SUBMIT_ACTIVE_BINDING_REQUIRED', review=True) from None

    # These are the existing coordinator's audited, durable boundaries.
    service.approve(principal, job_id, binding_sha256=expected['binding_sha256'],
                    actual_owner_response=actual_owner_response, approval_reference=approval_reference)
    permit = service.reserve_submit(principal, job_id, attempt_id=expected['attempt_id'],
                                    site_uid=expected['target_site_uid'],
                                    upload_reference_sha256=expected['upload_reference_sha256'])
    if permit.get('can_click_submit') is not True or permit.get('state') != 'dispatching':
        raise HubError('RECOVERED_SUBMIT_RECONCILIATION_REQUIRED', review=True)
    current = service.store.get(job_id)
    checkpoint = current['result'][STATE]
    rows = service.store.query("SELECT * FROM publications WHERE idempotency_key=? AND job_id=?",
                               (permit['idempotency_key'], job_id))
    audits = service.store.query("SELECT * FROM audit WHERE principal=? AND action='short_video_reserve_submit' "
                                 "AND target=? AND outcome='one_artifact_one_click_reserved'", (PRINCIPAL, job_id))
    if (current['payload'] != binding or current['state'] != 'publish_unknown' or checkpoint['state'] != 'submitting' or
            checkpoint['owner_acceptance']['binding_sha256'] != expected['binding_sha256'] or
            checkpoint.get('public_submission_approved') is not True or len(rows) != 1 or
            rows[0]['state'] != 'dispatching' or rows[0]['external_id'] or rows[0]['external_url'] or not audits):
        raise HubError('RECOVERED_SUBMIT_PERSISTED_RESERVATION_REQUIRED', review=True)
    issued = int(clock())
    ticket = str(uuid.uuid4())
    common = {'ticket_id': ticket, 'issued_at_unix': issued, 'expires_at_unix': issued + ttl_seconds,
              'binding': deepcopy(expected)}
    approval = {**common, 'version': 'short-video-recovered-owner-approval-v1',
                'owner_acceptance': deepcopy(checkpoint['owner_acceptance'])}
    approval_raw = _wire(approval)
    reservation = {**common, 'version': 'short-video-recovered-submit-reservation-v1',
                   'approval_sha256': hashlib.sha256(approval_raw).hexdigest(), 'reservation': deepcopy(permit),
                   'persisted_state': {'job_state': 'publish_unknown', 'pipeline_state': 'submitting',
                                       'publication_state': 'dispatching', 'audit_action': 'short_video_reserve_submit',
                                       'principal': PRINCIPAL}}
    return {'approval_bytes': approval_raw, 'reservation_bytes': _wire(reservation), 'ticket_id': ticket,
            'job_id': job_id, 'reconcile_before_retry': True, 'native_click_performed': False}
