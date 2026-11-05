"""Owned coordinator fixtures only; no owner approval, browser or site calls."""
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

import pytest

from services.hub.errors import HubError
from services.hub.recovered_submit_ticket import issue_recovered_submit_ticket
from services.hub.short_video_web import PRINCIPAL, STATE
from services.hub.youtube_download import digest
from test_short_video_web import short_video, category_amendment_uploaded, amend_category


def revised(f):
    _, store, service, _, _, _ = f
    job, attempt, before = category_amendment_uploaded(f)
    change = amend_category(service, job, before['metadata_sha256'])
    current = store.get(job); binding = current['payload']
    spec = {**{key: binding[key] for key in ('source_id', 'download_job_id', 'source_url', 'file',
            'file_bytes', 'artifact_sha256', 'metadata_sha256', 'metadata')},
            'job_id': job, 'attempt_id': attempt, 'uploaded_attempt_id': attempt,
            'recovery_id': str(uuid.uuid4()), 'target_site_uid': binding['site_uid'],
            'upload_metadata_sha256': before['metadata_sha256'], 'binding_sha256': digest(binding),
            'metadata_revision_id': change['metadata_revision_id'], 'upload_reference_sha256': 'b'*64}
    return job, spec


def issue(service, job, spec, **kwargs):
    return issue_recovered_submit_ticket(service, job, spec,
        principal=PRINCIPAL,
        actual_owner_response='Owned synthetic fixture acceptance, not a real owner response',
        approval_reference='Owned offline fixture only', **kwargs)


def test_real_coordinator_approval_and_durable_one_use_reservation_are_exported_and_node_validates(short_video):
    _, store, service, _, _, _ = short_video
    job, spec = revised(short_video)
    ticket = issue(service, job, spec)
    approval = json.loads(ticket['approval_bytes']); reservation = json.loads(ticket['reservation_bytes'])
    assert reservation['approval_sha256'] == hashlib.sha256(ticket['approval_bytes']).hexdigest()
    assert approval['binding'] == reservation['binding']
    assert approval['owner_acceptance']['human_review'] is False
    assert approval['owner_acceptance']['cryptographically_signed_by_owner'] is False
    assert approval['owner_acceptance']['automatic_other_video_publication_authorized'] is False
    current = store.get(job)
    assert current['state'] == 'publish_unknown' and current['result'][STATE]['state'] == 'submitting'
    assert store.query('SELECT state,external_id FROM publications') == [{'state': 'dispatching', 'external_id': None}]
    root = Path(__file__).resolve().parents[1]
    code = ("import {validateRecoveredTickets} from "+json.dumps((root/'adapters/recovered_submit_gate.mjs').as_uri())+";"
            "let raw='';for await(const p of process.stdin)raw+=p;"
            "const v=validateRecoveredTickets(JSON.parse(raw));console.log(JSON.stringify({ticket_id:v.ticket_id}));")
    result = subprocess.run(['node', '--input-type=module', '-e', code], input=json.dumps({
        'spec': spec, 'approval': approval, 'reservation': reservation,
        'approval_sha256': reservation['approval_sha256']}, ensure_ascii=False), capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ticket_id'] == ticket['ticket_id']
    with pytest.raises(HubError):
        issue(service, job, spec)
    assert len(store.query('SELECT * FROM publications')) == 1


@pytest.mark.parametrize('field', ['metadata_sha256', 'binding_sha256', 'metadata_revision_id',
    'upload_reference_sha256', 'attempt_id', 'target_site_uid', 'file', 'file_bytes'])
def test_changed_recovered_binding_cannot_approve_or_reserve(short_video, field):
    _, store, service, _, _, _ = short_video
    job, spec = revised(short_video)
    spec[field] = spec[field]+1 if field == 'file_bytes' else str(uuid.uuid4()) if field.endswith('_id') else 'changed'
    with pytest.raises(HubError):
        issue(service, job, spec)
    assert store.get(job)['result'][STATE]['public_submission_approved'] is False
    assert not store.query('SELECT * FROM publications')


def test_missing_actual_owner_response_is_not_filled_or_reservable(short_video):
    _, store, service, _, _, _ = short_video
    job, spec = revised(short_video)
    with pytest.raises(HubError):
        issue_recovered_submit_ticket(service, job, spec, principal=PRINCIPAL, actual_owner_response='', approval_reference='Owned fixture')
    assert not store.query('SELECT * FROM publications')
    assert store.get(job)['result'][STATE]['public_submission_approved'] is False


def test_existing_global_intent_preserves_unknown_and_blocks_new_reservation(short_video):
    settings, store, service, _, _, _ = short_video
    job, spec = revised(short_video)
    intent = settings.root/'media'/job/'recovered-submit-intent.json'
    intent.parent.mkdir(parents=True, exist_ok=True);intent.write_text('{"state":"unknown"}', encoding='utf-8')
    before = intent.read_bytes()
    with pytest.raises(HubError):
        issue(service, job, spec)
    assert intent.read_bytes() == before and not store.query('SELECT * FROM publications')


def test_readonly_principal_cannot_issue_approval_or_submit_reservation(short_video):
    _, store, service, _, _, _ = short_video
    job, spec = revised(short_video)
    with pytest.raises(HubError, match='ACCESS_DENIED'):
        issue_recovered_submit_ticket(service, job, spec, principal='reader', actual_owner_response='Owned fixture',
                                      approval_reference='Owned fixture')
    assert not store.query('SELECT * FROM publications')
    assert store.get(job)['result'][STATE]['public_submission_approved'] is False
