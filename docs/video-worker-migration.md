# Video worker migration log

## Phase 0 — audit

Baseline 1272f86; audit committed as 5a9e396. Read-only production inventory,
source comparison, call index and SQLite schema recorded. No production changes.

## Phase 1 — task isolation

Store and all six custom claim/recover implementations accept an optional
`job_types` whitelist. Worker supplies exactly its handler keys. SQL combines
this whitelist with existing policy/platform/ordinal guards. Empty means none;
None preserves legacy direct callers. Forced recovery now requires actual
thread-local OS queue-lock ownership; ordinary expired recovery remains available.
No database migration, task reset, attempt rewrite or publishing policy change.

Validation: 76 tests passed on Windows/Python 3.13 (real temporary SQLite and OS
locks), including existing V1/V2/V3/V4/V5 behavior, duplicate claims, concurrent
workers, stale recovery, unknown publication protection and producer liveness.
Linux/Python 3.12/container acceptance remains pending. Existing recovery tests
now acquire the actual queue lock instead of bypassing the documented contract.
A dedicated test verifies missing, wrong-queue and other-thread ownership fails.

No old code deleted; no production container restarted. Packaging blockers found:
missing upstream gitlinks and local hub.toml; tests currently use a copy of the
public example. Upstream-lock records Windows newlines for vendor files; exact
compositor commit is present in the old local checkout but absent on GitHub.
These require explicit packaging treatment before a reproducible fresh build.


## Phase 2 — independent container

Added profile-gated acquisition service and dual-thread/boot-scoped health probe.
Restored the two original pinned gitlinks and added exact-byte upstream
materialization (281 files verified; no changed checksum accepted).

Local health/isolation/runtime checks: 34 passed. Linux Python 3.12 real Docker
run: 77 passed, 1 existing moderation daily-boundary test failed and remains to
be diagnosed before full acceptance. Initial tests on a 1 GiB tmpfs correctly
hit the 15 GB resource floor; rerun used an isolated host-disk temporary directory.

Actual Compose acceptance on 2026-11-10 passed: UID/GID, readonly root/profile,
Secret loading, capabilities/no-new-privileges, CPU/memory/PID limits, rotated
logs, WAL, both health timestamps, SIGKILL/restart and preservation of a foreign
processing job. Report: /var/tmp/lighthouse-acquisition-g09yf937/report.json on
the test host. Synthetic key, empty source inventory, network_mode:none; zero
API/publication rows. This was NOT a YouTube download acceptance.

A full public-source overlay initially failed the existing X-retirement marker:
it restores retired files and changes protected source bytes. The successful
runtime image overlays ONLY this refactor's worker modules on the verified
existing image; no retirement/authorization protection was disabled. Full
public-source image is kept solely for offline tests which instantiate temporary
Settings. Production containers and live database are unchanged.


## Phase 3 — lifecycle and file delivery

Added expired-lease/attempt fencing to heartbeat, checkpoint and finish; failed
renewal no longer advertises worker health or permits completion. Stable error
classification distinguishes database, disk, authorization, platform restriction,
identity, integrity, configuration, network and internal errors; arbitrary error
strings are rejected before persistence. No historical state/code is bulk changed.

Malformed, oversized, duplicate-key or symlink verification records now stop for
review. Whole-file hashing can check lease/policy/disk between chunks. Original
packet/quality checks and receipt formats remain. Existing partials are retained.

Added explicit V5 --publication-only mode. The legacy --publish combined mode is
unchanged. Independent V4 uses V5's existing read-only FIFO projection when group
tables exist, preventing false single-publication reconciliation holds. It does
not register publishing handlers or aggregate/submit groups.

Validation: targeted local suite 125 passed, 2 Linux-only tests skipped. This
includes actual synthetic FFmpeg media, crash after download-result and after
verified.json, recovery without a second download-helper call, SQLite write-lock
contention, corrupt receipt, failed renewal and scoped publication behavior.
Linux SIGKILL/WAL and full-suite acceptance remain Phase 4. A simulated-clock
fixture now advances QueueView and Store together, so expired-lease enforcement
is tested against one consistent clock.

The vendored upstream lock requires CRLF for files whose Git blobs may use LF;
materialization retains those original verified bytes. Its working tree can show
newline-only changes; no upstream source commit or lock checksum was changed.
