"""Bounded policy-authorized yt-dlp acquisition. No publisher or browser UI.

Only ordinary public HTTPS tracks are downloaded. Local Node/EJS decodes the
standard player; login, CAPTCHA, PO-token, DRM and region restrictions stop.
"""
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit, parse_qs
from urllib.request import Request as CookieRequest

from adapters.media_tools import inspect_media, packet_evidence, process_lines
from adapters.safe_https import open_request
from services.hub.budget import Budget
from services.hub.db import Store, encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.locks import resource_guard
from services.hub.maintenance import Maintenance
from services.hub.media_processing import atomic_json

YTDLP_VERSION = '2026.10.26'
EJS_VERSION = '0.8.0'
VERSION = 'youtube-replay-download-v1'
PROVIDER = 'youtube_download_http'
LIMITS = {PROVIDER: dict(daily_usd=0, monthly_usd=0, daily_calls=1000,
                       monthly_calls=30000, max_call_usd=0, paid_authorized=False)}
DEFAULT_REQUEST_LIMIT = 60
MAX_REQUEST_LIMIT = 512
COOKIE_DOMAINS = frozenset(('.youtube.com', 'youtube.com'))
AUTH_CONTEXT_HEADERS = frozenset(('cookie', 'authorization', 'x-goog-authuser', 'x-goog-pageid',
                                 'x-youtube-identity-token', 'x-youtube-bootstrap-logged-in', 'x-origin'))
REDIRECT_CONTEXT_HEADERS = AUTH_CONTEXT_HEADERS | frozenset(('x-goog-visitor-id','origin','referer',
                                                            'x-youtube-client-name','x-youtube-client-version'))


class SilentCookieLog:
    def info(self, *args, **kwargs): pass
    def debug(self, *args, **kwargs): pass
    def warning(self, *args, **kwargs): pass
    def error(self, *args, **kwargs): pass


def filter_youtube_cookies(jar):
    from yt_dlp.cookies import YoutubeDLCookieJar
    filtered = YoutubeDLCookieJar()
    for cookie in jar:
        if cookie.domain in COOKIE_DOMAINS:
            filtered.set_cookie(cookie)
    return filtered


def _session_profile_readonly(profile):
    try:
        return any(parts[4] == str(profile) and 'ro' in parts[5].split(',')
                   for parts in (line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()))
    except (OSError, IndexError): return False


def load_youtube_session(settings, binding):
    if binding.get('authenticated_session') is not True:
        raise HubError('YOUTUBE_SESSION_NOT_AUTHORIZED')
    if binding.get('session_profile') != 'youtube' or binding.get('session_authorization_sha256') != binding['policy_sha256']:
        raise HubError('YOUTUBE_SESSION_NOT_AUTHORIZED')
    profile = settings.root / 'data/browser-profiles/youtube'
    if (profile.is_symlink() or not profile.is_dir() or
            any(p.is_symlink() for p in profile.parents) or
            os.name != 'posix' or profile.stat().st_uid != os.getuid() or profile.stat().st_mode & 0o077):
        raise HubError('YOUTUBE_SESSION_PROFILE_NOT_PRIVATE')
    lock = profile / 'SingletonLock'
    if lock.exists() or lock.is_symlink():
        raise HubError('YOUTUBE_SESSION_BROWSER_STILL_OPEN')
    if not _session_profile_readonly(profile):
        raise HubError('YOUTUBE_SESSION_READONLY_MOUNT_REQUIRED')
    default = profile / 'Default'
    if default.is_symlink() or not default.is_dir():
        raise HubError('YOUTUBE_SESSION_UNAVAILABLE')
    paths = [default/'Cookies', default/'Network/Cookies']
    databases = [p for p in paths if p.is_file()]
    if (not databases or any(p.is_symlink() or any(parent.is_symlink() for parent in p.parents)
                             or p.stat().st_size > 32_000_000 for p in databases)
            or (profile/'Local State').is_symlink()):
        raise HubError('YOUTUBE_SESSION_UNAVAILABLE')
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
        jar = filter_youtube_cookies(extract_cookies_from_browser('chromium', profile=str(default),
                                                                logger=SilentCookieLog(), keyring=None))
        names = {cookie.name for cookie in jar if cookie.value and not cookie.is_expired()}
        if 'LOGIN_INFO' not in names or not names & {'SAPISID', '__Secure-1PAPISID', '__Secure-3PAPISID'}:
            raise HubError('YOUTUBE_SESSION_AUTH_EXPIRED')
        return jar
    except HubError: raise
    except Exception:
        raise HubError('YOUTUBE_SESSION_UNAVAILABLE') from None


