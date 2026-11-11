# Video worker migration log

## Retiring superseded download releases after acceptance

The owner requested an observation period followed by joint confirmation before
deleting superseded versions. Keep the fallback until both the operator and
the owner explicitly confirm 2.0 has been reliable. The first successful
download and the passage of a calendar month do not trigger deletion.
For the first naturally discovered Chise replay, match the acquisition attempt
to the running image and deployment timeline, then verify the download receipt,
media checks and handoff to the publisher. The previous release downloaded
`2lILwnrnKTE` before cutover. Its publication outcome provides no download
acceptance for the new image.

Before removing a superseded artifact, check current container image IDs, boot
declarations, code mounts, active jobs and remaining code or recovery references.
At the November 11 post-cutover inspection, `replay-publication-worker` was still
running image `0722adba`. Preserve this shared image while the publisher uses it.
The new downloader also consumes FIFO V4 and the V4 adapter. Eligible cleanup
covers artifacts exclusive to retired releases. Keep publication mappings,
attempts, rollback receipts and task history available for the owner's on-demand
log reviews over the coming month.

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
Do not activate until release gates pass. The original isolation refactor did not
need a schema migration; the subsequent access-protection release requires the
additive migration 018 described in the new-server handoff. Existing FIFO/V5
initialization and historical task semantics remain unchanged.

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

## Historical recovery contract tests

`tests/fixtures/historical-recovery/README.md` records the exact old planner and
adapter commits/hashes. These `.source` files are read as data, never imported or
installed as workers. Existing operator source pins remain unchanged. Additional
negative tests prove today's planner/adapter are rejected, preserving rows,
attempts and partial media. Passing the old fixture is not authorization to run a
one-off historical recovery script on today's production state.

The retention builder similarly uses a private source-input archive assembled
from nine byte-matched Git objects plus the exact retained previous service unit.
Archive SHA-256 is
`070517adad0216df3eb9e2da5bf599bca41d0fe25b9a266f9c502044da898e13`.
This is newly packaged historical test input, not a regenerated acceptance proof.
The fixed old builder checks every reviewed source digest. Current backup fixes
were not reverted and a negative test confirms their old qualification is rejected.
No archive, source sample or old unit was deployed to the live application.

## New-server scope supersedes historical recovery compatibility

The owner's subsequent 2026-11-10 direction targets new downloads on the upcoming
server and explicitly removes the obligation to retain obsolete executable code.
[New-server handoff](new-server-download-handoff.md) defines the release gates and
the current dependencies that cannot safely be removed by filename alone.
Historical restoration work below is retained as an audit record, not further
work required before release. No obsolete-code deletion is claimed by this update.

## Closed historical work is not reset for this migration

Owner clarification on 2026-11-10: leave closed historical tasks and one-off
operators alone if they do not affect future jobs. The remaining old fixture
failures are listed, not waived as passing or allowed to expand repair scope.
Nine pending legacy rows and six review holds still exist in the database; preserve
their exact state/attempts. Actual final-image isolation tests prove their type/state
shape neither executes nor starves a newer V4 job. Do not reset or migrate these
rows merely to make a dashboard or old test green. This does not authorize ignoring
a failure in a shared Store/lock/lease component or a current acquisition path.

Historical deployment fixtures additionally use the private seven-source bundle
`historical-deployment-source-inputs.tar`, SHA-256
`7903c13bb927b3a03d6d3c5b3213365c298b871381ae35e8c9dc00b5795bc93d`.
Six source versions were located in original Git history; the native installer
matched its unchanged historical pin in the original private checkout. Member
bytes are bounded, uniquely addressed and independently SHA-256 checked; no
archive is extracted into runtime code. Current-source negative cases still refuse
old deployment approval. This test packaging is not a production installation.

## Migration-host configuration and database qualification

On 2026-11-10, all four pre-copied production policies matched the private
transfer bundle's reviewed before-hashes. The staged policies were installed
only in an isolated qualification directory, owned root:10001 with mode 0640.
Migration 018 was applied transactionally to an SQLite backup copy. All 59
pre-existing tables retained identical row counts and SHA-256 row digests,
including 21,123 jobs; integrity_check passed. No live database was modified.

The actual candidate's read-only preflight then reported only
`YOUTUBE_WHOLE_DISABLED`. A separate qualification-only configuration with that
switch enabled passed all configuration checks, preserving 474 terminal FIFO
records and their policy bindings. This did not start a worker. A stopped WAL
snapshot initially lacked readable sidecars for its read-only container mount;
the host's read-only inspection prepared the sidecars and the container check
passed. Do not solve this by opening a changing production database as immutable
or disabling the database checks.

The first new-host real acceptance stopped with `YOUTUBE_WHOLE_DISABLED` in the
download helper, after a successful official metadata refresh. Its download HTTP
counter was zero. The parent used `HUB_CONFIG=.../video-runtime-v5.toml`, but the
sanitized child environment dropped that path and loaded the default hub.toml.
The fix retains only a validated, regular configuration path inside HUB_ROOT;
API keys, credential file variables, proxies and Python injection variables stay
excluded. The original failed test records are retained; no production attempts
or publication records are reset to resume the acceptance exercise.

