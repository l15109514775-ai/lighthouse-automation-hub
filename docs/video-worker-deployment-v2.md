# Download Worker 2.0: deployment and rollback

## Current release checkpoint — 2026-11-11

Production migrated to the new host and passed an actual whole-host reboot.
Nine containers run there, including independent acquisition and publication workers. The
old host is fenced. This is a production activation checkpoint, **not full goal
completion**. See [acceptance evidence](video-worker-acceptance-v2.md) and the
append-only [migration history](video-worker-migration.md).

The latest [effective-source compatibility check](video-worker-current-compatibility.md)
compares 160 running source/dependency files against the public revision, records
all 20 differences, and verifies current policy bindings and the four-handler
acquisition boundary. Its 47 focused runtime tests supplement historical results;
they do not relabel the 34 deferred legacy failures as passing.

The new host passed an isolated authorized real download, media verification and
SIGKILL/resume with the same task. Both naturally discovered Chise downloads
completed; one also reached verified public publication as `BV12iWG6zEP7`.
The 11.2 GB source's bounded upload recovery is still in progress. These are
authorized real-content operations, not synthetic test publications.

Full new-host external backup/restore remains outstanding. The later within-budget
attempt hit an inherited 30-minute service timeout. The missing 75-minute drop-in
was restored, an exact-object HEAD proved absence, and the original reservations
and failed history were preserved. The [timeout repair record](releases/backup-timeout-repair-20261111.json)
supersedes the earlier zero-request failure checkpoint. Current preflight safely
defers under the unchanged daily budget; the next eligible period is November 12
CST, subject to monthly headroom. An enabled timer is not restore acceptance.

Earlier statements that this image had not been transferred or acquisition was
still staged disabled describe the pre-cutover phase. They are superseded by
this guide. The original policy conflicts and their resolutions remain in the
audit/migration history, rather than being current activation instructions.

## Actual service boundaries

API/scheduler, tweet worker, system worker, subtitle ingest, observer MCP and
observer tunnel form the seven-service core allowlist. The eighth service,
`replay-acquisition-worker`, runs separately with `--acquisition-only`. The old
ordinary video worker and subtitle runner remain stopped. Do not enable legacy
V5 combined `--publish` as another acquisition owner. The ninth service is the
separate `replay-publication-worker --publication-only`, enabled through its own
qualified Compose/marker pair and `lighthouse-publication.service`. It uses a
3 GiB memory ceiling after the preserved first large-upload OOM. It must not run
alongside the combined publisher. See the later restoration and recovery sections
for exact current artifacts and the executed configuration rollback.

All local workers share `data/hub.sqlite3` and `data/locks/video`. Never share
SQLite over a network filesystem or create per-worker DB replicas. Keep scoped
Store/QueueView claim and recovery filtering in every executor sharing this DB.
File locks cover recovery, claim and execution; SQLite leases remain a separate
requirement. No recovery may reset attempts or requeue unknown publications.

Acquisition has no public port or Bilibili credentials. It uses UID/GID 10001,
read-only root, dropped capabilities, no-new-privileges, read-only browser profile
and file-backed YouTube Secret. Limits are 1.5 CPU, 1400 MiB, 160 PIDs and 192 MiB
tmpfs, with logging rotation and `unless-stopped`. Low disk space defers work;
it does not authorize deleting media. `DRY_RUN=true` does **not** disable authorized
download networking. Offline tests explicitly use `network_mode: none`.

## Immutable release and private configuration

The qualified acquisition image is:

```
sha256:0722adba6834512161b932663951d76bb5994873ebd4456662cfa24c51aa7414
```

It reuses Python 3.12, Node 24.22.1, yt-dlp 2026.10.26, yt-dlp-ejs 0.8.0 and
FFmpeg/FFprobe from the inspected image. There is no dependency upgrade at
startup. Its captured base and hash-checked overlays are qualified; this is not
a claim of a fresh, bit-reproducible public Dockerfile build. Base/APT inputs
remain incompletely pinned. Keep private images private until their layers have
been audited for redistribution, even when the runtime secrets are bind-mounted.

