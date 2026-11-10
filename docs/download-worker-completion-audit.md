# Download Worker 2.0 — current completion audit

Checkpoint: 2026-11-10, after commits `fb051f9` and `7a319b0`.
**Overall goal remains active.** This index separates current evidence from the
historical failed/blocked entries retained in the long acceptance log. It does
not turn a successful probe, file restore or running container into an end-to-end
delivery claim.

The owner subsequently scoped this work to new downloads, permitting obsolete
operators to be retired and closed-history failures to remain if they do not
affect current tasks. Historical records and publication semantics still must be
preserved. The new host is the sole production owner; the old host remains fenced.
Yuuhi's tweet/replay forwarding is paused; only Chise is active.

## Requirement-to-evidence review

| Requirement | Evidence and current disposition |
|---|---|
| Full call-site, entry/handler, state/schema and version audit before rewrite | `video-worker-audit.md`, audit index and architecture document; original baseline and phase sequence retained. Current effective-source comparison covers 160 files, classifying 20 differences rather than blindly overwriting private code. |
| Independent acquisition container, existing FIFO/adapter/database | Actual four-handler constructor and current Compose acceptance; separate acquisition unit, shared original SQLite, no second scheduler/DB. |
| SQL job-type claim/recovery isolation including QueueViews | Current-image 47-test selection includes disjoint two-connection claims, empty scopes, intersection semantics, foreign processing/pending preservation and old backlog not starving new V4 work. Assertions were inspected, not inferred from test filenames. |
| Shared video file lock covers recovery/claim/execute | Type-isolation tests require the exact queue/owning thread lock for forced recovery and refuse competing heavy handlers; actual two-container fixture uses shared DB/lock. SQLite lease and OS lock are separate checks. |
| Lease fencing and truthful worker/producer health | Current-image tests reject expired/reused-attempt owners and completion after heartbeat loss. Separate acquisition health passed live with zero restarts. Upstream official discovery now has its own persisted health result. |
| Existing states, attempts, idempotency and unknown-publication handling | Unknown reservation remains `publish_unknown` and cannot be claimed. Current all-column comparison preserves every original field in 1,324 video jobs, 92 publication records and 474 FIFO records; zero missing/changed rows. |
| Error taxonomy and bounded refusal handling | Stable file error/review assertions, invalid-error redaction, budget/HTTP classification and persistent access policy; real isolated test succeeded on authorized new host. Old restricted attempt was not repeated. Current access gate is open without a recorded refusal. |
| File paths, receipts, cancellation, FFprobe/SHA/content checks | Path/receipt tests and actual isolated 1080p60 acquisition with stream/duration/full-packet-coverage and delivery hashes. A receipt or a partial-file byte count alone is insufficient. |
| Crash boundaries, SQLite WAL/write-lock/unwritable filesystem/disk floor | Linux fault records and `test_download_crash_boundaries.py` cover result/verified receipt boundaries, SIGKILL, SQLite write lock and process/WAL crash. Actual Docker restart and real same-job download SIGKILL/resume also passed. No production disk was filled for testing. |
| Resource limits, UID10001, readonly mounts, Secrets, no public download port | Current isolated Compose checks and live acquisition inspection. Eight-service selected-field audit verifies declarations and code mounts; its explicit network/healthcheck exclusions remain exclusions. |
| Locked effective runtime and repeatable deployment | Immutable 0722 base archive passed truly empty Docker/containerd import, all 20 rootfs layer hashes and offline startup. Exact source-control overlay archive passed isolated restore and current-image Compose acceptance. No fresh full public Dockerfile rebuild is claimed. |
| Effective configuration compatibility and migration | Private policy bundle/identity mapping, actual config preflight, additive migration 018 and original-table comparison. Core discovery mismatch was subsequently found and fixed; normal scheduled official monitoring now reaches FIFO. |
| Preserve other workflows and authorization | Other effective service declarations and mounted code match; legacy video/subtitle runner remain stopped. Tweet discovery has natural scheduled evidence; no fake publication or new blanket authorization was used to manufacture acceptance. New-host real tweet publication is not claimed by these checks. |
| Four testing layers and regression classification | Unit/real temporary SQLite, actual Docker and simulated/authorized real-download evidence are distinguished. Historical evidence union remains 4,552 passed, 14 failed, 20 errors, 1 skipped, not one clean full-suite run. The 34 unresolved cases are individually classified as owner-deferred closed/retired scope; shared/current failures cannot use that deferral. |
| Safe historical cleanup | `code-cleanup.md`, removed-file inventory and deployed cleanup record. Current V1/V2/V3/V5 dependencies are retained where used by V4. No further deletion is inferred from version suffixes. |
| Verified rollback path and preserved recovery materials | Current-image isolated stop-only acquisition rollback passed. Forward migration and actual whole-host reboot passed. Reverse-host migration is a runbook, not an executed drill; it must carry the latest DB/media and never restart the stale old host. The 30 current individual code overlays have a private hashed archive and isolated file-restore proof. |
| Requested artifacts | Audit, architecture, migration/deployment/operations docs, Compose/Worker/Store changes, migrations, tests, `rollback_acquisition.py`, current reports and reverse-migration runbook exist. Private manifests/credentials are deliberately excluded from Git. |

