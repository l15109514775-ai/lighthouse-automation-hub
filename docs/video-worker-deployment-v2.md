# Download Worker 2.0: deployment and rollback

## Current release checkpoint — 2026-11-10

Production migrated to the new host and passed an actual whole-host reboot.
Eight containers run there, including the independent acquisition worker. The
old host is fenced. This is a production activation checkpoint, **not full goal
completion**. See [acceptance evidence](video-worker-acceptance-v2.md) and the
append-only [migration history](video-worker-migration.md).

The new host passed an isolated authorized real download, media verification and
SIGKILL/resume with the same task. A naturally arriving replay has not yet been
observed completing through the new production queue. No test publication was
performed. Full new-host external backup/restore is still outstanding: the one
post-cutover attempt stopped before any external request because the retained
daily network budget had insufficient headroom after the earlier source backup.
The [guarded budget recovery](backup-budget-recovery.md) is now deployed and its
real systemd deferral path passed. It preserves the failed record and all usage;
the next within-budget timer run still needs full restore acceptance. An enabled
timer alone is not proof of functioning backups.

Earlier statements that this image had not been transferred or acquisition was
still staged disabled describe the pre-cutover phase. They are superseded by
this guide. The original policy conflicts and their resolutions remain in the
audit/migration history, rather than being current activation instructions.

## Actual service boundaries

API/scheduler, tweet worker, system worker, subtitle ingest, observer MCP and
observer tunnel form the seven-service core allowlist. The eighth service,
`replay-acquisition-worker`, runs separately with `--acquisition-only`. The old
ordinary video worker and subtitle runner remain stopped. Do not enable legacy
V5 combined `--publish` as another acquisition owner. V5 publication-only support
is a separate capability, not a statement that that daemon is currently running.

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
8. Keep the old host fenced. Never rerun the earlier source final-sync/promotion
   scripts after target production writes have begun.

Current host startup uses `lighthouse-hub.service` for the seven-service allowlist
and `lighthouse-acquisition.service` for the independent worker. The latter
requires the current owner and acquisition-enabled markers under
`/etc/lighthouse-hub`. Use these units for the installed release. A generic
`docker compose up` may revive retired services and bypass the owner gate.

## Service rollback versus reverse migration

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