A qualified private image archive is now available at
`/var/lib/lighthouse-migration/portable-acquisition-0722/image.tar` (838,561,280
bytes), SHA-256
`69bf7bd9dd7201b049cf89692e794a7f52275003cfcd94e6848bab2eae5f9f9d`.
After authenticated transfer, verify that hash before `docker image load --input
/absolute/private/path/image.tar`. Confirm the loaded image identity above and
rerun offline preflight before activation. Actual loading and a no-network
container probe passed on the existing host. A later import into empty Docker and
dedicated containerd stores also passed, followed by an offline, read-only,
UID 10001 container probe. See [the portable test procedure](portable-image-verification.md).
The companion private manifest records all 20 rootfs layer hashes and source
checkpoint `463675e`. It is a captured runtime, not the whole evolving public tree.

The current effective service additionally mounts two source-control modules and
an operational pause configuration. Their exact hashes and base binding are in
[`releases/acquisition-source-controls-v1.json`](releases/acquisition-source-controls-v1.json).
The private 20,480-byte `overlays.tar` is retained separately from the base image;
do not deploy the base alone and assume that it enforces the owner's pause.
Its isolated archive restoration, exact-file probe and five focused runtime tests
passed against the qualified image without live data mounts. Current package versions and
selected runtime hashes are in
[`releases/acquisition-runtime-0722.json`](releases/acquisition-runtime-0722.json).
This inventory is evidence about the captured runtime, not a new APT lockfile.

Actual private manifests are `/opt/lighthouse-hub/compose.migrated.json` and
`/opt/lighthouse-hub/compose.acquisition.json`. Do not replace them with generic
public examples. The migration preserved existing service commands/mounts and
restored required MCP `stdin_open`, plus reviewed policy/API-key bindings. Final
sync must preserve root:10001 / 0640 for application-readable policy files;
world readability or root group membership is not a repair.

The production data migration was **additive migration 018**, not zero schema
change. All original 59 tables matched their pre-activation row/count hashes;
post-activation all 1,324 historical video job rows remained unchanged. Do not
downgrade the DB during a service rollback. Completed links, review semantics,
attempts, deduplication and historical receipts are retained.

The backup helper graph has independently deployed pins that differ from older
public historical copies. Preserve the effective pinned graph during deployment;
do not overwrite it merely to make it match an older public script.

## Deployment procedure for a future host or update

1. Capture current redacted service identities, mount paths, images, policy hashes,
   owner marker and originally running/stopped state. Never print resolved Docker
   environment or export secrets into a public release artifact.
2. Export committed source with `scripts/export_locked_source.py --revision COMMIT
   --output /absolute/private/path/source.tar.gz`. It includes the original 281
   locked upstream files, rejects unsafe/tracked private inputs and never changes
   upstream locks. The original compositor Git objects are available locally but
   its missing public remote ref remains a fresh-clone limitation.
3. Qualify the exact immutable candidate in an isolated workspace. Do not overlay
   the entire anonymized public tree onto production or restore retired visual
   modules. Preserve current live overrides and the X visual-retirement marker.
4. Run `python -m services.hub.acquisition_preflight` in that image as UID/GID
   10001, network disabled, with production data/config read-only. Its exit 0
   proves only static configuration checks, not rights or real-source acceptance.
5. Run the relevant SQLite and Docker tests below. For a host migration, fence
   source producers before consumers, drain safely, use SQLite's backup API and
   copy the final consistent snapshot plus media/receipts. Compare all original
   table fingerprints after additive migration before allowing any target write.
6. Install owner-guarded units and private manifests. Verify the Secret is
   readable by UID/GID 10001, validate Compose without printing resolved settings,
   then activate only the reviewed service allowlist. Use the existing owner's
   authorization and do not start previously stopped publishers.
7. Check API readiness, both instance-bound acquisition heartbeats, locks, job
   types, attempts and actual media receipts. Reboot recovery needs an actual
   controlled reboot or container restart with evidence, not just restart policy.
   Also verify the scheduler/system-worker's own discovery configuration and a
   fresh completed official monitoring job. An enabled acquisition-local monitor
   flag cannot compensate for disabled core discovery; see
   [the recovered configuration mismatch](youtube-discovery-recovery.md).
8. Keep the old host fenced. Never rerun the earlier source final-sync/promotion
   scripts after target production writes have begun.

Current host startup uses `lighthouse-hub.service` for the seven-service allowlist
and `lighthouse-acquisition.service` for the independent worker. The latter
requires the current owner and acquisition-enabled markers under
`/etc/lighthouse-hub`. Use these units for the installed release. A generic
`docker compose up` may revive retired services and bypass the owner gate.

## Service rollback versus reverse migration

### Preserve backup systemd drop-ins during migration

