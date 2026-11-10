# Download Worker 2.0 — current completion audit

Checkpoint: 2026-11-11, following qualified recovery commit `a925423`.
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

1. **Natural production download delivery passed; publication remains open.**
   Chise source `lWzXr6h0SyM`, job `7f56a3ec-a69e-4a2d-9c42-f3250b04e0b9`,
   completed at attempt 1. Independent SHA-256 and FFprobe checks verified the
   11,214,658,526-byte, 30,595.402-second 1080p60 H264/AAC deliverable and bound
   receipt/manifest/ledger. FIFO projected `download_completed`; `_nadmR0WzqI`
   naturally started job `4c783108-6065-4197-aab8-980a63e0e60a` at attempt 1.
   See [actual download delivery](releases/natural-production-delivery-20261111.json).
   **This is not Bilibili upload acceptance.** The owner explicitly reiterated
   that the workflow must continue through upload, result reconciliation and
   saved links. No publication exists for either source at this checkpoint.
   The migrated legacy video service remains disabled. A separate V5
   publication-only service is now deployed and healthy, with zero restarts at
   the recorded observation. The policy mismatch came from removing a channel
   from the immutable policy; restoring the exact previously authorized bytes
   made the unchanged launch evidence valid. The independent source-control
   file still pauses Yuuhi. No old jobs or attempts were reset. The account
   probe authenticated the intended Bilibili UID without sending content.
   Actual Docker tests passed 67 checks; isolated start/restart, distinct health
   instances and SQLite integrity passed. All four new deployed Python file
   hashes match the worktree. The 29-file release bundle passed isolated file
   restoration. See [publication restoration](releases/publication-restoration-20261111.json).
   **Upload completion remains pending:** the first source automatically created
   parent `bf4f4c9c-3dee-4975-b663-c4d7d7b87c7e` and child
   `82a818a5-6ac2-44e6-adbd-88a3a279e0f8`. That attempt subsequently hit the
   publisher's 1400 MiB memory limit (cgroup `oom_kill=1`). It is now held for
   reconciliation; its selection intent and attempt count are preserved. The
   parent was projected to the same review condition using the existing method.
   The new 8 GiB host had sufficient free memory to raise only this container's
   memory/swap ceiling to 3 GiB without restart. This does not establish that
   the original 11.2 GB upload is repaired; it has not been retried.
   The next acquisition completed, and its child
   `d8274e7f-38a8-4e4e-b9ea-2bc8a9159658` is performing a real native upload.
   At epoch 1794334768 the official editor displayed 100% of 3255.1 MB, but still
   showed the uploading state. This is not a completed submission or a BV link.
   See [OOM and resource evidence](releases/publication-oom-20261111.json).
   **Subsequent real submission:** the same child completed native upload,
   metadata/cover preparation and submission. Two official manager observations
   identified `BV12iWG6zEP7` with transcoding/moderation labels; the latest was
   2026-11-10T18:23:18.102Z. The existing lookup policy conservatively retains
   `publish_unknown` until full public owner/source/player verification; its
   read-only lookup completed and scheduled another check. No resend occurred.
   See [submission evidence](releases/chise-submission-20261111.json).
   **Public verification subsequently passed:** bounded lookup recorded
   `BV12iWG6zEP7` as publicly accessible and completed the child, group and FIFO
   automatically at the unchanged upload attempt count of one. The hash-referenced
   local evidence references (manager inventory, public inventory and screenshot)
   were read and rehashed. Lookup recorded zero file selections and submit clicks,
   and one player seek. This is public UI/player verification, not a remote
   Bilibili file checksum. See [public verification](releases/chise-public-verification-20261111.json).
   The first source's upload recovery remains open.
   A subsequent operator recovery verified the absence of local draft/save or
   public-submit evidence, archived the consumed private selection and original
   checkpoint, and admitted one new native upload attempt. It does not assert
   private remote blob absence. The normal claim counter advanced from one to
   two without resetting. That claim stopped before a browser launch on the
   old cover provenance's attempt binding. After the tested cover compatibility
   fix, the same unconsumed native attempt resumed; the ordinary claim counter
   is now three. No extra public submission was authorized by recovery.
   See [bounded recovery](private-upload-recovery.md) and its dated reports.
   Other containers remained unchanged; only the idle publisher was recreated
   to load the qualified cover module. Upload completion still needs evidence.
   **Final preflight correction:** machine/browser validation also binds cover
   provenance to the current attempt. The incomplete cache exception was rolled
   back to the original strict module with the saved Compose/marker pair; this
   exercised the actual configuration rollback without a database rollback.
   The final recovery tool stages a hold, archives old cover assets and generates
   current-attempt provenance from the verified cached original. Its 19 Linux
   tests include the real machine cover verifier and a process-kill boundary.
   The full production machine verifier then passed before release. The ordinary
   worker claimed attempt four (previous counts preserved) using the same second
   native attempt. This is not four uploads: the two intervening claims stopped
   before any browser/video selection. See
   [strict cover preflight and rollback](releases/publication-cover-rebind-20261111.json).
   Current large-file upload/submission completion remains pending. Actual editor
   frames at epochs 1794337770 and 1794337950 show the same native attempt moving
   from 7% to 14%, with 1547.3/10695.1 MB transferred in the latter observation.
   This confirms a live transfer, not submission or public acceptance. The
   parent still shows its earlier review projection while the child is uploading;
   do not use that stale aggregate alone to requeue or clear error history.
   Nine containers are running with zero restarts and the Yuuhi pauses intact.
   [Current transfer and budget evidence](releases/publication-transfer-observation-20261111.json)
   records the private screenshot hashes and read-only observation scope.
   The independent publisher's owner-display bridge compatibility was repaired
   and installed without restart. Both exact legacy/new runtime pairs are
   supported, concurrent publishers rejected, and 17 offline tests passed
   locally and in Docker. Actual runtime identity validation passed; no real
   CAPTCHA or relay was exercised. Evidence:
   [bridge compatibility](releases/publication-owner-bridge-20261111.json).
   Preserve completed media until the existing publication and
   retention gates succeed; do not treat a healthy publisher as upload acceptance.
