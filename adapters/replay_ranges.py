"""Bounded, ordered HTTPS range reads for one exact-size replay track."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
import errno
import os
import stat
import time

from services.hub.errors import HubError


def worker_count(config):
    count = config.get("youtube_download_range_workers", 2)
    if type(count) is not int or count not in (1, 2, 3, 4):
        raise HubError("YOUTUBE_ACCESS_CONFIG_INVALID", review=True)
    return count


def replay_rate(config):
    rate = config.get("youtube_download_rate_bytes_per_second", 18_750_000)
    if type(rate) is not int or not 1 <= rate <= 100_000_000:
        raise HubError("YOUTUBE_ACCESS_CONFIG_INVALID", review=True)
    return rate


class ReplayBandwidth:
    """One shared byte clock for all readers and both tracks of a replay."""
    def __init__(self, rate):
        self.rate = rate
        self.next_read = time.monotonic()
        self.lock = Lock()

    def wait(self, count, stopped):
        with self.lock:
            now = time.monotonic()
            self.next_read = max(now, self.next_read) + count / self.rate
            deadline = self.next_read
        return stopped.wait(max(0, deadline - time.monotonic()))


def download_track(path, size, *, workers, open_range, check, progress,
                   bandwidth, chunk_size=10 * 1024 * 1024):
    """The coordinator admits requests and commits only contiguous, complete ranges."""
    partial = Path(str(path) + ".part")
    for target in (path, partial):
        try:
            info = target.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > size:
            raise HubError("YOUTUBE_HIGHEST_WORKSPACE_INVALID", review=True)
    if path.exists():
        if path.stat().st_size != size or partial.exists():
            raise HubError("YOUTUBE_HIGHEST_TRACK_INCOMPLETE", review=True)
        return

    stopped = Event()
    def read_body(response, length):
        with response:
            body = bytearray()
            while len(body) < length:
                if stopped.is_set():
                    return
                data = response.read(min(512 * 1024, length - len(body)))
                if not data:
                    raise HubError("YOUTUBE_DOWNLOAD_NETWORK_ERROR", retryable=True)
                if bandwidth.wait(len(data), stopped):
                    return
                body.extend(data)
            if len(body) != length or response.read(1):
                raise HubError("YOUTUBE_HIGHEST_TRACK_INCOMPLETE", review=True)
            return body

    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        with os.fdopen(os.open(partial, flags, 0o600), "ab", buffering=0) as output:
            committed = os.fstat(output.fileno()).st_size
            start = committed
            pending = deque()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                try:
                    while pending or start < size:
                        while len(pending) < workers and start < size:
                            end = min(size, start + chunk_size)
                            check()
                            response = open_range(start, end - 1)
                            try:
                                future = pool.submit(read_body, response, end - start)
                            except BaseException:
                                response.close()
                                raise
                            pending.append(future)
                            start = end
                        block = pending.popleft().result()
                        check()
                        if output.write(block) != len(block):
                            raise HubError("YOUTUBE_HIGHEST_TRACK_INCOMPLETE", review=True)
                        committed += len(block)
                        progress(committed)
                        del block
                    os.fsync(output.fileno())
                finally:
                    stopped.set()
        check()
        if partial.stat().st_size != size:
            raise HubError("YOUTUBE_HIGHEST_TRACK_INCOMPLETE", review=True)
        partial.rename(path)
    except OSError as exc:
        if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
            raise HubError("DISK_LOW", review=True) from None
        raise