Copy the existing `scripts/ops/lighthouse-cloudbase-backup.service.d/90-transfer-timeout.conf`
to `/etc/systemd/system/lighthouse-cloudbase-backup.service.d/` as root-owned mode
0644, alongside the inspected service definition. Run `systemctl daemon-reload`
and verify `systemctl show lighthouse-cloudbase-backup.service -p TimeoutStartUSec -p DropInPaths`.
The effective timeout must be **1h 15min**, not the base unit's 30min. Inner
transfer helpers remain bounded at 3600 seconds; budgets and the 03:30 timer are
unchanged. A copied base unit without its drop-in caused the November 11 real
backup to be killed mid-PUT. Installing this drop-in requires no container restart.

An interrupted upload must first be reconciled against its exact object using a
budgeted request. Do not restart it blindly. The budget recovery helper accepts
an `accept_started` pointer only with a root-owned `remote-absent-recovery/<daily-id>.json`
proof binding the unchanged daily history, terminal operation, original requests,
plan and budgeted HEAD 404. It otherwise retains the original interruption gate.
The normal entry can defer without starting a new backup when remaining budget
cannot cover the whole upload/readback plan. Neither deferral nor remote absence
is restore acceptance. Preserve the failed archive and recovery materials until
the separate full remote restore passes.

For the November 11 timeout repair, code rollback copies and the pre-transition
ledger are retained under the private `backup-timeout-repair-v1` evidence directory.
Rolling back helper code must restore its paired wrapper hash pin. Removing only
the newly installed timeout drop-in and reloading systemd restores the previous
unit configuration, but also restores its known premature-timeout risk. Do not
undo the proven absence record or restart a stale/unknown upload during rollback.

### Acquisition rollback

For an acquisition-only fault, first stop and disable its systemd startup unit
and preserve its original enable state in the maintenance record. Verify the
container identity before using the independently tested stop-only helper:

```sh
python scripts/rollback_acquisition.py \
  --container EXACT_CONTAINER_ID \
  --expected-image-id sha256:RECORDED_IMAGE_ID \
  --expected-data-root /opt/lighthouse-hub/data
# Review the plan; add --apply to stop this exact container.
```

The helper preserves every DB row and media file. It does not disable systemd
itself, restore an image/DB, or start any legacy worker. Address the boot unit
as well or a host reboot could reactivate acquisition. A previous image can only
be restored after compatibility with the current additive schema and payloads
has been qualified. Keep unknown publications held.

`restore_fenced_source.py` is only a **preactivation** rollback. It now correctly
refuses because the target has been activated. Returning production to another
host requires [reverse migration using current target data](video-worker-reverse-migration.md).
No production reverse-host migration has been executed or claimed tested.

## Test evidence and remaining limits

`scripts/test_acquisition_container.py --image QUALIFIED_TEST_IMAGE --compose
/absolute/path/compose.yaml` starts real isolated Docker containers with synthetic
credentials and networking disabled. It checks hardening, scoped recovery,
two-container locking/WAL, SIGKILL/restart and stop-only rollback. Separate real
content evidence verifies SHA-256, FFprobe, streams, duration, packet coverage
and bound delivery receipts. Neither grants publication approval.

For a captured release with source overlays, additionally pass
`--overlay-manifest /absolute/private/overlay/manifest.json --overlay-archive
/absolute/private/overlay/overlays.tar`. The archive and base binding are checked;
only the two executable modules are used with synthetic fixture policy. Production
pause restoration is tested independently. The actual 0722 image plus current
overlays passed all seven checks, including SIGKILL/restart and stop-only rollback;
see `releases/acquisition-current-compose-acceptance.json`.

Preserve browser seccomp and sandboxing in GUI compatibility tests. Use private
writable HOME/XDG tmpfs, 256 MiB shared memory and a short pytest basetemp to
avoid Chromium socket path overflow. Never mount live profiles for offline tests.

Historical archive tests require genuine registered files via
`LIGHTHOUSE_TEST_ARTIFACT_DIR`; missing inputs are not fabricated. The 34 recorded
closed-history/retired-visual failures are not claimed green. Per the owner's
scope decision, repair them only if a current execution path depends on them.
Current acquisition, shared DB/lease/lock and unknown-publication safety remain
mandatory. `Dockerfile.api-only` has static evidence only; it is not the qualified
acquisition image. Full goal-wide regression/provenance audit remains open.

