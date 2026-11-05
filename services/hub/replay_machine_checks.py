"""Trusted, local replay checks. No network, model, approval or platform write.

Each call hashes the real current download and checks codecs and cover RGB.
Packet chains use a genuine full scan or an explicitly recorded per-job
full-scan receipt after that current full SHA. No passed flag is evidence.
Global workflow authorization and the native-account/send gate stay separate.
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
import stat
import time
import tomllib
import uuid

from adapters.media_tools import inspect_media, packet_evidence
from . import cover_normalizer_v4 as cover
from .errors import HubError
from .locks import resource_guard
from .youtube_download import CHANNELS, STATE as DOWNLOAD_STATE, VERSION as DOWNLOAD_VERSION, digest, duration_seconds

VERSION = 'verified-replay-machine-checks-v1'
REPLAY_VERSION = 'replay-video-website-v1'
REPLAY_POLICY = 'replay-video-website-policy-v1'
REPLAY_STATE = 'replay_video_web_v1'
MAX_BYTES = 3_800_000_000


def fail(code):
    raise HubError(code, review=True)


def safe(root, path):
    path = Path(path)
    if (not path.is_absolute() or path.resolve() != path or not path.is_relative_to(root) or
            any(p.is_symlink() for p in (path, *path.parents))):
        fail('REPLAY_MACHINE_PATH_REJECTED')
    return path


def fingerprint(value):
    # On Windows Python fstat ctime can describe change-time while path.stat
    # still reports birth-time. Linux production additionally binds change-time.
    keys=('st_dev','st_ino','st_size','st_mtime_ns')+(('st_ctime_ns',) if os.name=='posix' else ())
    return tuple(getattr(value,key) for key in keys)


def read_checked(root, path, *, maximum, check, collect=True):
    check()
    path = safe(root, path)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
                fail('REPLAY_MACHINE_FILE_INVALID')
            result = bytearray() if collect else None
            sha = hashlib.sha256(); total = 0;next_check=time.monotonic()+.25
            while True:
                if time.monotonic()>=next_check:
                    check();next_check=time.monotonic()+.25
                part = os.read(descriptor, 1024*1024)
                if not part: break
                total += len(part)
                if total > maximum: fail('REPLAY_MACHINE_FILE_INVALID')
                sha.update(part)
                if collect: result.extend(part)
            after = os.fstat(descriptor)
            if total != before.st_size or fingerprint(before) != fingerprint(after):
                fail('REPLAY_MACHINE_FILE_CHANGED')
        finally:
            os.close(descriptor)
        if fingerprint(path.stat()) != fingerprint(before) or path.is_symlink():
            fail('REPLAY_MACHINE_FILE_CHANGED')
        check()
        return {'file': str(path), 'bytes': total, 'sha256': sha.hexdigest()}, bytes(result) if collect else before
    except HubError:
        raise
    except OSError:
        fail('REPLAY_MACHINE_FILE_UNAVAILABLE')


def moment(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or not math.isfinite(parsed.timestamp()): raise ValueError()
    return parsed.timestamp()


class ReplayMachineChecks:
    VERSION = VERSION
    REPLAY_VERSION = REPLAY_VERSION
    REPLAY_POLICY = REPLAY_POLICY
    REPLAY_STATE = REPLAY_STATE
    JOB_TYPE = 'replay_video_publish'
    DOWNLOAD_STATE = DOWNLOAD_STATE
    DOWNLOAD_VERSION = DOWNLOAD_VERSION
    DOWNLOAD_JOB_TYPE = 'youtube_download'
    DOWNLOAD_DIRECTORY = 'youtube-download-v1'
    POLICY_FILE = 'replay-video-web-policy.toml'
    PUBLIC_MODE = 'workflow_authorization_and_machine_acceptance_v1'
    MAX_BYTES = MAX_BYTES
    MAX_SECONDS = 86400
    EVIDENCE_DIRECTORY = 'replay-machine-checks-v1'

    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    def verify(self, job, binding, state, policy, *, cover_provenance_file=None, check=None):
        check = check or (lambda: None)
        check(); root = Path(self.settings.root)
        try:
            ident = job['job_id']
            if str(uuid.UUID(ident)) != ident: raise ValueError()
            source_id = binding['download_job_id']
            if str(uuid.UUID(source_id)) != source_id: raise ValueError()
            fresh = self.store.get(ident)
            if (not fresh or fresh['job_type'] != self.JOB_TYPE or fresh['source_platform'] != 'youtube' or
                    fresh['policy_version'] != self.REPLAY_POLICY or fresh['payload'] != binding or job['payload'] != binding or
                    fresh['source_id'] != binding['source_id'] or fresh['result'].get(self.REPLAY_STATE) != state or
                    fresh['result'].get('publish_performed') is not False or
                    binding.get('version') != self.REPLAY_VERSION or binding.get('human_review') is not False or
                    state.get('version') != self.REPLAY_VERSION or state.get('human_review') is not False or
                    state.get('public_submission_approved') is not False or 'owner_acceptance' in state or
                    state.get('state') not in ('prepared', 'uploading', 'uploaded')):
                raise ValueError()
        except (KeyError, ValueError, TypeError, AttributeError):
            fail('REPLAY_MACHINE_JOB_BINDING_CHANGED')
        folder = safe(root, root/'media'/ident)
        with resource_guard(safe(root, root/'data/locks/heavy'), safe(root, folder/'replay-machine-checks.lock')):
            return self._verify(root, job, binding, state, policy, cover_provenance_file, check)

    def _verify(self, root, job, binding, state, policy, provenance_file, check):
        try:
            source = self.store.get(binding['download_job_id'])
            checkpoint = source['result'][self.DOWNLOAD_STATE]
            download_binding, asset = checkpoint['binding'], checkpoint['source_asset']
            video_id = binding['source_id']; url = 'https://www.youtube.com/watch?v='+video_id
            if (source['source_platform'] != 'youtube' or source['job_type'] != self.DOWNLOAD_JOB_TYPE or source['state'] != 'completed' or
                    source['job_id'] != binding['download_job_id'] or source['source_id'] != video_id or
                    not re.fullmatch('[A-Za-z0-9_-]{11}', video_id) or checkpoint['version'] != self.DOWNLOAD_VERSION or
                    checkpoint['state'] != 'verified' or download_binding['job_id'] != source['job_id'] or
                    download_binding['video_id'] != video_id or download_binding['source_payload_sha256'] != digest(source['payload']) or
                    download_binding['channel_id'] != binding['channel_id'] or binding['channel_id'] not in CHANNELS or
                    any(source['result'].get(k) is not False for k in ('publish_performed', 'human_review', 'publication_authorized')) or
                    source['result'].get('download_performed') is not True or
                    any(checkpoint.get(k) is not False for k in ('human_review', 'publication_authorized')) or
                    type(download_binding['duration']) not in (int,float) or not math.isfinite(download_binding['duration']) or
                    not 0 < download_binding['duration'] <= self.MAX_SECONDS or
                    type(asset['bytes']) is not int or not 0 < asset['bytes'] <= min(self.MAX_BYTES, download_binding['max_bytes']) or
                    type(asset['duration']) not in (int, float) or not math.isfinite(asset['duration']) or
                    not 0 < asset['duration'] <= self.MAX_SECONDS or abs(asset['duration']-download_binding['duration']) > 2 or
                    binding['artifact_sha256'] != asset['sha256'] or binding['file_bytes'] != asset['bytes'] or
                    binding['file'] != asset['file'] or binding['source_url'] != url or
                    sorted(asset['stream_types'].values()) != ['audio','video']):
                raise ValueError()
            expected_file = root/'media'/source['job_id']/self.DOWNLOAD_DIRECTORY/'original.mp4'
            if Path(asset['file']) != expected_file: raise ValueError()
            verified_ref, verified_body = read_checked(root, expected_file.parent/'verified.json', maximum=262144, check=check)
            verified = json.loads(verified_body)
            if (verified['version'] != self.DOWNLOAD_VERSION or verified['binding_sha256'] != digest(download_binding) or
                    verified['source_asset'] != asset or verified['receipt'] != checkpoint['receipt'] or
                    binding['download_receipt_sha256'] != verified_ref['sha256'] or
                    any(verified.get(k) is not False for k in ('human_review', 'publication_authorized', 'creator_original_bytes_verified')) or
                    verified['receipt'].get('source_to_merged_packet_coverage_verified') is not True):
                raise ValueError()
            payload = source['payload']; raw = payload['raw']; snippet = raw['snippet']; live = raw['liveStreamingDetails']
            start, end = moment(live['actualStartTime']), moment(live['actualEndTime'])
            if (payload['source_api'] != 'youtube-data-v3-official' or raw['id'] != video_id or payload['video_id'] != video_id or
                    payload['url'] != url or payload['channel_id'] != binding['channel_id'] or snippet['channelId'] != binding['channel_id'] or
                    snippet['title'] != payload['title'] or raw['status']['privacyStatus'] != 'public' or
                    raw['contentDetails']['duration'] != payload['duration'] or
                    abs(duration_seconds(payload['duration'])-download_binding['duration']) > 2 or
                    not 0 <= start <= end <= datetime.now(timezone.utc).timestamp()+30):
                raise ValueError()
        except HubError:
            raise
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            fail('REPLAY_MACHINE_VERIFIED_SOURCE_REQUIRED')
        try:
            policy_ref, policy_raw = read_checked(root, root/'config'/self.POLICY_FILE, maximum=16384, check=check)
            actual_policy = tomllib.loads(policy_raw.decode('utf-8-sig'))
            if ({**actual_policy,'automation_enabled':actual_policy.get('automation_enabled',False)} !=
                    {k:v for k,v in policy.items() if not k.startswith('_')} or
                    actual_policy['version'] != self.REPLAY_POLICY or actual_policy['enabled'] is not True or
                    policy['_sha256'] != policy_ref['sha256'] or binding['policy_sha256'] != policy_ref['sha256'] or
                    binding['site_uid'] != policy['target_site_uid'] or binding['site_uid'] != self.settings.config['target_bilibili_site_uid'] or
                    binding['account_name'] != policy['target_account_name'] or binding['account_name'] != self.settings.config['target_bilibili_account_name'] or
                    not isinstance(policy['allowed_channel_ids'],list) or len(policy['allowed_channel_ids']) != 2 or
                    set(policy['allowed_channel_ids']) != CHANNELS or type(policy.get('automation_enabled',False)) is not bool or
                    policy['public_submission_mode'] != self.PUBLIC_MODE or
                    any(not isinstance(policy.get(k),str) or not policy[k].strip() or len(policy[k])>2048 for k in
                        ('owner_authorization_reference','owner_permission_reference','workflow_authorization_reference')) or
                    not isinstance(policy['acceptance_video_ids'],list) or
                    moment(policy['activated_at']) > datetime.now(timezone.utc).timestamp() or
                    policy['source_scope'] == 'pilot_single' and policy['acceptance_video_ids'] != [video_id] or
                    policy['source_scope'] == 'automatic_new_only' and (policy['acceptance_video_ids'] or end < moment(policy['activated_at'])) or
                    policy['source_scope'] not in ('pilot_single','automatic_new_only')):
                raise ValueError()
            # Reuse the unchanged metadata schema; this import performs no
            # machine or browser action and grants no submission authority.
            from .short_video_web import ShortVideoWeb
            metadata = ShortVideoWeb._metadata(binding['metadata'], url)
            if digest(metadata) != binding['metadata_sha256'] or '转载许可编号' in metadata['description']:
                raise ValueError()
        except HubError:
            raise
        except (KeyError, ValueError, TypeError, AttributeError, UnicodeError):
            fail('REPLAY_MACHINE_POLICY_OR_METADATA_CHANGED')
        if not shutil.which('ffprobe'): fail('REPLAY_MACHINE_FFPROBE_MISSING')
        binary = shutil.which('ffmpeg')
        if not binary: fail('REPLAY_MACHINE_FFMPEG_MISSING')
        video_ref, video_stat = read_checked(root, expected_file, maximum=self.MAX_BYTES, check=check, collect=False)
        if video_ref['bytes'] != asset['bytes'] or video_ref['sha256'] != asset['sha256']:
            fail('REPLAY_MACHINE_ARTIFACT_HASH_MISMATCH')
        details = self.inspect_source_media(expected_file, check=check)
        if (details['stream_config']['video']['codec_name'] != 'h264' or details['stream_config']['audio']['codec_name'] != 'aac' or
                abs(details['duration']-asset['duration']) > 2 or details['bytes'] != asset['bytes'] or
                {int(k):v for k,v in asset['stream_types'].items()} != details['stream_types']):
            fail('REPLAY_MACHINE_MEDIA_CHANGED')
        quality = self.quality_check(source, download_binding, asset, checkpoint['receipt'], details)
        from .replay_packet_cache_v1 import PacketProofCache,tool_identity
        cache=PacketProofCache(root,job['job_id'],self.VERSION,self.EVIDENCE_DIRECTORY,check=check,
            reader=lambda file,maximum:read_checked(root,file,maximum=maximum,check=check))
        identity=cache.identity(binding,download_binding,verified_ref,video_ref,details,tool_identity(shutil.which('ffprobe'),check))
        reused=cache.load(identity,asset.get('packet_evidence'))
        packets = reused['packets'] if reused else packet_evidence(expected_file, details['stream_types'], timeout=7200, check=check)
        packet_verification=reused['verification'] if reused else cache.full_scan(identity)
        if packets != asset.get('packet_evidence'): fail('REPLAY_MACHINE_PACKET_EVIDENCE_MISMATCH')
        cover_result = self._cover(root, job['job_id'], video_id, provenance_file, binary, check, attempt_id=state.get('attempt_id'))
        check()
        fresh = self.store.get(job['job_id']); fresh_source = self.store.get(source['job_id'])
        if (fresh['payload'] != binding or fresh['result'].get(self.REPLAY_STATE) != state or
                fresh_source['payload'] != source['payload'] or fresh_source['result'] != source['result'] or
                fresh_source['state'] != 'completed' or fingerprint(expected_file.stat()) != fingerprint(video_stat)):
            fail('REPLAY_MACHINE_INPUT_CHANGED_DURING_CHECK')
        for reference, maximum in ((verified_ref,262144),(policy_ref,16384)):
            after,_=read_checked(root,reference['file'],maximum=maximum,check=check)
            if after != reference: fail('REPLAY_MACHINE_INPUT_CHANGED_DURING_CHECK')
        checks = {'download': {'job_id':source['job_id'],'binding_sha256':digest(download_binding), 'verified_receipt':verified_ref,
                    'artifact':video_ref,'packet_evidence':packets,'packet_verification':packet_verification},
            'official_metadata': {'source_api':payload['source_api'],'raw_metadata_sha256':digest(raw), 'video_id':video_id,
                    'channel_id':binding['channel_id'],'actual_start_time':live['actualStartTime'],'actual_end_time':live['actualEndTime'],
                    'duration_seconds':download_binding['duration'],'privacy_status':raw['status']['privacyStatus']},
            'media':details,'cover':cover_result,
            'metadata':{'sha256':digest(metadata),'source_url':metadata['source'],'public_permission_line_present':False},
            'target':{'site_uid':binding['site_uid'],'account_name':binding['account_name'],'policy':policy_ref,
                    'source_scope':policy['source_scope'],'workflow_authorization_reference':policy['workflow_authorization_reference']}}
        checks=json.loads(json.dumps(checks))
        if quality is not None:
            checks['quality'] = quality
        record = {'version':self.VERSION,'job_id':job['job_id'],'binding_sha256':digest(binding),'policy_sha256':policy_ref['sha256'],
            'created_at':datetime.now(timezone.utc).isoformat(),'checks':checks,'human_review':False,
            'per_artifact_owner_acceptance':False,'publication_authorized':False,'external_requests':0}
        target = safe(root, root/'media'/job['job_id']/self.EVIDENCE_DIRECTORY/str(uuid.uuid4()))
        target.mkdir(mode=0o700,parents=True); safe(root,target)
        body = json.dumps(record,sort_keys=True,separators=(',',':')).encode()
        file = target/'receipt.json'; descriptor=os.open(file,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(descriptor,'wb') as stream:
            stream.write(body);stream.flush();os.fsync(stream.fileno())
        if os.name != 'nt':
            descriptor=os.open(target,os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)
        # Reopen the exact evidence to bind actual persisted bytes, never only
        # an in-memory dictionary that may not have reached disk.
        evidence_ref, stored = read_checked(root,file,maximum=262144,check=check)
        if stored != body: fail('REPLAY_MACHINE_EVIDENCE_WRITE_FAILED')
        if not reused:cache.commit(identity,evidence_ref,json.loads(body))
        return {**deepcopy(record),'evidence_file':str(file),'evidence_sha256':evidence_ref['sha256'],'evidence_bytes':evidence_ref['bytes']}

    def quality_check(self, source, download_binding, asset, receipt, details):
        """Versioned subclasses may add stronger source-quality invariants."""
        return None

    def inspect_source_media(self, file, *, check):
        return inspect_media(file, check=check)

    def _cover(self, root, job_id, video_id, file, binary, check, *, attempt_id=None):
        canonical = root/'media'/job_id/'cover-assets-v4/provenance.json'
        if file is not None and Path(file) != canonical: fail('REPLAY_MACHINE_COVER_PATH_REJECTED')
        reference, body = read_checked(root, canonical, maximum=65536, check=check)
        try:
            value=json.loads(body); original=Path(value['input']['file'])
            folder=root/'media'/job_id/'short-video-attempts'
            if (original.parent.parent != folder or str(uuid.UUID(original.parent.name)) != original.parent.name or
                    attempt_id is not None and original.parent.name != attempt_id or
                    original.name != 'cover-source-official-maxres.jpg' or
                    value['input']['source_url'] != 'https://i.ytimg.com/vi/'+video_id+'/maxresdefault.jpg'):
                raise ValueError()
            source_ref, source_bytes=read_checked(root,original,maximum=cover.MAX_BYTES,check=check)
            output=canonical.parent/'top-bottom-blur-cover.png'
            output_ref, output_bytes=read_checked(root,output,maximum=cover.MAX_OUTPUT_BYTES,check=check)
            expected_input={**source_ref,'source_url':value['input']['source_url'],'width':1280,'height':720}
            if (set(value) != {'version','policy_version','input','output','transform','ffmpeg_version','image_edited',
                    'no_text_regenerated','official_original_bytes','created_at'} or
                    value['version'] != cover.VERSION or value['policy_version'] != cover.POLICY or
                    value['input'] != expected_input or cover.jpeg_dimensions(source_bytes) != (1280,720) or
                    value['output'] != {**output_ref,'width':1280,'height':960,'mime_type':'image/png'} or
                    cover.png_dimensions(output_bytes) != (1280,960) or value['transform'] != cover._transform() or
                    value['image_edited'] is not True or value['no_text_regenerated'] is not True or value['official_original_bytes'] is not False or
                    not isinstance(value['ffmpeg_version'],str) or not value['ffmpeg_version'].startswith('ffmpeg version ') or
                    not isinstance(value['created_at'],str)):
                raise ValueError()
        except (ValueError, KeyError, TypeError, AttributeError):
            fail('REPLAY_MACHINE_COVER_PROVENANCE_INVALID')
        env={k:os.environ[k] for k in ('PATH','SystemRoot','WINDIR','TEMP','TMP','LANG','LC_ALL') if k in os.environ}
        check(); source_rgb=cover._rgb(binary,source_bytes,'mjpeg','format=rgb24',env)
        check(); middle_rgb=cover._rgb(binary,output_bytes,'png','format=rgb24,crop=1280:720:0:120',env)
        if source_rgb != middle_rgb: fail('REPLAY_MACHINE_COVER_RGB_MISMATCH')
        for ref, maximum in ((source_ref,cover.MAX_BYTES),(output_ref,cover.MAX_OUTPUT_BYTES),(reference,65536)):
            after,_=read_checked(root,ref['file'],maximum=maximum,check=check)
            if after != ref: fail('REPLAY_MACHINE_COVER_CHANGED')
        return {'version':cover.VERSION,'policy_version':cover.POLICY,'provenance':reference,'source':source_ref,'output':output_ref,
            'source_rgb_sha256':hashlib.sha256(source_rgb).hexdigest(),'foreground_rgb_sha256':hashlib.sha256(middle_rgb).hexdigest(),
            'compared_rgb_bytes':len(source_rgb),'foreground_rect':cover.FOREGROUND,'safe_crop':cover.SAFE_CROP,
            'no_text_regenerated':True,'official_original_bytes':False}