Detailed sources: [acceptance history](video-worker-acceptance-v2.md),
[effective-source compatibility](video-worker-current-compatibility.md),
[current Compose proof](releases/acquisition-current-compose-acceptance.json),
[portable image/overlay proof](portable-image-verification.md),
[runtime drift audit](production-runtime-audit.md),
[full-field history comparison](releases/history-preservation-20261110.json),
[regression disposition](video-worker-regression-v2.json).

## Outstanding operational acceptance

1. **Natural production queue delivery.** Earlier Chise source `lWzXr6h0SyM`
   owns download job `7f56a3ec-a69e-4a2d-9c42-f3250b04e0b9`, processing at attempt 1.
   Its plan describes 30,596 seconds and an approximately 11.19 GB source-byte
   budget; that budget is not a downloaded percentage or exact final file size.
   The reported `_nadmR0WzqI` has a completed plan and waits behind it. Require the
   first job's terminal outcome, bound media verification/delivery receipt and
   correct FIFO projection, then observe the next task receiving the slot. Do not
   reorder/reset/duplicate either job to accelerate acceptance. On access denial,
   preserve the stop and report the actual code.
2. **New-host full external backup and restore.** The daily budget refusal had
   zero dispatched requests and has an exact guarded recovery proof. The actual
   safe-deferral entry passed; next normal timer is 2026-11-11 03:30 Asia/Shanghai.
   Require a budget-permitted real upload, captured remote GET, all-entry hashes
   and restored SQLite integrity/foreign-key results. A local code archive, old
   verified backup, canary or enabled timer is not this evidence. Preserve errors,
   reservations and the unknown-upload guard. Do not raise budgets or create
   repeated speculative backup operations.

3. **Owner-authorized cleanup after acceptance.** The subsequent owner instruction
   permits removing the inspected package cache, completed isolated test video,
   abandoned migration transport fragment and actually reclaimable build cache
   after acceptance. The three migration transport archives (about 7.3 GB) must
   additionally wait for the new-host full remote restore above. Recheck exact
   identities and live references immediately before removal; retain reports,
   verification receipts, databases, sessions and necessary rollback artifacts.
   No global Docker prune or retention-policy change is authorized by this item.
   The private dated disk review holds the exact candidate paths and sizes.

These acceptance and conditional cleanup items remain open; none is a reason to disable the already
qualified service or to repeat a healthy in-flight download. Fresh public-source
rebuild/redistribution and actual reverse-host execution remain explicitly
unproven capabilities, not hidden passes. Before final closure re-read current
states and compare them against this full scope, including any newly found defect.