def scoped_headers(defaults, supplied, url, *, authenticated, jar=None, original_host=None):
    """Credentials only reach the authorized YouTube origin, never media/CDN."""
    from yt_dlp.utils.networking import HTTPHeaderDict
    host = destination(url)
    values = HTTPHeaderDict(defaults or {}, supplied or {})
    if any(k.lower() in ('proxy-authorization', 'host', 'forwarded', 'x-forwarded-for', 'x-real-ip') for k in values):
        raise HubError('YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN')
    changed_origin = original_host is not None and host != original_host
    if changed_origin:
        values = HTTPHeaderDict({k:v for k,v in values.items() if k.lower() not in REDIRECT_CONTEXT_HEADERS})
    authorized = authenticated is True and host == 'www.youtube.com' and not changed_origin
    if any(k.lower() in AUTH_CONTEXT_HEADERS for k in values) and not authorized:
        raise HubError('YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN')
    # Only our filtered, explicitly authorized server jar supplies Cookie.
    if 'Cookie' in values: del values['Cookie']
    if authorized and jar is not None:
        cookie = jar.get_cookie_header(url)
        if cookie: values['Cookie'] = cookie
    headers = {k:v for k,v in values.items() if k.lower() != 'accept-encoding'}
    headers['Accept-Encoding'] = 'identity'
    return headers


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def request_limit(settings):
    """A bounded transport allowance, separate from immutable content rights.

    yt-dlp's normal YouTube HTTPS downloader uses roughly 10MiB ranges, so a
    long, authorized file can legitimately require hundreds of HTTP requests.
    The shared provider's persisted daily/monthly request budget still applies.
    """
    value = settings.config.get('youtube_download_max_http_requests', DEFAULT_REQUEST_LIMIT)
    if type(value) is not int or not DEFAULT_REQUEST_LIMIT <= value <= MAX_REQUEST_LIMIT:
        raise HubError('YOUTUBE_DOWNLOAD_REQUEST_LIMIT_INVALID', review=True)
    return value


def destination(url, *, track=False):
    try:
        if not isinstance(url, str) or len(url) > 16384 or any(ord(c) < 33 for c in url):
            raise ValueError()
        p = urlsplit(url); host = p.hostname
        cdn = bool(host and host.endswith('.googlevideo.com') and host != 'googlevideo.com')
        if (p.scheme != 'https' or p.username or p.password or p.fragment or p.port not in (None, 443)
                or not host or not re.fullmatch(r'[a-z0-9.-]+', host)
                or not cdn and host not in ('www.youtube.com', 'youtube.com', 's.ytimg.com', 'i.ytimg.com')
                or track and (not cdn or p.path != '/videoplayback')):
            raise ValueError()
        if {'pot', 'po_token'} & set(parse_qs(p.query)):
            raise HubError('YOUTUBE_DOWNLOAD_PLATFORM_TOKEN_REQUIRED')
        return host
    except (ValueError, TypeError, AttributeError):
        raise HubError('YOUTUBE_DOWNLOAD_DESTINATION_REJECTED') from None


