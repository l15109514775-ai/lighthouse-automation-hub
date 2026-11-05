from pathlib import Path
import hashlib
import os
import socket
from services.hub.errors import HubError
from .safe_https import open_request


def download(url, destination, *, allowed_hosts, max_bytes, check_disk, expected_sha256=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + '.partial')
    check_disk(max_bytes)
    total, digest = 0, hashlib.sha256()
    try:
        connection, response = open_request(url, allowed_hosts)
        try:
            if response.status in (401,403):
                raise HubError('ASSET_PERMISSION_DENIED', review=True)
            if response.status == 429 or response.status >= 500:
                raise HubError('ASSET_NETWORK_ERROR', retryable=True)
            if response.status != 200:
                raise HubError('ASSET_HTTP_REJECTED', review=True)
            length = response.getheader('Content-Length')
            if length and (not length.isdigit() or int(length) > max_bytes):
                raise HubError('ASSET_SIZE_LIMIT', review=True)
            with partial.open('wb') as stream:
                while chunk := response.read(256 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise HubError('ASSET_SIZE_LIMIT', review=True)
                    check_disk(len(chunk))
                    stream.write(chunk)
                    digest.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            if not total or (length and total != int(length)):
                raise HubError('ASSET_INCOMPLETE', retryable=True)
            actual = digest.hexdigest()
            if expected_sha256 and actual != expected_sha256:
                raise HubError('ASSET_HASH_MISMATCH', review=True)
            os.replace(partial, destination)
            return {'file':str(destination), 'sha256':actual, 'bytes':total}
        finally:
            connection.close()
    except (OSError, socket.timeout) as exc:
        raise HubError('ASSET_NETWORK_ERROR', retryable=True) from None
