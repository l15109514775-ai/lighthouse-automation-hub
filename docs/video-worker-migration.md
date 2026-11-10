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

## Phase 4 — Linux and Docker evidence

Linux/Python 3.12 targeted run: 89 passed, 1 failed in 124.11 seconds. The failure
was incomplete restoration of a deliberate readonly filesystem fault: SQLite
mirrored mode 0400 onto its WAL/SHM files, but the test restored only the main DB.
The test now closes its checkpoint reader and restores all its own fixture files;
the two real resource-fault tests then passed in 0.53 seconds. Production code
never chmods database files to defeat a permission error. All 90 scoped cases
therefore have passing results across these runs, not a fabricated single-run total.

Real Docker acceptance, including two separate containers sharing one SQLite DB
and physical video lock, passed for the effective-production candidate. The second
worker cannot claim while the first holds the lock, then completes once with one
attempt; unrelated historical processing rows remain unchanged. A fixture was
corrected to wait a bounded interval for ordinary idle lock contention, rather
than assume the first scheduling attempt always wins.

Candidate report: `/var/tmp/lighthouse-acquisition-b8kg57be/report.json`.
After explicit health-probe connection cleanup, the final candidate passed the
same acceptance at `/var/tmp/lighthouse-acquisition-lczk41l5/report.json`.
Final boot-identity binding passed at
`/var/tmp/lighthouse-acquisition-sudn2ryu/report.json`: the health nonce changes
after SIGKILL/restart, and another process's fresh heartbeats cannot satisfy this
instance's health probe. Legacy heartbeat names remain for observers.
Earlier runtime-image report: `/var/tmp/lighthouse-acquisition-c6__lq1i/report.json`.
Both cover profile/ports, UID, readonly mounts, Secret readability, capabilities,
limits, WAL, SIGKILL/restart and the rollback script. Networking was disabled;
synthetic configuration is not production authorization. The real production
containers and database were not used as fault-injection targets.

Full canonical-source regression run and baseline comparison are recorded in the
acceptance report. A sample historical archive-proof failure already
reproduces on baseline `1272f86`; its frozen code hashes are not rewritten here.

## Production preflight and migration gate

Real-content preflight used a private temporary DB, an existing authorized source
observation and copies of actual configuration. It stopped before network access:
the production acquisition switch is off, FIFO policy is unreadable by UID 10001,
acquisition permits one channel while FIFO/configuration expect two, and FIFO's
planning fingerprint does not match the planning file. Temporary copies were used
to isolate these errors; production policies, authorization references and bindings
were not edited. This is **not** a successful real YouTube download acceptance.

Subsequent explicit user authorization allowed one real attempt with coherent
test-only policy copies and an isolated DB/media directory. Official metadata
refresh succeeded; the single download-side planning request failed with
`YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED`. The test container was stopped with no retry,
no verified media and no publication. Production policy conflicts above remain;
test-only reconciliation does not authorize changing historical production bindings.
See the acceptance record for the exact scope and evidence.

See [video-worker-deployment-v2.md](video-worker-deployment-v2.md) for the ordered
policy/version reconciliation, source-overlay qualification and targeted startup.
Do not activate until release gates pass. No schema migration is required by this
refactor; explicit existing FIFO/V5 table initialization remains unchanged.

## Offline continuation: production policy staging

Read-only inspection on 2026-11-10 found 474 FIFO records: 14 completed, 458
excluded and two existing. None was an unstarted `registered` candidate eligible
for rebinding. Their old policy hashes are historical evidence, not an instruction
to reset or rewrite them. Recheck immediately before migration; this inventory is
a snapshot, not permission to act on future rows.

A private staged configuration bundle was prepared from byte-checked live files.
It fixes application-group readability, aligns the acquisition channel list to
the already-authorized two-channel runtime, and makes the FIFO planning digest
match the actual existing planning policy. All original authorization references
and dates are preserved. The whole-acquisition runtime switch remains **false**.
No staged file was installed over production, and no DB row was changed.

With those staged files, the actual candidate's validators passed in an isolated
network-disabled container as UID/GID 10001. Read-only preflight reports only
`YOUTUBE_WHOLE_DISABLED`; a separate in-memory switch was used solely to call policy
validators, never a worker/producer/adapter. This is static configuration
qualification, not real download or permission to activate. Private staging
manifest/validation evidence is retained in `work/video-worker-v2-audit/` outside
the public repository. Temporary host staging is not the authoritative live config.

Apply only after real-content, regression and image gates pass, while video
executors are stopped and their shared lock is held. Compare live file hashes
against the staging manifest first, capture a consistent DB backup and previous
file bytes/modes, and abort if config or active-task inventory changed. Install
only the reviewed policy bundle and service override; preserve terminal hashes,
attempts, results, publications and all unrelated files. Keep acquisition disabled
until the deliberate activation step. Rollback restores the prior config bytes
and modes while the new worker is stopped; never restore an old DB over live jobs.

## Historical cleanup and rollback scope

Deleted files: **none**. No historical adapter, payload/result format, maintenance
entry or recovery script has been proved safe to delete. Shared SQL scope/lock
ownership/receipt reading reduce duplication without removing versioned contracts.
The legacy V5 combined entry and original job types remain compatible.

`scripts/rollback_acquisition.py` is default-plan-only and checks service identity,
command, immutable image and data mount. `--apply` stops only that container. Actual
isolated Compose testing confirmed DB/media preservation and no restart of legacy
workers. It never restores a stale DB, rewrites attempts, changes publication
records or requeues unknown results. This verified additive rollback is distinct
from a still-unqualified production-wide image/config rollback.

## Frozen compatibility inputs restored

The original eight-case synthetic QA matrix is now in
`tests/fixtures/qa-benchmark-v1.json`, retaining PLAN_SHA
`f01fc2787eceabf198090b1a6e99a04a12d440abfa58b2b449e1be8ca82c5bf8`.
`qualify_replay_parts_retention_v5.py` was restored byte-for-byte from the original
checkout to existing M_SHA
`4e451f198c2f4e57534a71898d497693b57b0bf94aee1712453f0e1b266f70b3`.
Its historical public account UID 3747574504754572 is retained intentionally;
Git attributes preserve those reviewed bytes. No operator was run against live
data and no proof digest was updated. Other changed historical operators remain
unqualified rather than being rolled back to obsolete code.

Two genuine historical overlay archives are private external test inputs, selected
by an explicit directory and verified against their original hashes. They are not
new deployment packages and are not installed in production. Missing inputs fail
clearly instead of downloading or regenerating evidence. No old adapter was deleted.