def select_tracks(info, binding):
    if (not isinstance(info, dict) or info.get('id') != binding['video_id']
            or info.get('channel_id') != binding['channel_id'] or info.get('entries') is not None
            or info.get('_type', 'video') != 'video'):
        raise HubError('YOUTUBE_DOWNLOAD_IDENTITY_CHANGED')
    if (info.get('is_live') is True or info.get('live_status') in ('is_live', 'is_upcoming', 'post_live')
            or info.get('availability') not in (None, 'public') or info.get('age_limit', 0)
            or info.get('has_drm') is True):
        raise HubError('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED')
    duration = info.get('duration')
    if (type(duration) not in (int, float) or not math.isfinite(duration)
            or abs(duration - binding['duration']) > 2):
        raise HubError('YOUTUBE_DOWNLOAD_DURATION_CHANGED')
    videos, audios, combined = [], [], []
    for f in info.get('formats', [])[:300]:
        if (not isinstance(f, dict) or f.get('protocol') != 'https' or f.get('fragments')
                or f.get('manifest_url') or f.get('has_drm') or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', str(f.get('format_id', '')))):
            continue
        video = isinstance(f.get('vcodec'), str) and f['vcodec'].startswith(('avc1', 'h264'))
        audio = isinstance(f.get('acodec'), str) and f['acodec'].startswith(('mp4a', 'aac'))
        if video and (type(f.get('height')) is not int or not 1 <= f['height'] <= binding['max_height']):
            continue
        if not (video or audio) or f.get('ext') not in ('mp4', 'm4a'):
            continue
        destination(f.get('url'), track=True)
        headers = f.get('http_headers') or {}
        if any(k.lower() in ('cookie', 'authorization', 'proxy-authorization') for k in headers):
            raise HubError('YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN')
        size = f.get('filesize') or f.get('filesize_approx')
        if size is not None and (type(size) not in (int, float) or not math.isfinite(size) or not 0 < size <= binding['max_bytes']):
            continue
        (combined if video and audio else videos if video and f.get('acodec') == 'none'
         else audios if audio and f.get('vcodec') == 'none' else []).append(f)
    choices = [[f] for f in combined] + [[v, a] for v in videos for a in audios]
    choices = [fs for fs in choices if sum(f.get('filesize') or f.get('filesize_approx') or 0 for f in fs) <= binding['max_bytes']]
    if not choices:
        raise HubError('YOUTUBE_DOWNLOAD_PUBLIC_FORMAT_UNAVAILABLE')
    return max(choices, key=lambda fs: (max(f.get('height') or 0 for f in fs),
                                       sum(f.get('tbr') or 0 for f in fs)))


def track_identity(tracks):
    # No signed URLs, request headers or expiring tokens in durable evidence.
    return [{k: f.get(k) for k in ('format_id', 'ext', 'vcodec', 'acodec', 'height', 'width', 'filesize')}
            for f in tracks]


def player_access_guard(response):
    if not isinstance(response, dict): return
    status = response.get('playabilityStatus') or {}
    reason = str(status.get('reason', '')).lower()
    if (status.get('status') in ('LOGIN_REQUIRED', 'AGE_VERIFICATION_REQUIRED', 'CONTENT_CHECK_REQUIRED', 'AGE_CHECK_REQUIRED')
            or any(x in reason for x in ('sign in', 'confirm your age', 'not a bot', 'captcha',
                                        'not available in your country', 'not available in your region', 'drm'))
            or (response.get('streamingData') or {}).get('licenseInfos')):
        raise HubError('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED')


def private_environment(root):
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL') if k in os.environ}
    env.update(HUB_ROOT=str(root), PYTHONNOUSERSITE='1', YTDLP_NO_PLUGINS='1',
               HOME='/tmp/lighthouse-ytdlp', XDG_CONFIG_HOME='/tmp/lighthouse-ytdlp', XDG_CACHE_HOME='/tmp/lighthouse-ytdlp')
    # The parent may use a dedicated video configuration. Keep that selection
    # in the helper without inheriting API keys, proxies or other environment.
    selected = os.environ.get('HUB_CONFIG')
    if selected is not None:
        try:
            path = Path(selected)
            base = Path(root).resolve()
            if (not path.is_absolute() or path.resolve() != path or not path.is_relative_to(base)
                    or any(p.is_symlink() for p in (path, *path.parents))):
                raise ValueError()
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 262144:
                raise ValueError()
            env['HUB_CONFIG'] = str(path)
        except (OSError, ValueError):
            raise HubError('YOUTUBE_DOWNLOAD_CONFIG_PATH_INVALID', review=True) from None
    return env


def checked_folder(settings, job_id):
    try:
        import uuid
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise HubError('YOUTUBE_DOWNLOAD_WORKSPACE_INVALID') from None
    folder = settings.root / 'media' / job_id / 'youtube-download-v1'
    if any(p.is_symlink() for p in (settings.root / 'media', folder.parent, folder)):
        raise HubError('YOUTUBE_DOWNLOAD_WORKSPACE_INVALID')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def bounded_files(folder, maximum):
    size = 0
    for f in folder.iterdir():
        if f.is_symlink() or not f.is_file():
            raise HubError('YOUTUBE_DOWNLOAD_WORKSPACE_INVALID')
        size += f.stat().st_size
    if size > maximum:
        raise HubError('YOUTUBE_DOWNLOAD_SIZE_LIMIT')
    return size


def source_packet_evidence(sources, check,*,max_packets=10_000_000,timeout=7200):
    if len(sources) == 1:
        details = inspect_media(sources[0], check=check)
        if (details['stream_config']['video']['codec_name'] != 'h264'
                or details['stream_config']['audio']['codec_name'] != 'aac'):
            raise HubError('YOUTUBE_DOWNLOADED_CODEC_CHANGED')
        return (packet_evidence(sources[0],details['stream_types'],timeout=7200,check=check) if max_packets==10_000_000 and timeout==7200 else
            packet_evidence(sources[0],details['stream_types'],timeout=timeout,check=check,max_packets=max_packets))
    proof = {}
    for file, kind, codec in zip(sources, ('video', 'audio'), ('h264', 'aac')):
        raw = bytearray()
        cmd = ['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-show_entries',
               'stream=index,codec_type,codec_name', '-of', 'json', str(file)]
        for line in process_lines(cmd, file.parent, 30, check):
            raw.extend(line)
            if len(raw) > 65536: raise HubError('YOUTUBE_DOWNLOAD_RESULT_INVALID')
        streams = json.loads(raw)['streams']
        if len(streams) != 1 or streams[0]['codec_type'] != kind or streams[0]['codec_name'] != codec:
            raise HubError('YOUTUBE_DOWNLOADED_CODEC_CHANGED')
        mapping={int(streams[0]['index']):kind};initial={kind:{'packets':0,'chain':'0'*64}}
        proof.update(packet_evidence(file,mapping,initial=initial,timeout=7200,check=check) if max_packets==10_000_000 and timeout==7200 else
            packet_evidence(file,mapping,initial=initial,timeout=timeout,check=check,max_packets=max_packets))
    return proof


class ReplayDownloader:
    def __init__(self, settings):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.maintenance = Maintenance(settings, self.store)

    def acquire(self, job, binding, *, check):
        folder = checked_folder(self.settings, job['job_id'])
        maximum = binding['max_bytes']; binding_hash = digest(binding)
        ticket = folder / 'ticket.json'; output = folder / 'download-result.json'
        receipt_file = folder / 'verified.json'
        bounded_files(folder, maximum * 2 + 32_000_000)
        if receipt_file.exists():
            if receipt_file.is_symlink() or receipt_file.stat().st_size > 262144:
                raise HubError('YOUTUBE_DOWNLOADED_FILE_CHANGED')
            old = json.loads(receipt_file.read_text('utf-8'))
            source = old['source_asset']; file = Path(source['file'])
            if (old.get('version') != VERSION or old.get('human_review') is not False
                    or old.get('publication_authorized') is not False or old.get('creator_original_bytes_verified') is not False
                    or old['binding_sha256'] != binding_hash or file != folder / 'original.mp4' or file.is_symlink()
                    or not file.is_file() or file.stat().st_size != source['bytes'] or sha256_file(file) != source['sha256']):
                raise HubError('YOUTUBE_DOWNLOADED_FILE_CHANGED')
            check()
            return old
        if ticket.exists() and json.loads(ticket.read_text('utf-8')).get('binding') != binding:
            raise HubError('YOUTUBE_DOWNLOAD_RESUME_BINDING_CHANGED')
        check()
        atomic_json(ticket, {'root': str(self.settings.root), 'job_id': job['job_id'],
                             'lease_owner': job['lease_owner'], 'binding': binding})
        ticket.chmod(0o600)
        result = None
        if output.exists():
            if output.is_symlink():
                raise HubError('YOUTUBE_DOWNLOAD_WORKSPACE_INVALID')
            if output.stat().st_size > 262144: raise HubError('YOUTUBE_DOWNLOAD_RESULT_INVALID')
            previous = json.loads(output.read_text('utf-8'))
            if not previous.get('error_code') and previous.get('binding_sha256') == binding_hash:
                result = previous
            else:
                atomic_json(folder / ('failure-attempt-' + str(job['attempts']) + '.json'), previous)
                output.unlink()  # Only an owned helper result; all stream partials remain.
        if result is None:
            # An interrupted local merge is derived from retained tracks, never
            # an original source. Overwrite it only when resuming that merge.
            self.maintenance.check_disk(maximum * 2 + 64_000_000)
            result = self._download_helper(job, binding, folder, ticket, output, check)
        return self._finalize(job, binding, folder, result, check)

    def _download_helper(self, job, binding, folder, ticket, output, check):
        maximum = binding['max_bytes']
        try:
            proc = subprocess.Popen([sys.executable, '-m', 'adapters.ytdlp_replay', '--download', str(ticket)],
                cwd=Path(__file__).resolve().parents[1], env=private_environment(self.settings.root),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=os.name != 'nt')
        except OSError:
            raise HubError('YOUTUBE_DOWNLOAD_RUNTIME_UNAVAILABLE') from None
        deadline = time.monotonic() + binding['timeout_seconds']
        try:
            while proc.poll() is None:
                check(); self.maintenance.check_disk(maximum + 64_000_000)
                bounded_files(folder, maximum + 16_000_000)
                if time.monotonic() > deadline:
                    raise HubError('YOUTUBE_DOWNLOAD_TIMEOUT', retryable=True)
                time.sleep(.5)
            check()
            if not output.is_file() or output.is_symlink() or output.stat().st_size > 262144:
                raise HubError('YOUTUBE_DOWNLOAD_HELPER_INTERRUPTED', retryable=True)
            result = json.loads(output.read_text('utf-8'))
            if proc.returncode or result.get('error_code'):
                raise HubError(result.get('error_code', 'YOUTUBE_DOWNLOAD_FAILED'), retryable=result.get('retryable') is True)
            if result.get('binding_sha256') != digest(binding): raise HubError('YOUTUBE_DOWNLOAD_RESULT_INVALID')
            return result
        finally:
            if proc.poll() is None:
                if os.name == 'nt': proc.terminate()
                else: os.killpg(proc.pid, signal.SIGTERM)
                try: proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == 'nt': proc.kill()
                    else: os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)

    def _finalize(self, job, binding, folder, result, check):
        maximum = binding['max_bytes']; binding_hash = digest(binding)
        receipt_file = folder / 'verified.json'
        tracks = result['tracks']
        if not 1 <= len(tracks) <= 2:
            raise HubError('YOUTUBE_DOWNLOAD_RESULT_INVALID')
        sources = []
        for track in tracks:
            name = 'stream-' + str(track['format_id']) + '.' + track['ext']
            if not re.fullmatch(r'stream-[A-Za-z0-9_-]{1,40}\.(mp4|m4a)', name):
                raise HubError('YOUTUBE_DOWNLOAD_RESULT_INVALID')
            file = folder / name
            if file.is_symlink() or not file.is_file() or file.stat().st_size != track['bytes'] or sha256_file(file) != track['sha256']:
                raise HubError('YOUTUBE_DOWNLOAD_TRACK_CHANGED')
            sources.append(file)
        total = sum(f.stat().st_size for f in sources)
        if not 0 < total <= maximum:
            raise HubError('YOUTUBE_DOWNLOAD_SIZE_LIMIT')
        # The network stage has bounded CPU/RAM and does not monopolize heavy.
        # Wait locally for the existing lock; no extra external attempt is made.
        guard = None
        for _ in range(600):
            check(); self.maintenance.check_disk(total + 64_000_000)
            candidate = resource_guard(self.settings.root / 'data/locks/heavy')
            try:
                candidate.__enter__(); guard = candidate; break
            except HubError as exc:
                if exc.code != 'HEAVY_RESOURCE_BUSY': raise
                time.sleep(.5)
        if guard is None:
            raise HubError('HEAVY_RESOURCE_BUSY', retryable=True)
        try:
            def verify_check():
                check(); self.maintenance.check_disk()
                bounded_files(folder, maximum * 2 + 32_000_000)
            source_proof = source_packet_evidence(sources, verify_check)
            merged = folder / 'merged.partial.mp4'
            if merged.is_symlink(): raise HubError('YOUTUBE_DOWNLOAD_WORKSPACE_INVALID')
            command = ['ffmpeg', '-nostdin', '-y', '-v', 'error', '-protocol_whitelist', 'file,pipe', '-i', str(sources[0])]
            if len(sources) == 2: command += ['-protocol_whitelist', 'file,pipe', '-i', str(sources[1])]
            command += ['-map', '0:v:0', '-map', '1:a:0' if len(sources) == 2 else '0:a:0',
                        '-c', 'copy', '-threads', '1', '-f', 'mp4', str(merged)]
            for _ in process_lines(command, folder, min(7200, binding['timeout_seconds']), verify_check): pass
            details = inspect_media(merged, check=verify_check)
            if not 0 < details['bytes'] <= maximum or abs(details['duration'] - binding['duration']) > 2:
                raise HubError('YOUTUBE_DOWNLOADED_MEDIA_INCOMPLETE')
            if (details['stream_config']['video']['codec_name'] != 'h264'
                    or details['stream_config']['audio']['codec_name'] != 'aac'):
                raise HubError('YOUTUBE_DOWNLOADED_CODEC_CHANGED')
            proof = packet_evidence(merged, details['stream_types'], timeout=7200, check=verify_check)
            if proof != source_proof: raise HubError('YOUTUBE_DOWNLOAD_MERGE_PACKET_MISMATCH')
            file_hash = sha256_file(merged); verify_check()
            final = folder / 'original.mp4'
            if final.is_symlink(): raise HubError('YOUTUBE_DOWNLOAD_WORKSPACE_INVALID')
            os.replace(merged, final)
            receipt = {'version': VERSION, 'binding_sha256': binding_hash,
                'source_asset': {'file': str(final), 'bytes': final.stat().st_size, 'sha256': file_hash,
                                 'duration': details['duration'], 'stream_types': details['stream_types'], 'packet_evidence': proof},
                'receipt': {'yt_dlp_version': YTDLP_VERSION, 'ejs_version': EJS_VERSION, 'tracks': tracks,
                            'http': result['http'], 'local_merge': 'ffmpeg_stream_copy', 'resumable_partials_retained': True,
                            'authenticated_session':binding.get('authenticated_session') is True,
                            'credential_origin_scope':['https://www.youtube.com'] if binding.get('authenticated_session') is True else [],
                            'hash_kind': 'computed_after_download', 'expected_source_hash_verified': False,
                            'source_to_merged_packet_coverage_verified': True},
                'human_review': False, 'publication_authorized': False, 'creator_original_bytes_verified': False}
            atomic_json(receipt_file, receipt); receipt_file.chmod(0o600)
            return receipt
        finally:
            guard.__exit__(None, None, None)