## New-host cutover findings (2026-11-10)

Real acquisition and same-job interruption recovery have now passed; see the
acceptance report for exact image, hashes, sample and test limits. Do not treat
this as permission to run both hosts' schedulers or publishers concurrently.

When reconstructing private Compose settings from `docker inspect`, Docker may
serialize a seccomp policy as `seccomp={...}`. This is not a portable Compose file
reference. Compare the parsed inline JSON to the copied, trusted profile, then
use that profile's absolute deployment path. Stop on any semantic difference;
do not replace it with `unconfined`. The migration generator now performs this
comparison, and the sandboxed browser started with the preserved profile.

The copied Bilibili profile required owner login on the new device. The owner
completed official QR login; the expected account UID was confirmed. Preserve
that newly authenticated private profile outside the final source-file delta,
then install it after the delta while both browser writers are stopped. Never
publish profile contents, copy them into an image, or overwrite this new session
with the source server's stale profile.

The Shanghai subtitle poller is active and its fetch process still targets the
old host's authenticated ingest endpoint. Move this caller only after the target
ingest service and source-restricted ingress have been verified; retain the old
endpoint until then. A local-only target binding is staging isolation, not a
working replacement for the remote caller. None of these checks has yet switched
production ownership or enabled legacy stopped workers.

## Ingest routing rehearsal (2026-11-10)

The target's 20 installed runtime roots passed a read-only checksum comparison
against the private pre-copy. This is not the final stopped-writer delta. A fixed
read-only rsync sender was added to the existing IP-restricted temporary export
key: it accepts only two exact sender commands over the same root-owned path
allowlist, rejects arbitrary commands, and does not enable deletion. Its successful
dry run reported 126,572,557 changed bytes out of 7,461,889,983 total bytes. The
initial protocol check failed because SSH `-n` discarded rsync's stdin; that flag
was removed from this transport, with the failed evidence retained.

For the existing Shanghai subtitle caller, the target ingest container remains
bound to 127.0.0.1:18081. A systemd socket proxy exposes the target IPv4 endpoint
through the host input firewall, limited to the existing Shanghai source IP.
This avoids relying on UFW to protect a Docker port published on all interfaces.
The socket units require UFW and are not enabled for boot during rehearsal.
Existing application token authentication is unchanged. The inherited HTTP
transport has not been represented as HTTPS.

The real Shanghai check initially exposed a pre-existing missing-header bug:
`HTTPMessage` returns None for a missing X-Claim, causing base64 decoding to raise
TypeError outside the existing exception guard. The one-line fix returns the
existing HTTP 400 response instead of dropping the connection. Five real HTTP
boundary cases passed locally and in a network-isolated Linux container, with
worker execution forbidden and no files generated. The target-only overlay has
before/after hashes and must be reapplied after the final source sync; the old
host has not been patched as part of this rehearsal.

The unchanged Shanghai check then returned 403 without credentials and 400 with
the existing valid token but missing parameters. The original host, which is not
in the ingress source allowlist, could not connect. No subtitle job, model call
or publication was submitted. The Shanghai poller remained active and its actual
endpoint still points to the original host. Caller update, final synchronization
and single-owner production activation remain outstanding.
# Host cutover: restore before first target activation

`scripts/restore_fenced_source.py` is a narrowly scoped source restoration tool.
It accepts the root-owned `source-frozen.json` captured before stopping source
writers and a fresh `lighthouse-target-quiescent-v1` proof from a coordinator
which has just inspected the target over pinned SSH. The proof must establish
that all target containers are stopped, no production owner record exists, and
the target has never been activated. Evidence older than 120 seconds is rejected.

The command defaults to dry-run; `--apply` restores original Docker restart
policies, only originally running containers, recorded unit enablement/activity,
and hash-checked saved cron entries. A scoped systemd drop-in replaces generic
`compose up` with that exact original active container list, preventing legacy
video/subtitle containers from being resurrected during restore or reboot.
Container identity drift and changed cron destinations stop restoration.

This tool does **not** restore databases or edit task states. Once target
production has started, stop both sides, preserve the target's authoritative
database/media/publication records and prepare a reverse migration; this
preactivation restoration command is no longer applicable.

Validation on 2026-11-10: four pure plan tests passed (including six target-proof
rejection variants). These are not a claim that a production host rollback has
been executed. Source freeze must preserve cron metadata before altering each
entry and disable cron producers before draining consumers.

## 2026-11-10 production cutover and whole-host reboot

The new host is now the sole production owner. The old host remains accessible
for recovery, but its application containers are stopped, Docker restart
policies set to `no`, Lighthouse timers/services disabled, and subtitle cron
entries fenced with their exact originals preserved. A fresh post-cutover SSH
check confirmed that fence. Do not restart the old stack against its stale DB.

The final stopped-source SQLite snapshot contained **21,372 jobs and 59 tables**,
SHA-256 `c22b6703819249b8db925f7d329b9a1d672857320abc2ce678135782195eedfb`.
Final rsync transferred 182,975,527 bytes in 727 files with no deletion. The
snapshot hash and every original table's count/sorted-row hash matched after
copy and additive migration 018. The four reviewed policies, dedicated API
Secret, corrected ingest boundary and owner-authenticated Bilibili profile were
installed after the delta. No human-review fields were fabricated.

