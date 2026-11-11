"""Crash a guarded two-reader child, recover its real SQLite lease and resume.

The owned extractor, session and HTTPS replies never contact a platform.
"""
import hashlib
import io
import json
import os
import re
import signal
from types import SimpleNamespace

import pytest

from adapters import replay_ranges, ytdlp_highest_v2 as engine, ytdlp_whole_v4 as adapter
from services.hub.config import Settings
from services.hub.locks import exclusive
from services.hub.replay_fifo_v4 import DOWNLOAD_JOB
from test_youtube_download_v4 import whole, fifo, tracks


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux process and queue-lock recovery")
@pytest.mark.timeout(45)
def test_killed_range_attempt_resumes_with_same_fifo_identity_and_ledger(whole, monkeypatch):
    from yt_dlp import YoutubeDL
    from yt_dlp.cookies import YoutubeDLCookieJar
    f = whole
    binding = f.service.resource_binding(f.job)
    folder = adapter.folder(f.s, f.job["job_id"])
    ticket = folder / "ticket.json"
    def write_ticket(job):
        engine.atomic_json(ticket, {"root": str(f.s.root), "job_id": job["job_id"],
                                    "lease_owner": job["lease_owner"], "binding": binding})
    write_ticket(f.job)
    monkeypatch.setattr(Settings, "load", lambda: f.s)
    # The isolated container uses a small tmpfs. Model the already-admitted disk reservation.
    monkeypatch.setattr(adapter.shutil, "disk_usage", lambda _: SimpleNamespace(free=100_000_000_000))
    monkeypatch.setattr(adapter.legacy, "load_youtube_session", lambda *a: YoutubeDLCookieJar())
    monkeypatch.setattr(YoutubeDL, "extract_info", lambda *a, **kw: f.info)
    bodies = {}
    for fmt, path in zip(f.info["formats"], f.inputs):
        fmt.update(has_drm=False, url="https://fixture.googlevideo.com/videoplayback?id=" + fmt["format_id"])
        bodies[fmt["url"]] = path.read_bytes()
    # Reduce range size to exercise three real requests on a one-second fixture.
    chunk = len(f.inputs[0].read_bytes()) // 3
    download = replay_ranges.download_track
    monkeypatch.setattr(replay_ranges, "download_track",
                        lambda *a, **kw: download(*a, **kw, chunk_size=chunk))
    parent_pid = os.getpid()
    resumed_offsets = []

    class Response(io.BytesIO):
        status = 206
        def getheaders(self):
            return list(self.headers.items())
        def getheader(self, key, default=None):
            return self.headers.get(key, default)

    def open_owned(url, hosts, *, headers, **kwargs):
        assert hosts == ["fixture.googlevideo.com"] and "Cookie" not in headers
        first, last = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", headers["Range"]).groups())
        video = url == f.info["formats"][0]["url"]
        if video and first == 2 * chunk and os.getpid() != parent_pid:
            os.kill(os.getpid(), signal.SIGKILL)
        if video and os.getpid() == parent_pid:
            resumed_offsets.append(first)
        body = bodies[url]
        response = Response(body[first:last+1])
        response.headers = {"Content-Range": f"bytes {first}-{last}/{len(body)}",
                            "Content-Length": str(last-first+1), "Content-Type": "application/octet-stream"}
        return SimpleNamespace(close=lambda: None), response

    monkeypatch.setattr(engine, "open_request", open_owned)
    pid = os.fork()
    if pid == 0:
        try:
            os._exit(adapter.child(ticket))
        except BaseException:
            os._exit(2)
    _, status = os.waitpid(pid, 0)
    if (folder / "download-result.json").exists():
        pytest.fail(json.loads((folder / "download-result.json").read_bytes()).get("error_code", "unexpected completion"))
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
    fmt = f.info["formats"][0]
    part = folder / ("stream-" + fmt["format_id"] + "." + fmt["ext"] + ".part")
    assert part.read_bytes() == bodies[fmt["url"]][:chunk]
    ledger = lambda: f.db.query("SELECT call_id,state FROM api_calls WHERE provider=? AND job_id=? ORDER BY rowid",
                                (engine.PROVIDER, f.job["job_id"]))
    original = ledger()
    assert len(original) == 3
    assert json.loads((folder / "progress.json").read_bytes())["http"]["requests"] == 3
    with exclusive(f.s.root / "data/locks/video"):
        assert f.db.recover("video", 4, all_processing=True, job_types=(DOWNLOAD_JOB,)) == 1
        new = f.db.claim("video", 180, 4, job_types=(DOWNLOAD_JOB,))
        assert new["job_id"] == f.job["job_id"] and new["attempts"] == f.job["attempts"] + 1
        assert new["lease_owner"] != f.job["lease_owner"] and f.service.resource_binding(new) == binding
        write_ticket(new)
        code = adapter.child(ticket)
        result = json.loads((folder / "download-result.json").read_bytes())
        assert code == 0, result.get("error_code")
    assert resumed_offsets[0] == chunk and 0 not in resumed_offsets
    assert ledger()[:3] == original and result["http"]["requests"] == len(ledger())
    assert [t["sha256"] for t in result["tracks"]] == [hashlib.sha256(p.read_bytes()).hexdigest() for p in f.inputs]
    assert not part.exists() and not f.db.query("SELECT * FROM publications")