def child_download(ticket):
    from services.hub.config import Settings
    from services.hub.youtube_download import YouTubeDownload
    from yt_dlp.networking.common import Request, Response
    from yt_dlp.networking.exceptions import HTTPError
    from yt_dlp.utils.networking import HTTPHeaderDict
    import yt_dlp
    from yt_dlp.extractor.youtube._video import YoutubeIE as BaseYoutubeIE
    from yt_dlp.version import __version__
    from importlib.metadata import version
    data = json.loads(ticket.read_text('utf-8')); settings = Settings.load()
    folder = checked_folder(settings, data['job_id']); output = folder / 'download-result.json'
    if ticket != folder / 'ticket.json' or ticket.is_symlink():
        raise HubError('YOUTUBE_DOWNLOAD_WORKSPACE_INVALID')
    store = Store(settings.db_path); service = YouTubeDownload(settings, store)
    job = store.get(data['job_id']); binding = data['binding']
    if not job or job.get('lease_owner') != data['lease_owner']:
        raise HubError('YOUTUBE_DOWNLOAD_LEASE_LOST')
    job['lease_owner'] = data['lease_owner']
    maintenance = Maintenance(settings, store); stats = {'requests': 0, 'response_bytes': 0}
    budget = Budget(store, LIMITS)
    last_progress = 0
    def check():
        # Same immutable job binding and fresh lease as the parent service.
        service.check_binding(job, binding)
        maintenance.check_disk(binding['max_bytes'] + 64_000_000)
        bounded_files(folder, binding['max_bytes'] + 16_000_000)
    def progress(message):
        nonlocal last_progress
        check()
        if time.monotonic() - last_progress >= 2:
            last_progress = time.monotonic()
            atomic_json(folder / 'progress.json', {'version': VERSION, 'checked_at': time.time(),
                'format_id': message.get('info_dict', {}).get('format_id'),
                'status': message.get('status'), 'downloaded_bytes': message.get('downloaded_bytes'),
                'total_bytes': message.get('total_bytes'), 'http': stats})
    class Log:
        def debug(self, *a): pass
        def warning(self, *a): pass
        def error(self, *a): pass
    class YoutubeIE(BaseYoutubeIE):
        # Same version-pinned ordinary initial client list as upstream. Stop on
        # protection BEFORE upstream's age/login fallback logic can run.
        def _extract_player_response(self, *args, **kwargs):
            response = super()._extract_player_response(*args, **kwargs)
            player_access_guard(response)
            return response
        def _extract_player_responses(self, clients, video_id, webpage, webpage_client, webpage_ytcfg, is_premium_subscriber):
            if webpage:
                initial = self._search_json(self._YT_INITIAL_PLAYER_RESPONSE_RE, webpage,
                    'initial player protection check', video_id, fatal=False)
                player_access_guard(initial)
            return super()._extract_player_responses(clients, video_id, webpage, webpage_client, webpage_ytcfg, is_premium_subscriber)
        @staticmethod
        def _is_agegated(player_response):
            if BaseYoutubeIE._is_agegated(player_response):
                raise HubError('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED')
            return False
    class StreamResponse(Response):
        def __init__(self, connection, response, url):
            super().__init__(response, url, dict(response.getheaders()), status=response.status)
            self.connection = connection; self.read_bytes = 0
            self.bound = binding['max_bytes'] if destination(url).endswith('.googlevideo.com') else 8_388_608
        def read(self, amt=None):
            check()
            metadata_read=amt is None or amt<0
            if amt is None or amt < 0:
                # Extractors also read small CDN manifests/probes with read().
                # Bound that operation independently of the media file limit.
                raw = self.fp.read(min(8_388_609, max(0,self.bound - self.read_bytes + 1)))
            else: raw = self.fp.read(min(amt, 8_388_608))
            self.read_bytes += len(raw); stats['response_bytes'] += len(raw)
            if metadata_read and len(raw)>8_388_608:
                raise HubError('YOUTUBE_DOWNLOAD_METADATA_RESPONSE_LIMIT')
            if self.read_bytes > self.bound or stats['response_bytes'] > binding['max_bytes'] * 2 + 32_000_000:
                raise HubError('YOUTUBE_DOWNLOAD_SIZE_LIMIT')
            return raw
        def close(self):
            try: super().close()
            finally: self.connection.close()
    class Guarded(yt_dlp.YoutubeDL):
        def urlopen(self, request):
            request = Request(request) if isinstance(request, str) else request
            if not isinstance(request, Request) or any(request.proxies.values()) or request.extensions.get('impersonate'):
                raise HubError('YOUTUBE_DOWNLOAD_REQUEST_REJECTED')
            if request.method not in ('GET', 'HEAD', 'POST') or request.data is not None and (not isinstance(request.data, bytes) or len(request.data) > 1_048_576):
                raise HubError('YOUTUBE_DOWNLOAD_REQUEST_REJECTED')
            url = request.url; original_host = destination(url)
            for hop in range(4):
                check(); host = destination(url)
                headers = scoped_headers(self.params.get('http_headers'), request.headers, url,
                    authenticated=binding.get('authenticated_session') is True,
                    jar=self.cookiejar, original_host=original_host)
                if stats['requests'] >= stats['request_limit']: raise HubError('YOUTUBE_DOWNLOAD_REQUEST_LIMIT')
                stats['requests'] += 1; call = budget.reserve(PROVIDER, job['job_id']); connection = None
                try:
                    connection, response = open_request(url, [host], method=request.method, headers=headers, body=request.data)
                    status = response.status
                    mime=(response.getheader('Content-Type','').split(';',1)[0] or 'unknown').lower()
                    kind=('manifest' if 'mpegurl' in mime or 'dash+xml' in mime else
                          'media' if mime.startswith(('video/','audio/')) else
                          'json' if 'json' in mime else 'html' if 'html' in mime else
                          'javascript' if 'javascript' in mime else 'other')
                    counts=stats.setdefault('response_kinds',{});counts[kind]=counts.get(kind,0)+1
                    if status in (301, 302, 303, 307, 308):
                        if request.method not in ('GET', 'HEAD') or hop == 3: raise HubError('YOUTUBE_DOWNLOAD_REDIRECT_REJECTED')
                        url = urljoin(url, response.getheader('Location', '')); destination(url)
                        connection.close(); connection = None; budget.finish(call, 'done'); continue
                    if status in (401, 403): raise HubError('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED')
                    if status == 429: raise HubError('YOUTUBE_DOWNLOAD_RATE_LIMITED')
                    if status >= 500: raise HubError('YOUTUBE_DOWNLOAD_NETWORK_ERROR', retryable=True)
                    if binding.get('authenticated_session') is True and host == 'www.youtube.com':
                        self.cookiejar.extract_cookies(response, CookieRequest(url))
                        for cookie in list(self.cookiejar):
                            if cookie.domain not in COOKIE_DOMAINS:
                                self.cookiejar.clear(cookie.domain, cookie.path, cookie.name)
                    wrapped = StreamResponse(connection, response, url); connection = None
                    budget.finish(call, 'done')
                    if status not in (200, 206): raise HTTPError(wrapped)
                    if response.getheader('Content-Encoding', 'identity') not in ('identity', ''):
                        wrapped.close(); raise HubError('YOUTUBE_DOWNLOAD_CONTENT_ENCODING_REJECTED')
                    return wrapped
                except BaseException as exc:
                    # HTTP receipt counts are request outcomes, not a bandwidth invoice.
                    budget.finish(call, 'error', getattr(exc, 'code', 'YOUTUBE_DOWNLOAD_NETWORK_ERROR'))
                    raise
                finally:
                    if connection: connection.close()
            raise HubError('YOUTUBE_DOWNLOAD_REDIRECT_REJECTED')
    info=None
    try:
        if __version__ != YTDLP_VERSION or version('yt-dlp-ejs') != EJS_VERSION:
            raise HubError('YOUTUBE_DOWNLOAD_DEPENDENCY_VERSION_CHANGED')
        stats['request_limit'] = request_limit(settings)
        check()
        authenticated = binding.get('authenticated_session') is True
        session = load_youtube_session(settings,binding) if authenticated else None
        clients = BaseYoutubeIE._DEFAULT_AUTHED_CLIENTS if authenticated else BaseYoutubeIE._DEFAULT_CLIENTS
        options = {'quiet': True, 'logger': Log(), 'cachedir': False, 'noplaylist': True,
            'proxy': '', 'usenetrc': False, 'cookiefile': None, 'cookiesfrombrowser': None,
            'username': None, 'password': None, 'nocheckcertificate': False, 'geo_bypass': False, 'xff': 'never',
            'extractor_retries': 0, 'retries': 0, 'fragment_retries': 0, 'socket_timeout': 20,
            'concurrent_fragment_downloads': 1, 'skip_unavailable_fragments': False,
            'js_runtimes': {'node': {'path': '/usr/local/bin/node'}}, 'remote_components': [],
            'postprocessors': [], 'fixup': 'never', 'continuedl': True, 'nopart': False, 'overwrites': False,
            'max_filesize': binding['max_bytes'], 'ratelimit': 2_000_000, 'buffersize': 65536,
            'noprogress': True, 'progress_hooks': [progress], 'mark_watched': False,
            'impersonate': None, 'writeinfojson': False, 'writesubtitles': False, 'writethumbnail': False,
            'getcomments': False, 'allowed_extractors': ['youtube'],
            'extractor_args': {'youtube': {'player_client': list(clients)}}}
        with Guarded(options) as ydl:
            if authenticated: ydl.cookiejar = session
            ydl.add_info_extractor(YoutubeIE())
            info = ydl.extract_info('https://www.youtube.com/watch?v=' + binding['video_id'], download=False)
            selected = select_tracks(info, binding); identities = track_identity(selected)
            selection = folder / 'selection.json'; expected = {'binding_sha256': digest(binding), 'tracks': identities}
            if selection.exists() and (selection.is_symlink() or json.loads(selection.read_text('utf-8')) != expected):
                raise HubError('YOUTUBE_DOWNLOAD_RESUME_FORMAT_CHANGED')
            atomic_json(selection, expected); selection.chmod(0o600)
            tracks = []
            for fmt, identity in zip(selected, identities):
                check(); file = folder / ('stream-' + str(fmt['format_id']) + '.' + fmt['ext'])
                if file.is_symlink() or Path(str(file) + '.part').is_symlink():
                    raise HubError('YOUTUBE_DOWNLOAD_WORKSPACE_INVALID')
                ok, _ = ydl.dl(str(file), fmt)
                if not ok or not file.is_file() or not 0 < file.stat().st_size <= binding['max_bytes']:
                    raise HubError('YOUTUBE_DOWNLOAD_TRACK_INCOMPLETE')
                if fmt.get('filesize') and file.stat().st_size != fmt['filesize']:
                    raise HubError('YOUTUBE_DOWNLOAD_TRACK_INCOMPLETE')
                tracks.append({**identity, 'bytes': file.stat().st_size, 'sha256': sha256_file(file)})
        atomic_json(output, {'binding_sha256': digest(binding), 'tracks': tracks, 'http': stats}); output.chmod(0o600)
        return 0
    except HubError as exc:
        error = {'error_code': exc.code, 'retryable': exc.retryable}
    except (OSError, http.client.HTTPException):
        error = {'error_code': 'YOUTUBE_DOWNLOAD_NETWORK_ERROR', 'retryable': True}
    except Exception as exc:
        low = str(exc).lower()
        code = ('YOUTUBE_DOWNLOAD_RATE_LIMITED' if '429' in low else
                'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED' if any(x in low for x in ('403', 'sign in', 'captcha', 'confirm you', 'not a bot', 'age-restrict', 'not available in your country', 'drm', 'po token')) else
                'YOUTUBE_DOWNLOAD_PUBLIC_FORMAT_UNAVAILABLE' if 'format' in low else 'YOUTUBE_DOWNLOAD_EXTRACTION_FAILED')
        error = {'error_code': code, 'retryable': False}
    if isinstance(info,dict):
        error['formats_observed']=[{k:f.get(k) for k in ('format_id','protocol','ext','vcodec','acodec','height','filesize','language')}
                                   for f in info.get('formats',[])[:100] if isinstance(f,dict)]
    atomic_json(output, {**error, 'http': stats, 'partial_files_retained': True}); output.chmod(0o600)
    return 1


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--download':
        raise SystemExit('INTERNAL_DOWNLOAD_HELPER_ONLY')
    raise SystemExit(child_download(Path(sys.argv[2])))