## Independent publication restoration — 2026-11-11

`replay-publication-worker` is an optional `replay-publication` Compose profile.
It runs the existing V5 `--publication-only` entry and registers only the 32
positive ordinal publication types. It uses the same SQLite database, media,
source-control file and video/profile/resource locks. It has no YouTube secret,
no listening port and no new scheduler/database. The old combined V5 `--publish`
service must remain stopped during this split deployment.

Enabling this profile is a real sending operation. It requires the existing
`video-runtime-v5.toml`, publication policy, root-owned launch evidence and server
Bilibili session. It does not load `.env` or provide a DRY_RUN preview. A completed
download receipt alone cannot authorize a submission. For a qualified deployment,
start only this service with `docker compose --profile replay-publication up -d
--no-build --no-deps replay-publication-worker`; do not implicitly start unrelated
services. Production uses the private pinned manifest described below.

The migrated policy had only Chise in `allowed_channel_ids`, which broke both the
two-channel historical contract and the hash in the original launch evidence.
Only that field differed from the exact archived authorized policy. Restoring
the archived bytes retained the existing launch marker and owner authorization.
Yuuhi remains stopped by `source-controls.json` and the Worker guard; operational
pauses must not rewrite immutable authorization policies.

The shared startup loop now handles ordinary producer-lock contention without a
restart loop. Publication admission reports a resource wait without claiming an
acquisition task. Publisher health uses its own boot UUID and separate main and
producer heartbeats; acquisition heartbeats cannot make it healthy. This code is
currently overlaid only in the new publication service; the running acquisition
container was not restarted or modified for this release.

The actual private deployment is `/opt/lighthouse-hub/compose.publication.json`,
managed by `lighthouse-publication.service` with the existing production-owner
check. Its executable overlays are hash-recorded in the dated report. Four new
files live under `/usr/local/lib/lighthouse-publication`; other publisher files
reuse the previously captured private runtime. Local targeted tests passed 28;
the actual isolated Docker combination passed 67 overlapping/expanded tests.
The Docker tests used synthetic fixtures with explicit identity substitution,
not live publication. Independent paused-database startup and container restart
passed with new health identities and SQLite checks. Bilibili session validation
used a protected copy of the video profile and confirmed the intended account.

Rollback stops only the new service: `sudo systemctl disable --now
lighthouse-publication.service`. The same command is saved in
`/var/lib/lighthouse-migration/publication-restore-v1/stop-publication.sh` and its
shell syntax was checked. Do not restore an old database, reset attempts, enable
the combined publisher, or resend an unknown result. Production rollback has not
been executed. The private `publication-runtime.tar` contains 29 code/config/unit
files and passed an isolated file restore/hash comparison; it excludes browser
credentials and does not replace the full remote backup acceptance.

At the first healthy production observation, the existing download was still
processing. A later observation confirmed automatic group/child creation and
the first child entering `replay_part_uploading`, attempt 1, with the browser
live. The next download deferred at its verified stage on the shared resource
lock. Both publication links and the completed upload observation remain absent.
Upload completion, moderation and final links remain required acceptance. See
`releases/publication-restoration-20261111.json` for exact evidence and the earlier
test-capacity/observation-timeout limitations; no clean full-project suite is claimed.

### Follow-up: native upload memory limit

The first 11.2 GB upload exceeded the inherited 1400 MiB container ceiling.
Cgroup evidence recorded one OOM kill; its unknown-upload state and selection
intent remain intact. No blind retry or attempt reset was performed. Only the
publisher was raised to 3 GiB memory and memory+swap, in place without restart,
after checking the migrated 8 GiB host's available memory. The optional public
profile now exposes `LIGHTHOUSE_PUBLICATION_MEMORY_LIMIT` with that default.
Validate host capacity before enabling it on a smaller server. Actual Docker
Compose parsing verifies both limits; no extra production service was started
by that check.

A different, newly completed download then uploaded normally in the existing
queue. At epoch 1794334768 the official editor showed 3255.1/3255.1 MB (100%)
while still finalizing the upload. Neither this percentage nor the healthy
container proves submission, moderation or public availability. The original
large attempt still requires reconciliation before recovery.

The revised 29-file `publication-runtime-memory-3g.tar` bundle passed isolated
file restoration and hash checks, and an identical copy is stored off-server
on D:. It is not the pending full CloudBase backup/restore. See
`releases/publication-oom-20261111.json` for the preserved failure, limit change
and bundle hash. Existing test results remain applicable to the code; they do
not prove large-file browser upload success under the revised memory ceiling.

