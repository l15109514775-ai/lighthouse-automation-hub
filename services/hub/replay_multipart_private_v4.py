"""Explicit two-part private probe, separate from acquisition and publication.

The probe is a maintenance job. It cannot reserve a public publication, edit
an existing BV, or be claimed by the registration-only V4 runtime.
"""
from copy import deepcopy
from datetime import datetime, timezone
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import threading
import time
import uuid

from adapters.media_tools import inspect_media
from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .locks import resource_guard
from .outcomes import JobOutcome
from .replay_video_browser import ReplayBrowserSession, ReplayVideoBrowser
from .youtube_download import CHANNELS, digest

JOB_TYPE = 'replay_multipart_private_probe_v4'
POLICY = 'replay-multipart-private-probe-v4'
STATE = 'multipart_private_v4'
MANIFEST = 'multipart-private-native-manifest-v4'
UID = '1000000000000000'
SHA = re.compile('[a-f0-9]{64}')


def fail(code):
    raise HubError(code, review=True)


def safe(file):
    file = Path(file)
    if not file.is_absolute() or file.resolve() != file or any(p.is_symlink() for p in (file, *file.parents)):
        fail('MULTIPART_PRIVATE_PATH_INVALID')
    return file


def read_json(file, maximum=131072):
    file = safe(file)
    try:
        with file.open('rb') as stream:
            before = os.fstat(stream.fileno())
            if not 0 < before.st_size <= maximum:
                fail('MULTIPART_PRIVATE_EVIDENCE_SIZE_INVALID')
            raw = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
        if len(raw) != before.st_size or any(getattr(before, k) != getattr(after, k) for k in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')):
            fail('MULTIPART_PRIVATE_EVIDENCE_CHANGED')
        return json.loads(raw), {'file': str(file), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
    except (OSError, ValueError, TypeError):
        fail('MULTIPART_PRIVATE_EVIDENCE_INVALID')


def metadata(source_id):
    url = 'https://www.youtube.com/watch?v=' + source_id
    return {'title': '【多P私有验收】示例主播A 两段真实片源样本',
        'description': '多P私有流程验收，不公开投稿。\n两段样本按关键帧切分，实际时长以逐段核验为准。\n原视频：' + url,
        'copyright': 2, 'source': url, 'category_label': '娱乐',
        'tags': ['示例主播A', '虚拟UP主', '直播回放', '流程验收']}


class ProbeQueueView:
    """One explicitly requested probe; never consume other video jobs."""
    def __init__(self, store, job_id):
        self.store, self.job_id = store, job_id

    def __getattr__(self, name):
        return getattr(self.store, name)

    @staticmethod
    def _queue(queue):
        if queue != 'video':
            raise ValueError('MULTIPART_PRIVATE_QUEUE_SCOPE')

    def beat(self, name):
        if name != 'worker:video':
            raise ValueError('MULTIPART_PRIVATE_HEARTBEAT_SCOPE')
        self.store.beat(name)
        self.store.beat('worker:video-private-probe-v4')

    def _scope(self):
        return ('job_id=? AND policy_version=? AND source_platform=? AND job_type=?',
                (self.job_id, POLICY, 'maintenance', JOB_TYPE))

    def claim(self, queue, lease_seconds, max_attempts, *, job_types=None):
        self._queue(queue)
        from .job_scope import job_type_filter
        scope, types = job_type_filter(job_types)
        now, owner = time.time(), str(uuid.uuid4())
        with self.store.transaction() as db:
            if db.execute("SELECT paused FROM pipelines WHERE name='video'").fetchone()['paused']:
                return None
            row = db.execute(f'SELECT * FROM jobs WHERE job_id=? AND {scope}', (self.job_id, *types)).fetchone()
            if (not row or row['job_type'] != JOB_TYPE or row['policy_version'] != POLICY or row['source_platform'] != 'maintenance' or
                    row['queue'] != 'video' or row['state'] != 'upload_pending' or row['attempts'] != 0 or
                    row['lease_owner'] is not None or row['lease_until'] is not None or json.loads(row['result'])[STATE]['state'] != 'prepared'):
                return None
            if not db.execute("SELECT 1 FROM audit WHERE action='multipart_private_run_requested' AND target=?", (self.job_id,)).fetchone():
                return None
            db.execute("UPDATE jobs SET state='processing',attempts=1,lease_owner=?,lease_until=?,updated_at=? WHERE job_id=?",
                (owner, now + lease_seconds, now, self.job_id))
        return self.store.get(self.job_id)

    def recover(self, queue, max_attempts, *, all_processing=False, job_types=None):
        self._queue(queue)
        from .job_scope import job_type_filter
        from .locks import require_queue_lock
        scope, types = job_type_filter(job_types)
        if all_processing:
            require_queue_lock(self.store, queue)
        with self.store.transaction() as db:
            row = db.execute(f'SELECT * FROM jobs WHERE job_id=? AND {scope}', (self.job_id, *types)).fetchone()
            if (not row or row['job_type'] != JOB_TYPE or row['policy_version'] != POLICY or row['source_platform'] != 'maintenance' or
                    row['queue'] != 'video' or row['state'] != 'processing' or not (all_processing or (row['lease_until'] or 0) < time.time())):
                return 0
            result = json.loads(row['result'])
            state = result[STATE]
            state['state'] = 'save_unknown' if state['state'] == 'uploaded' else 'upload_unknown'
            db.execute("UPDATE jobs SET state='upload_pending',result=?,error_code='MULTIPART_PRIVATE_RECONCILE_REQUIRED',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE job_id=?",
                (encode(result), time.time(), self.job_id))
        return 1


class PrivateSession(ReplayBrowserSession):
    """Reuse the bounded ordinary transport, changing only its private child."""
    def __init__(self, settings, manifest, *, assert_lease, process_factory=subprocess.Popen):
        self.settings, self.manifest, self.assert_lease = settings, manifest, assert_lease
        self.messages, self.closed = queue.Queue(), False
        self._owned, self._tracking_stop = {}, threading.Event()
        script = safe(settings.root / 'adapters/browser_multipart_private_v4.mjs')
        if not script.is_file():
            fail('MULTIPART_PRIVATE_SOURCE_REQUIRED')
        env = {name: os.environ[name] for name in ('PATH', 'LANG', 'LC_ALL', 'HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME') if name in os.environ}
        env.update(HUB_ROOT=str(settings.root), HUB_BROWSER_CHANNEL='chromium', BILIBILI_EXPECTED_SITE_UID=manifest['target_site_uid'],
            PLAYWRIGHT_BROWSERS_PATH=str(settings.root / 'compositor/runtime/browsers'))
        self.process = process_factory(['node', str(script)], cwd=settings.root, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1,
            start_new_session=os.name == 'posix')
        self._remember()
        self._tracker = threading.Thread(target=self._track, daemon=True)
        self._tracker.start()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self._send(manifest)
            ready = self._receive(75)
            self._remember()
            if ready.get('state') != 'ready' or ready.get('job_id') != manifest['job_id'] or ready.get('attempt_id') != manifest['attempt_id']:
                fail(self._safe_code(ready.get('error_code')))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _processes():
        rows = {}
        if os.name != 'posix' or not Path('/proc').is_dir():
            return rows
        for file in Path('/proc').glob('[0-9]*/stat'):
            try:
                raw = file.read_text()
                fields = raw[raw.rfind(')') + 2:].split()
                rows[int(file.parent.name)] = (int(fields[1]), int(fields[19]), fields[0])
            except (OSError, ValueError, IndexError):
                pass
        return rows

    def _remember(self):
        rows = self._processes()
        if self.process.pid in rows and self.process.pid not in self._owned and self.process.poll() is None:
            self._owned[self.process.pid] = rows[self.process.pid][1]
        changed = True
        while changed:
            changed = False
            for pid, (parent, started, _) in rows.items():
                if (self._owned.get(pid) != started and parent in self._owned and parent in rows and
                        rows[parent][1] == self._owned[parent]):
                    self._owned[pid] = started
                    changed = True

    def _track(self):
        while not self._tracking_stop.wait(.1):
            self._remember()

    def _live_owned(self):
        self._remember()
        return [pid for pid, row in self._processes().items()
                if self._owned.get(pid) == row[1] and row[2] != 'Z']

    def close(self):
        if self.closed:
            return
        self._remember()
        try:
            super().close()  # stdin end / bounded Node SIGTERM handler closes its context
        finally:
            # Chromium can have a detached process group. Kill only captured
            # descendants whose /proc start tick still matches this Node run.
            # Never kill the video worker, an arbitrary profile owner or a PG.
            for sig in (signal.SIGTERM, signal.SIGKILL) if os.name == 'posix' else ():
                for pid in self._live_owned():
                    try:
                        os.kill(pid, sig)
                    except ProcessLookupError:
                        pass
                until = time.monotonic() + 3
                while self._live_owned() and time.monotonic() < until:
                    time.sleep(.05)
            self._tracking_stop.set()
            self._tracker.join(timeout=1)
            remaining = self._live_owned()
            folder = self.settings.root / 'media' / self.manifest['job_id'] / 'multipart-private-v4' / self.manifest['attempt_id']
            if folder.is_dir() and not folder.is_symlink():
                from .media_processing import atomic_json
                atomic_json(folder / ('owned-process-cleanup-' + str(uuid.uuid4()) + '.json'), {
                    'version': 'multipart-private-owned-process-cleanup-v4', 'recorded_at': time.time(),
                    'job_id': self.manifest['job_id'], 'attempt_id': self.manifest['attempt_id'],
                    'captured_processes': len(self._owned), 'remaining_owned_live_processes': len(remaining),
                    'broad_profile_or_worker_kill': False})
            if remaining:
                fail('MULTIPART_PRIVATE_OWNED_BROWSER_CLEANUP_REQUIRED')

    def _operation(self, operation, **arguments):
        if operation not in ('private_upload', 'save', 'inspect') or arguments or self.closed:
            fail('MULTIPART_PRIVATE_OPERATION_INVALID')
        ident = str(uuid.uuid4())
        self._send({'version': 'replay-native-browser-request-v1', 'request_id': ident, 'operation': operation, 'arguments': {}})
        value = self._receive({'private_upload': 900, 'save': 180, 'inspect': 150}[operation])
        if value.get('request_id') != ident or value.get('operation') != operation or value.get('ok') is not True or not isinstance(value.get('result'), dict):
            fail(self._safe_code(value.get('error_code')))
        return value['result']


class MultipartPrivateProbeV4:
    def __init__(self, settings, store, *, browser=None):
        self.settings, self.store = settings, store
        self.browser = browser or ReplayVideoBrowser(settings, session_factory=PrivateSession)

    def prepare(self, samples_file, samples_sha256, *, owner_reference):
        if (self.settings.config.get('target_bilibili_site_uid') != UID or not SHA.fullmatch(samples_sha256 or '') or
                not isinstance(owner_reference, str) or not 0 < len(owner_reference.strip()) <= 2048 or
                owner_reference.startswith(('REPLACE', 'UNCONFIGURED'))):
            fail('MULTIPART_PRIVATE_OWNER_SCOPE_REQUIRED')
        samples_file = safe(samples_file)
        folder = samples_file.parent
        if (folder.parent != self.settings.root / 'media' or not re.fullmatch(r'multipart-v4-native-real-samples-[a-f0-9-]{36}', folder.name) or
                samples_file.name != 'samples.json'):
            fail('MULTIPART_PRIVATE_SAMPLE_PATH_INVALID')
        body, reference = read_json(samples_file)
        if (reference['sha256'] != samples_sha256 or body.get('version') != 'real-source-private-multipart-samples-v4' or
                body.get('private_upload_only') is not True or body.get('public_submission_allowed') is not False or
                body.get('formal_splitter_acceptance_claimed') is not False or body.get('method') != 'ffmpeg_stream_copy' or
                not re.fullmatch('[A-Za-z0-9_-]{11}', body.get('source_id', '')) or len(body.get('parts', [])) != 2):
            fail('MULTIPART_PRIVATE_SAMPLE_BINDING_INVALID')
        source_id = body['source_id']
        sources = []
        for row in self.store.query("SELECT job_id FROM jobs WHERE source_platform='youtube' AND source_id=? AND state='completed'", (source_id,)):
            job = self.store.get(row['job_id'])
            for state_key in ('youtube_download_v2', 'youtube_download_v3', 'youtube_download_v4'):
                state = job['result'].get(state_key, {})
                asset = state.get('source_asset', {})
                if (state.get('state') in ('verified', 'parts_verified') and asset.get('file') == body['source_path'] and
                        asset.get('sha256') == body['source_sha256'] and asset.get('bytes') == body['source_bytes']):
                    sources.append(job)
        if len(sources) != 1 or sources[0]['payload'].get('channel_id') not in CHANNELS:
            fail('MULTIPART_PRIVATE_VERIFIED_SOURCE_REQUIRED')
        parts = []
        for index, item in enumerate(body['parts'], 1):
            file = safe(item['path'])
            duration = float(item['probe']['format']['duration'])
            if (file != folder / f'part-{index:03d}.mp4' or type(item['bytes']) is not int or not 0 < item['bytes'] <= 100_000_000 or
                    not SHA.fullmatch(item.get('sha256', '')) or not math.isfinite(duration) or not 0 < duration <= 120 or
                    file.stat().st_size != item['bytes'] or sha256_file(file) != item['sha256']):
                fail('MULTIPART_PRIVATE_PART_CHANGED')
            actual = inspect_media(file, include_frame_rates=True)
            if (abs(actual['duration'] - duration) > .05 or actual['stream_config']['video']['codec_name'] != 'h264' or
                    actual['stream_config']['audio']['codec_name'] != 'aac'):
                fail('MULTIPART_PRIVATE_PART_PROBE_CHANGED')
            parts.append({'index': index, 'file': str(file), 'filename': file.name, 'bytes': item['bytes'],
                'sha256': item['sha256'], 'duration_seconds': duration})
        meta = metadata(source_id)
        payload = {'version': MANIFEST, 'target_site_uid': UID, 'source_id': source_id,
            'source_url': 'https://www.youtube.com/watch?v=' + source_id, 'source_job_id': sources[0]['job_id'],
            'sample_manifest': reference, 'parts': parts, 'parts_manifest_sha256': digest(parts),
            'metadata': meta, 'metadata_sha256': digest(meta), 'owner_reference': owner_reference,
            'human_review': False, 'publication_authorized': False, 'native_multipart_verified': False}
        ident = self.store.enqueue('maintenance', 'multipart-private:' + reference['sha256'], JOB_TYPE, 'video', payload, POLICY)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE job_id=?', (ident,)).fetchone()
            if json.loads(row['payload']) != payload:
                fail('MULTIPART_PRIVATE_EXISTING_BINDING_CHANGED')
            if not json.loads(row['result']):
                state = {'state': 'prepared', 'binding_sha256': digest(payload), 'attempt_id': str(uuid.uuid4()),
                    'human_review': False, 'publication_authorized': False, 'native_multipart_verified': False}
                db.execute("UPDATE jobs SET state='upload_pending',stage='multipart_private_prepared',result=? WHERE job_id=? AND attempts=0 AND state='pending'",
                    (encode({STATE: state}), ident))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (time.time(), 'operator:multipart-private-v4', 'multipart_private_prepare', ident,
                     encode({'owner_reference': owner_reference, 'private_only': True, 'formal_splitter_acceptance_claimed': False})))
        return ident

    def manifest(self, job):
        return {**{k: deepcopy(v) for k, v in job['payload'].items() if k not in ('source_job_id', 'owner_reference')},
                'job_id': job['job_id'], 'attempt_id': job['result'][STATE]['attempt_id'],
                'binding_sha256': digest(job['payload'])}

    def assert_lease(self, job):
        current = self.store.get(job['job_id'])
        if (not current or current['job_type'] != JOB_TYPE or current['source_platform'] != 'maintenance' or
                current['queue'] != 'video' or current['policy_version'] != POLICY or current['state'] != 'processing' or
                not job.get('lease_owner') or current['lease_owner'] != job['lease_owner'] or
                (current['lease_until'] or 0) <= time.time() or current['payload'] != job['payload'] or
                current['attempts'] != job['attempts'] or current['result'][STATE]['binding_sha256'] != digest(job['payload'])):
            fail('MULTIPART_PRIVATE_LEASE_CHANGED')
        if self.store.query('SELECT 1 FROM publications WHERE job_id=?', (job['job_id'],)):
            fail('MULTIPART_PRIVATE_PUBLICATION_FORBIDDEN')
        body, reference = read_json(job['payload']['sample_manifest']['file'])
        if reference != job['payload']['sample_manifest']:
            fail('MULTIPART_PRIVATE_SAMPLE_CHANGED')
        return current

    def receipt(self, spec, result, kind):
        key = {'upload': 'upload_receipt', 'draft': 'draft_receipt', 'observation': 'observation'}[kind]
        expected_state = {'upload': 'private_parts_uploaded', 'draft': 'private_draft_saved', 'observation': 'private_parts_observed'}[kind]
        body = result.get(key)
        ref = result.get('receipt_file')
        output = self.settings.root / 'media' / spec['job_id'] / 'multipart-private-v4' / spec['attempt_id']
        if (result.get('state') != expected_state or not isinstance(body, dict) or not isinstance(ref, dict) or
                set(ref) != {'file', 'bytes', 'sha256'} or Path(ref['file']).parent != output):
            fail('MULTIPART_PRIVATE_RECEIPT_INVALID')
        raw_body, actual_ref = read_json(ref['file'], 262144)
        if actual_ref != ref or raw_body != body:
            fail('MULTIPART_PRIVATE_RECEIPT_CHANGED')
        expected = {'version': 'multipart-private-' + kind + '-v4', 'job_id': spec['job_id'], 'attempt_id': spec['attempt_id'],
            'site_uid': spec['target_site_uid'], 'source_id': spec['source_id'], 'source_url': spec['source_url'],
            'binding_sha256': spec['binding_sha256'], 'metadata_sha256': spec['metadata_sha256'],
            'parts_manifest_sha256': spec['parts_manifest_sha256'], 'human_review': False, 'publication_authorized': False,
            'native_multipart_verified': False, 'server_file_hash_verified': False}
        if any(body.get(k) != v or type(body.get(k)) is not type(v) for k, v in expected.items()):
            fail('MULTIPART_PRIVATE_RECEIPT_BINDING_CHANGED')
        try:
            observed = datetime.fromisoformat(body['observed_at'].replace('Z', '+00:00'))
            if observed.tzinfo is None or not -30 <= time.time() - observed.timestamp() <= 300:
                raise ValueError()
            ordered = body['ordered_parts']
            if len(ordered) != 2:
                raise ValueError()
            for native, local in zip(ordered, spec['parts']):
                if (any(native.get(k) != local[k] for k in ('index', 'filename', 'bytes', 'sha256', 'duration_seconds')) or
                        native.get('native_filename') not in (local['filename'], Path(local['filename']).stem) or
                        native.get('native_upload_complete') is not True or native.get('whole_file_streamed') is not True or
                        native.get('streamed_bytes') != local['bytes'] or native.get('streamed_sha256') != local['sha256']):
                    raise ValueError()
            if kind == 'draft' or kind == 'observation' and (body.get('metadata_verified') is True or body.get('recovered_without_modification') is True):
                meta = spec['metadata']
                wanted = {'title': meta['title'], 'description': meta['description'], 'creation_statement': '内容为转载',
                    'source': meta['source'], 'category': meta['category_label'], 'tags': meta['tags']}
                if ((kind == 'draft' and body.get('private_draft_saved') is not True) or body.get('metadata_verified') is not True or
                        body.get('metadata_readback') != wanted):
                    raise ValueError()
            if kind == 'draft':
                screenshot = body['screenshot']
                image = safe(screenshot['file'])
                if (set(screenshot) != {'file', 'bytes', 'sha256'} or image != output / 'private-draft.png' or
                        type(screenshot['bytes']) is not int or not 33 <= screenshot['bytes'] <= 5_000_000 or
                        image.stat().st_size != screenshot['bytes']):
                    raise ValueError()
                with image.open('rb') as stream:
                    picture = stream.read(screenshot['bytes'] + 1)
                if (len(picture) != screenshot['bytes'] or not picture.startswith(b'\x89PNG\r\n\x1a\n') or
                        hashlib.sha256(picture).hexdigest() != screenshot['sha256']):
                    raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            fail('MULTIPART_PRIVATE_NATIVE_COVERAGE_INVALID')
        return {**body, 'evidence': ref}

    def process(self, job):
        current = self.assert_lease(job)
        state = current['result'][STATE]
        if state['state'] != 'prepared':
            fail('MULTIPART_PRIVATE_RECONCILE_REQUIRED')
        spec = self.manifest(current)
        state.update(state='uploading', started_at=time.time())
        self.store.checkpoint(job, 'multipart_private_uploading', {**current['result'], STATE: state})
        try:
            with resource_guard(self.settings.root / 'data/locks/moderation'):
                with self.browser.session(spec, assert_lease=lambda: self.assert_lease(job)) as session:
                    uploaded = self.receipt(spec, session.operation('private_upload'), 'upload')
                    self.assert_lease(job)
                    state.update(state='uploaded', upload_receipt=uploaded)
                    self.store.checkpoint(job, 'multipart_private_uploaded', {**job['result'], STATE: state})
                    saved = self.receipt(spec, session.operation('save'), 'draft')
                    self.assert_lease(job)
                    state.update(state='private_draft_saved', draft_receipt=saved)
                    self.store.checkpoint(job, 'multipart_private_draft_saved', {**job['result'], STATE: state})
                    return JobOutcome('completed', job['result'])
        except Exception as exc:
            current = self.store.get(job['job_id'])
            if current and current['state'] == 'processing' and current['lease_owner'] == job.get('lease_owner'):
                state = current['result'][STATE]
                code = exc.code if isinstance(exc, HubError) else 'MULTIPART_PRIVATE_RESULT_UNKNOWN'
                state.update(state='save_unknown' if state['state'] == 'uploaded' else 'upload_unknown', error_code=code)
                self.store.checkpoint(job, 'multipart_private_reconcile_required', {**current['result'], STATE: state})
                return JobOutcome('upload_pending', job['result'], error_code=code)
            raise

    def request_run(self, job_id):
        """Make only an unattempted prepared probe pending; never reset retries."""
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
            if (not row or row['job_type'] != JOB_TYPE or row['policy_version'] != POLICY or row['source_platform'] != 'maintenance' or
                    row['state'] != 'upload_pending' or row['attempts'] != 0 or row['lease_owner'] is not None or row['lease_until'] is not None or
                    json.loads(row['result'])[STATE]['state'] != 'prepared'):
                fail('MULTIPART_PRIVATE_RECONCILE_REQUIRED')
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (time.time(), 'operator:multipart-private-v4', 'multipart_private_run_requested', job_id, 'private_only_no_publication'))

    def inspect(self, job_id):
        """A bounded read-only browser observation; no claim/retry or save."""
        with resource_guard(self.settings.root / 'data/locks/video', self.settings.root / 'data/locks/moderation'):
            before = self.store.get(job_id)
            if (not before or before['job_type'] != JOB_TYPE or before['policy_version'] != POLICY or
                    before['source_platform'] != 'maintenance' or before['lease_owner'] is not None or before['lease_until'] is not None or
                    before['result'][STATE]['state'] not in ('uploaded', 'upload_unknown', 'save_unknown', 'private_draft_saved')):
                fail('MULTIPART_PRIVATE_INSPECTION_SCOPE_INVALID')
            def fence():
                current = self.store.get(job_id)
                if current != before or self.store.query('SELECT 1 FROM publications WHERE job_id=?', (job_id,)):
                    fail('MULTIPART_PRIVATE_INSPECTION_BINDING_CHANGED')
            spec = self.manifest(before)
            with self.browser.session(spec, assert_lease=fence) as session:
                observed = self.receipt(spec, session.operation('inspect'), 'observation')
            fence()
            with self.store.transaction() as db:
                row = db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
                current = dict(row)
                for key in ('payload', 'result'):
                    current[key] = json.loads(current[key])
                if current != before:
                    fail('MULTIPART_PRIVATE_INSPECTION_BINDING_CHANGED')
                result = deepcopy(before['result'])
                result[STATE].setdefault('inspections', []).append(observed)
                db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?', (encode(result), time.time(), job_id))
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (time.time(), 'operator:multipart-private-v4', 'multipart_private_inspected', job_id,
                     encode({'evidence': observed['evidence'], 'no_retry': True, 'no_file_selection': True, 'no_save': True})))
            return observed


