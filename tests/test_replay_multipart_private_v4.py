from pathlib import Path as _RecoveryPath
from services.hub.locks import exclusive as _recovery_lock
"""Real files/probes/SQLite; identified native transport doubles, no platform."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

import pytest

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.replay_multipart_private_v4 import (MultipartPrivateProbeV4, ProbeQueueView,
    JOB_TYPE, POLICY, STATE, UID, read_json)
from services.hub.worker import Worker
from test_ytdlp_highest_v2 import tracks


def sha(file):
    return hashlib.sha256(file.read_bytes()).hexdigest()


@pytest.fixture
def private(runtime, tracks):
    s, db = runtime
    s.config['target_bilibili_site_uid'] = UID
    folder = s.root / 'media' / ('multipart-v4-native-real-samples-' + str(uuid.uuid4()))
    folder.mkdir(parents=True)
    source_id = 'abcdefghijk'
    ident = db.enqueue('youtube', source_id, 'youtube_download_v2', 'video',
        {'channel_id': 'UCaaaaaaaaaaaaaaaaaaaaaa'}, 'Owned download boundary')
    job = db.claim('video', 180, 4)
    part = folder / 'part-001.mp4'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(tracks[0]), '-i', str(tracks[1]),
        '-map', '0:v:0', '-map', '1:a:0', '-c', 'copy', str(part)], check=True)
    shutil.copyfile(part, folder / 'part-002.mp4')
    source = s.root / 'media' / ident / 'youtube-highest-v2/original.mp4'
    source.parent.mkdir(parents=True)
    shutil.copyfile(part, source)
    asset = {'file': str(source), 'bytes': source.stat().st_size, 'sha256': sha(source)}
    db.finish(job, 'completed', result={'youtube_download_v2': {'state': 'verified', 'source_asset': asset}})
    parts = []
    for index in (1, 2):
        p = folder / f'part-{index:03d}.mp4'
        probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(p)]))
        parts.append({'path': str(p), 'bytes': p.stat().st_size, 'sha256': sha(p), 'probe': probe,
            'requested_seconds': .5, 'source_seek_seconds': index})
    body = {'version': 'real-source-private-multipart-samples-v4', 'source_id': source_id,
        'source_path': str(source), 'source_bytes': asset['bytes'], 'source_sha256': asset['sha256'], 'parts': parts,
        'method': 'ffmpeg_stream_copy', 'private_upload_only': True, 'public_submission_allowed': False,
        'formal_splitter_acceptance_claimed': False}
    file = folder / 'samples.json'
    file.write_text(encode(body), encoding='utf-8')
    service = MultipartPrivateProbeV4(s, db)
    return {'s': s, 'db': db, 'service': service, 'file': file, 'body': body, 'source': ident}


def prepare(f):
    return f['service'].prepare(f['file'], sha(f['file']), owner_reference='Owned synthetic owner instruction, not real approval')


def test_prepare_checks_real_mp4_duration_and_holds_one_idempotent_private_job(private):
    f = private
    ident = prepare(f)
    assert prepare(f) == ident
    job = f['db'].get(ident)
    assert job['state'] == 'upload_pending' and job['attempts'] == 0 and job['source_platform'] == 'maintenance'
    assert len(job['payload']['parts']) == 2
    assert job['payload']['parts'][0]['duration_seconds'] != f['body']['parts'][0]['requested_seconds']
    assert job['payload']['publication_authorized'] is job['payload']['native_multipart_verified'] is False
    assert not f['db'].query('SELECT * FROM publications')
    # The ordinary pre-existing video worker cannot see this held probe.
    assert f['db'].claim('video', 60, 4) is None


@pytest.mark.parametrize('change', ['part_bytes', 'part_duration', 'allow_public', 'wrong_uid'])
def test_changed_part_or_public_scope_cannot_create_a_probe(private, change):
    f = private
    body = deepcopy(f['body'])
    if change == 'part_bytes':
        Path(body['parts'][0]['path']).write_bytes(b'not video')
    elif change == 'part_duration':
        body['parts'][0]['probe']['format']['duration'] = '119'
    elif change == 'allow_public':
        body['public_submission_allowed'] = True
    else:
        f['s'].config['target_bilibili_site_uid'] = '123'
    f['file'].write_text(encode(body), encoding='utf-8')
    with pytest.raises(HubError):
        prepare(f)
    assert not f['db'].query('SELECT * FROM jobs WHERE job_type=?', (JOB_TYPE,))


def test_one_requested_probe_claims_held_directly_and_does_not_steal_legacy_or_active_lease(private):
    f = private
    ident = prepare(f)
    old = f['db'].enqueue('youtube', 'Owned-old', 'replay_highest_video_publish_v2', 'video', {}, 'Owned legacy')
    view = ProbeQueueView(f['db'], ident)
    assert view.claim('video', 60, 4) is None
    f['service'].request_run(ident)
    assert f['db'].get(ident)['state'] == 'upload_pending'
    claimed = view.claim('video', 60, 4)
    assert claimed['job_id'] == ident and claimed['attempts'] == 1
    assert f['db'].get(old)['state'] == 'pending' and f['db'].get(old)['attempts'] == 0
    assert view.claim('video', 60, 4) is None
    with pytest.raises(HubError, match='RECONCILE_REQUIRED'):
        f['service'].request_run(ident)


def test_interrupted_probe_stays_unknown_with_same_attempt_and_cannot_reselect(private):
    f = private
    ident = prepare(f)
    f['service'].request_run(ident)
    view = ProbeQueueView(f['db'], ident)
    claimed = view.claim('video', 60, 4)
    before = claimed['payload']
    result = claimed['result']
    result[STATE]['state'] = 'uploading'
    f['db'].checkpoint(claimed, 'Owned native intent may exist', result)
    with _recovery_lock(_RecoveryPath(view.path).parent / "locks" / 'video'):
        assert view.recover('video', 4, all_processing=True) == 1
    current = f['db'].get(ident)
    assert current['state'] == 'upload_pending' and current['attempts'] == 1
    assert current['result'][STATE]['state'] == 'upload_unknown' and current['payload'] == before
    assert view.claim('video', 60, 4) is None
    with pytest.raises(HubError):
        f['service'].request_run(ident)


class OwnedNative:
    def __init__(self, f, *, unknown=False, wrong_order=False):
        self.f, self.calls, self.unknown, self.wrong_order = f, [], unknown, wrong_order

    @contextmanager
    def session(self, spec, *, assert_lease):
        f = self.f
        def operation(action):
            assert_lease()
            self.calls.append(action)
            if self.unknown:
                raise HubError('OWNED_NATIVE_RESPONSE_UNKNOWN')
            kind = {'private_upload': 'upload', 'save': 'draft', 'inspect': 'observation'}[action]
            ordered = [{**{k: p[k] for k in ('index', 'filename', 'bytes', 'sha256', 'duration_seconds')},
                'native_filename': p['filename'], 'native_upload_complete': True, 'whole_file_streamed': True,
                'streamed_bytes': p['bytes'], 'streamed_sha256': p['sha256']} for p in spec['parts']]
            if self.wrong_order:
                ordered.reverse()
            body = {'version': 'multipart-private-' + kind + '-v4', 'job_id': spec['job_id'], 'attempt_id': spec['attempt_id'],
                'site_uid': spec['target_site_uid'], 'source_id': spec['source_id'], 'source_url': spec['source_url'],
                'binding_sha256': spec['binding_sha256'], 'metadata_sha256': spec['metadata_sha256'],
                'parts_manifest_sha256': spec['parts_manifest_sha256'], 'ordered_parts': ordered,
                'observed_at': datetime.now(timezone.utc).isoformat(), 'human_review': False, 'publication_authorized': False,
                'native_multipart_verified': False, 'server_file_hash_verified': False}
            if kind == 'draft':
                m = spec['metadata']
                body.update(private_draft_saved=True, metadata_verified=True, metadata_readback={'title': m['title'],
                    'description': m['description'], 'creation_statement': '内容为转载', 'source': m['source'], 'category': m['category_label'], 'tags': m['tags']})
            folder = f['s'].root / 'media' / spec['job_id'] / 'multipart-private-v4' / spec['attempt_id']
            folder.mkdir(parents=True, exist_ok=True)
            if kind == 'draft':
                image = folder / 'private-draft.png'
                subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', spec['parts'][0]['file'],
                    '-frames:v', '1', str(image)], check=True)
                body['screenshot'] = {'file': str(image), 'bytes': image.stat().st_size, 'sha256': sha(image)}
            file = folder / (kind + '-receipt.json')
            file.write_text(encode(body), encoding='utf-8')
            return {'state': {'upload': 'private_parts_uploaded', 'draft': 'private_draft_saved', 'observation': 'private_parts_observed'}[kind],
                {'upload': 'upload_receipt', 'draft': 'draft_receipt', 'observation': 'observation'}[kind]: body,
                'receipt_file': {'file': str(file), 'bytes': file.stat().st_size, 'sha256': sha(file)}}
        yield type('OwnedSession', (), {'operation': staticmethod(operation)})()


def run(f, native):
    ident = prepare(f)
    service = MultipartPrivateProbeV4(f['s'], f['db'], browser=native)
    service.request_run(ident)
    assert Worker(f['s'], ProbeQueueView(f['db'], ident), {JOB_TYPE: service.process}).once('video')
    return ident, f['db'].get(ident)


def test_actual_worker_checkpoints_both_bound_private_receipts_without_publication(private):
    f = private
    native = OwnedNative(f)
    ident, current = run(f, native)
    assert current['state'] == 'completed' and current['attempts'] == 1
    assert current['result'][STATE]['state'] == 'private_draft_saved' and current['external_publish_id'] is None
    assert native.calls == ['private_upload', 'save']
    assert not f['db'].query('SELECT * FROM publications')
    assert f['db'].get(f['source'])['state'] == 'completed'


@pytest.mark.parametrize('change', ['unknown', 'wrong_order'])
def test_unknown_or_reversed_native_result_is_held_and_never_blindly_runs_again(private, change):
    f = private
    native = OwnedNative(f, unknown=change == 'unknown', wrong_order=change == 'wrong_order')
    ident, current = run(f, native)
    assert current['state'] == 'upload_pending' and current['attempts'] == 1
    assert current['result'][STATE]['state'] == 'upload_unknown'
    assert native.calls == ['private_upload']
    assert ProbeQueueView(f['db'], ident).claim('video', 60, 4) is None
    assert not f['db'].query('SELECT * FROM publications')


def test_readonly_inspection_appends_bound_evidence_without_claim_save_or_attempt_reset(private):
    f = private
    native = OwnedNative(f)
    ident, before = run(f, native)
    service = MultipartPrivateProbeV4(f['s'], f['db'], browser=native)
    observed = service.inspect(ident)
    after = f['db'].get(ident)
    assert native.calls == ['private_upload', 'save', 'inspect']
    assert after['state'] == before['state'] == 'completed' and after['attempts'] == before['attempts'] == 1
    assert after['payload'] == before['payload'] and after['lease_owner'] is None
    assert after['result'][STATE]['draft_receipt'] == before['result'][STATE]['draft_receipt']
    assert after['result'][STATE]['inspections'] == [observed]
    assert not f['db'].query('SELECT * FROM publications')
