"""Official public thumbnail -> deterministic accepted cover policy.

No generated text/image API, Cookie, login, proxy, or platform-internal API.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

import httpx

from .cover_normalizer_v4 import normalize_cover, _read, _sync_directory
from .errors import HubError
from .locks import resource_guard


def _private(path, root):
    if not path.is_relative_to(root) or path.resolve() != path:
        raise HubError('REPLAY_COVER_PATH_INVALID', review=True)
    for entry in (path, *path.parents):
        if entry.exists() and entry.is_symlink():
            raise HubError('REPLAY_COVER_PATH_INVALID', review=True)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise HubError('REPLAY_COVER_PATH_INVALID', review=True)


def _exclusive_bytes(path, data):
    with path.open('xb') as handle:
        if os.name != 'nt':
            os.chmod(path, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if os.name != 'nt':
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def ensure_replay_cover(settings, job, binding, attempt_id, *, client_factory=httpx.Client):
    root = settings.root
    try:
        if (str(uuid.UUID(job['job_id'])) != job['job_id'] or str(uuid.UUID(attempt_id)) != attempt_id or
                not re.fullmatch('[A-Za-z0-9_-]{11}', binding['source_id'])):
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HubError('REPLAY_COVER_BINDING_INVALID', review=True) from None
    url = 'https://i.ytimg.com/vi/'+binding['source_id']+'/maxresdefault.jpg'
    with resource_guard(root/'data/locks'/('replay-cover-'+job['job_id'])):
        provenance = root/'media'/job['job_id']/'cover-assets-v4/provenance.json'
        if provenance.exists():
            try:
                if provenance.is_symlink() or provenance.stat().st_size > 32768:
                    raise ValueError()
                source = json.loads(provenance.read_bytes())['input']
                if (source['source_url'] != url or source['file'] != str(root/'media'/job['job_id']/'short-video-attempts'/attempt_id/'cover-source-official-maxres.jpg')):
                    raise ValueError()
                return normalize_cover(root, job['job_id'], binding['source_id'], source['file'], source['sha256'], url)
            except (OSError, KeyError, TypeError, ValueError):
                raise HubError('REPLAY_COVER_EXISTING_PROVENANCE_INVALID', review=True) from None
        folder = root/'media'/job['job_id']/'short-video-attempts'/attempt_id
        _private(folder, root)
        original = folder/'cover-source-official-maxres.jpg'
        if not original.exists():
            try:
                # Redirects, ambient proxy credentials, and authentication are
                # intentionally absent. Only this canonical official URL is read.
                with client_factory(timeout=30, follow_redirects=False, trust_env=False) as client:
                    with client.stream('GET', url) as response:
                        if response.status_code == 429:
                            raise HubError('REPLAY_COVER_RATE_LIMITED', retryable=True, retry_after=60)
                        if response.status_code != 200 or response.headers.get('content-type', '').split(';')[0].lower() not in ('image/jpeg', 'image/jpg'):
                            raise HubError('REPLAY_OFFICIAL_COVER_UNAVAILABLE', review=True)
                        data = bytearray()
                        for part in response.iter_bytes(65536):
                            data.extend(part)
                            if len(data) > 1_000_000:
                                raise HubError('REPLAY_OFFICIAL_COVER_TOO_LARGE', review=True)
                if not data:
                    raise HubError('REPLAY_OFFICIAL_COVER_UNAVAILABLE', review=True)
                temporary=original.with_name(original.name+'.'+str(uuid.uuid4())+'.partial')
                _exclusive_bytes(temporary, bytes(data))
                try:
                    os.link(temporary,original)
                finally:
                    temporary.unlink(missing_ok=True)
                _sync_directory(folder)
            except httpx.RequestError:
                raise HubError('REPLAY_OFFICIAL_COVER_NETWORK_ERROR', retryable=True) from None
        if original.is_symlink() or original.resolve() != original or not 0 < original.stat().st_size <= 1_000_000:
            raise HubError('REPLAY_COVER_SOURCE_INVALID', review=True)
        data = _read(original,maximum=1_000_000)
        return normalize_cover(root, job['job_id'], binding['source_id'], str(original), hashlib.sha256(data).hexdigest(), url)