def main():
    from .config import Settings
    from .db import Store
    from .worker import Worker
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples')
    parser.add_argument('--samples-sha256')
    parser.add_argument('--owner-reference')
    parser.add_argument('--run')
    parser.add_argument('--inspect')
    args = parser.parse_args()
    if args.run and args.inspect or not (args.run or args.inspect) and not all((args.samples, args.samples_sha256, args.owner_reference)):
        parser.error('Use --run JOB, --inspect JOB, or all of --samples/--samples-sha256/--owner-reference.')
    if os.name != 'posix' or os.getuid() != 10001:
        fail('MULTIPART_PRIVATE_APP_UID_REQUIRED')
    settings = Settings.load()
    store = Store(settings.db_path)
    service = MultipartPrivateProbeV4(settings, store)
    if args.inspect:
        observed = service.inspect(args.inspect)
        print(json.dumps({'job_id': args.inspect, 'state': 'private_parts_observed', 'receipt': observed['evidence'],
            'publication_authorized': False, 'native_multipart_verified': False}))
    elif args.run:
        service.request_run(args.run)
        worker = Worker(settings, ProbeQueueView(store, args.run), {JOB_TYPE: service.process})
        worked = worker.once('video')
        current = store.get(args.run)
        print(json.dumps({'job_id': args.run, 'worked': worked, 'job_state': current['state'],
            'private_stage': current['result'][STATE]['state'], 'publication_authorized': False, 'native_multipart_verified': False}))
    else:
        ident = service.prepare(args.samples, args.samples_sha256, owner_reference=args.owner_reference)
        print(json.dumps({'job_id': ident, 'state': 'prepared', 'publications': 0, 'browser_actions': 0}))


if __name__ == '__main__':
    main()