An actual reboot changed boot ID from
`9a5124ec-8ccf-403f-b4bb-816e1d7b85c6` to
`29ae7770-338a-4f01-b381-b66bb408020b`. Eight containers started automatically:
API, scheduler, tweet worker, system worker, subtitle ingest, observer MCP,
observer tunnel and the independent replay acquisition worker. The old ordinary
video worker and subtitle runner remain stopped. Acquisition uses immutable
image `sha256:0722adba6834512161b932663951d76bb5994873ebd4456662cfa24c51aa7414`,
UID/GID 10001, read-only root filesystem, 1.5 CPU, 1400 MiB memory and no public
port. Its instance-bound producer and main heartbeat probe returned zero and
Docker health is healthy. Both `/health` and `/ready` returned HTTP 200.

Post-boot read-only comparison found **all 1,324 historical video job rows
unchanged, across all job columns**. Publication row count stayed 92; no synthetic
publication was submitted. At 12:58 UTC the new host had completed four normal
`x_discovery` and fifteen maintenance jobs without new errors. This does not
claim a new production tweet publication or a naturally arriving new replay has
already completed. The separate authorized real download and SIGKILL recovery
evidence above remains the real acquisition acceptance.

The Shanghai subtitle caller was paused only when no fetch subprocess existed,
then atomically changed to the new host after an authenticated HTTP boundary
check. Its original fetch file and service state were preserved. TAT invocation
`inv-0b12fagf23` confirmed `migrated_and_resumed`, active/enabled polling and the
expected HTTP 400 for deliberately missing job headers. No synthetic subtitle
job was submitted. The existing dot plugin returned a current `ready` snapshot
through the migrated tunnel, including the healthy acquisition container, with
zero provider requests. The temporary cross-host export key has been revoked;
ordinary operations SSH was rechecked successfully.

### Migration faults found and resolved

- Docker `top -eo comm` omitted the PID column required by Docker; adding
  `pid,comm` fixed the idle-receiver check before stopping any target service.
- A broad database filename glob also matched two historical backup copies.
  The promotion guard stopped, the inert partial move was restored, and the
  script was restricted to `hub.sqlite3`, `-wal`, and `-shm`. Those historical
  backup copies were not modified. Failed-attempt evidence is preserved.
- Final rsync restored `tweet-runtime-v4.toml` to root:root 0640, which the source
  process had already cached but the new API could not read. Only its group was
  changed to 10001, retaining contents and mode; future promotion does this too.
- The observer's original `OpenStdin=true` had been omitted in Compose and its
  process exited on EOF. `stdin_open: true` restores the required behavior. The
  runtime generator now preserves stdin, tty and supplemental groups. Docker's
  legacy `StdinOnce` has no direct Compose field; this difference is recorded,
  not asserted identical. A real dot query verified the migrated transport.

### Explicit limits and remaining acceptance

- Daily backup is active/enabled, next scheduled for **2026-11-11 03:30 Asia/Shanghai**.
  The last full verified backup displayed by dot still predates migration.
  New-host HEAD/GET and a tiny PUT/GET canary passed earlier; they are not a
  full archive restoration. New-host full backup/restore evidence is outstanding.
- The previously failed incremental-watch service and legacy replay-reconcile
  timer were not revived. Historical errors remain in the database/status.
- Four preactivation rollback plan tests passed on Windows and in a real
  network-disabled, read-only Linux container. This is not an executed production
  rollback. Since the target now has new writes, reverse migration must preserve
  the target's current DB, media and publication records. The preactivation
  `restore_fenced_source.py` path is no longer applicable.
- Runtime artifacts use a pinned captured base plus hash-checked changes and
  separately exported locked source. This is not a claim that the entire
  historical production stack has been freshly rebuilt from the public repo.
- No new clean full-suite run is claimed. Earlier targeted test results,
  intentionally retired compatibility failures and their scope remain recorded.
  Final goal-wide regression/reproducibility audit and reverse-migration
  documentation remain open; migration success alone does not close the goal.

Evidence on the new host: `/var/lib/lighthouse-migration/final-promotion.json`,
`final-database-fingerprints.json`, `production-reboot-before.json`,
`production-reboot-after.json`, `production-release-verification.json`,
`core-compatibility-fix/report.json` and `rollback-plan-tests/report.json`.
Local copies are under `work/zhiyin-migration-20261110/evidence-*`.

## Portable runtime follow-up — 2026-11-10

A separate empty Docker/containerd import and hardened offline container probe
passed for the captured 0722 acquisition base. Production containers were unchanged.
The first diagnostic run used a host-containerd connection and failed; it is not
counted as a clean-store success. See portable-image-verification.md for evidence,
isolation fixes, temporary storage cleanup and remaining limitations. Dependency
inventory and exact current source-control overlay hashes are versioned under
`docs/releases/`. No DB migration, old-job mutation, publisher activation or new
YouTube request accompanied these tests.
