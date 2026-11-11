import io
import time
from threading import Barrier, Lock

import pytest

from adapters.replay_ranges import ReplayBandwidth, download_track, replay_rate, worker_count
from services.hub.errors import HubError


def test_two_readers_resume_contiguous_prefix_and_deliver_exact_bytes(tmp_path):
    original = bytes(range(253))
    path = tmp_path / "stream.mp4"
    partial = tmp_path / "stream.mp4.part"
    partial.write_bytes(original[:7])
    ranges = []
    barrier = Barrier(2, timeout=5)
    guard = Lock()
    active = peak = 0

    class Body(io.BytesIO):
        def read(self, size):
            nonlocal active, peak
            if self.tell() == 0 and self.synchronize:
                with guard:
                    active += 1
                    peak = max(peak, active)
                try:
                    barrier.wait()
                finally:
                    with guard:
                        active -= 1
            return super().read(size)

    def open_range(start, end):
        ranges.append((start, end))
        stream = Body(original[start:end + 1])
        stream.synchronize = len(ranges) <= 2
        return stream

    progress = []
    download_track(path, len(original), workers=worker_count({}), chunk_size=32,
                   bandwidth=ReplayBandwidth(replay_rate({})), open_range=open_range, check=lambda: None, progress=progress.append)
    assert peak == 2
    assert ranges == [(s, min(s + 31, 252)) for s in range(7, 253, 32)]
    assert progress == [end + 1 for _, end in ranges]
    assert path.read_bytes() == original and not partial.exists()


@pytest.mark.parametrize("fault", ["refused", "truncated", "oversized"])
def test_bad_range_never_commits_later_bytes(tmp_path, fault):
    path = tmp_path / "stream.m4a"
    partial = tmp_path / "stream.m4a.part"
    partial.write_bytes(b"ab")
    data = b"abcdefghijklmnop"
    requests = []

    def open_range(start, end):
        requests.append(start)
        chunk = data[start:end + 1]
        if start == 5:
            if fault == "refused":
                raise HubError("YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED", review=True)
            chunk = chunk[:-1] if fault == "truncated" else chunk + b"!"
        return io.BytesIO(chunk)

    with pytest.raises(HubError):
        download_track(path, len(data), workers=2, bandwidth=ReplayBandwidth(replay_rate({})), chunk_size=3, open_range=open_range,
                       check=lambda: None, progress=lambda _: None)
    assert not path.exists()
    assert partial.read_bytes() in (data[:2], data[:5])
    if fault == "refused":
        assert requests == [2, 5]
    download_track(path, len(data), workers=2, bandwidth=ReplayBandwidth(replay_rate({})), chunk_size=3,
                   open_range=lambda start, end: io.BytesIO(data[start:end + 1]),
                   check=lambda: None, progress=lambda _: None)
    assert path.read_bytes() == data


def test_combined_delivery_cannot_exceed_150_megabits_per_second(tmp_path):
    assert replay_rate({}) == 18_750_000
    limiter = ReplayBandwidth(replay_rate({}))
    start = time.monotonic()
    for track in ("video", "audio"):
        download_track(tmp_path / track, 3_750_000, workers=2, chunk_size=375_000, bandwidth=limiter,
                       open_range=lambda a,b: io.BytesIO(bytes(b-a+1)),
                       check=lambda: None, progress=lambda _: None)
    assert time.monotonic() - start >= 7_500_000 / 18_750_000


@pytest.mark.parametrize("value", [0, 5, True, "4"])
def test_invalid_count(value):
    with pytest.raises(HubError, match="YOUTUBE_ACCESS_CONFIG_INVALID"):
        worker_count({"youtube_download_range_workers": value})