2. **New-host full external backup and restore.** The daily budget refusal had
   zero dispatched requests and has an exact guarded recovery proof. The actual
   safe-deferral entry passed. After the real Asia/Shanghai day rollover, the
   unchanged budget allowed one normal-entry acceptance run at 2026-11-11 00:00:16.
   Invocation `036100ba2f34469b8d955f901f519312`, operation
   `7e1f6b00-ead5-45ae-8ae4-be0e8cbf2b2e`, subsequently stopped at the old 30-minute
   systemd timeout. The migrated host was missing the existing 75-minute drop-in;
   it is now installed and effective. One budgeted exact-object HEAD confirmed
   absence. The operation is `resolved_remote_absent`; all original reservations
   and the failed daily pointer/history remain. A root-attested recovery gate
   verifies the exact operation, original requests, plan and HEAD before allowing
   a successor. It does not treat a 404 as a successful backup or refund usage.
   Local and isolated Linux tests each passed 26 checks. The actual systemd entry
   safely deferred with zero new operations/requests because the unchanged daily
   budget cannot fit another full transfer plan. The 03:30 timer is unchanged;
   the next eligible daily period is November 12 CST, subject to monthly headroom.
   Full new-host upload/GET/restore remains unverified. See
   [timeout repair and reconciliation](releases/backup-timeout-repair-20261111.json).
   See [the dated attempt record](releases/backup-acceptance-20261111.json).
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
   The authorized disposable subset has now been removed after download
   acceptance: the isolated test MP4, abandoned `.part`, package cache and unused
   build cache. Known file/package allocation reclaimed was 3,632,848,896 bytes,
   plus 18.71 MB of build cache. Production images and running container IDs
   remained unchanged. See [cleanup evidence](releases/accepted-disposable-cleanup-20261111.json).
   Current production media and the full migration archives remain preserved.

These acceptance and conditional cleanup items remain open; none is a reason to disable the already
qualified service or to repeat a healthy in-flight download. Fresh public-source
rebuild/redistribution and actual reverse-host execution remain explicitly
unproven capabilities, not hidden passes. Before final closure re-read current
states and compare them against this full scope, including any newly found defect.