The subsequent 3255.1 MB upload completed and its automatic submission was
observed in the official manager as `BV12iWG6zEP7`, transcoding/under review.
The existing reconciliation policy requires public owner/source/player evidence,
so the job remains conservatively unknown while bounded read-only lookup runs.
Do not re-upload or reset it on that basis. Submission is now observed; public
availability is not yet accepted. Evidence: `releases/chise-submission-20261111.json`.

### Owner verification bridge compatibility

The root-owned display bridge previously accepted only the combined
`video-worker --publish` runtime. It now accepts either that exact pair or
`replay-publication-worker --publication-only`. Command/service cross-pairings
and acquisition services remain rejected. Its current-worker check considers
both service generations together and refuses concurrent publishers. Existing
image/container identity, application UID, mounts, no exposed ports, live lease,
challenge evidence, expiry and fixed loopback relay checks are unchanged.

Seventeen offline tests passed locally and in the actual qualified Docker image
with networking disabled and no credentials mounted. The candidate also
validated the real publisher identity before installation. No container restart,
credential read, relay opening, CAPTCHA interaction or job update occurred.
Actual future CAPTCHA handoff is not claimed tested. The installed hash and
off-server copy verification are recorded in
`releases/publication-owner-bridge-20261111.json`.

The prior bridge is retained as
`/var/lib/lighthouse-migration/publication-owner-bridge-v1/before.py`; the new
candidate and report are in the same protected directory and copied to D:.
To roll back only this helper, first ensure no owner handoff is active, verify
the installed file against `new_sha256` and the retained file against
`old_sha256` in that report, then atomically restore the retained bytes as
root with the existing file mode. Do not roll back SQLite or restart workers.
The older hash-pinned qualification script remains historical evidence and
must not be reused as an installer for this new helper. Tunnel invocation must
use the current installed hash. Production rollback and a live relay remain
unexecuted, explicitly separate from the file/hash and runtime checks.

### First migrated download-to-publication acceptance

The bounded follow-up lookup subsequently confirmed `BV12iWG6zEP7` publicly
accessible and completed its child, group and FIFO records automatically.
The upload attempt count remains one. The saved manager/public inventories and
public screenshot were re-read and their byte counts and SHA-256 references
matched the reconciliation receipt. Lookup selected no files, clicked no submit
button and performed one player seek. This proves the existing UI/player check,
not byte-for-byte equality with a Bilibili-side file (that flag remains false).
See `releases/chise-public-verification-20261111.json`. The separate 11.2 GB
failed upload and the full new-host CloudBase backup/restore are still pending;
this acceptance is not completion of the entire migration goal.

### Superseded recovery cover overlay — 2026-11-11

The idle publisher was recreated with one additional qualified overlay,
`services/hub/replay_video_cover.py`, to preserve verified cover reuse across an
explicit bounded private-upload recovery. No video/moderation task was processing
at stop; other container IDs were unchanged. The new publisher became healthy
before the held job was resumed. The exact code, manifest, prior manifest and
enable marker are retained under
`/var/lib/lighthouse-migration/publication-cover-cache-v1` and copied to D:.
The source hash, 19-test Linux result and actual unchanged cover-provenance hash
are in `releases/publication-cover-cache-20261111.json`.

The complete machine verifier and browser also require current-attempt cover
provenance; the cache exception alone was insufficient. It has been rolled back
by restoring the saved `compose-before.json` and `enabled-before.json` pair and
recreating only the idle publisher. The exact original cover source hash was
verified in the running container; other container IDs and job rows were
unchanged. This is an executed configuration rollback, not a database rollback.
The failed intermediate evidence is retained in the dated report.

The final operator recovery generates current-attempt cover provenance from the
verified cached original, archives the prior derived cover intact, and runs full
machine QA before releasing the held task. Production cover/machine/browser
checks remain unchanged. See `private-upload-recovery.md` for the two-stage
operation and tested process-kill recovery; no automatic extra upload is granted.

Final preflight and rollback evidence is in
`releases/publication-cover-rebind-20261111.json`. The original strict cover module
hash was checked in the replacement running container, the final recovery code
passed 19 Linux tests, and complete production machine verification succeeded
before the held task was released. The worker has claimed it normally; there is
no large-file upload completion claim in this checkpoint.
