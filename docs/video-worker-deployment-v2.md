# Download Worker 2.0: controlled deployment and rollback

## Release gate (2026-11-10)

**Not approved for production activation yet.** The new service was exercised in
isolated, real Docker containers. The live database and existing containers were
not replaced. In particular, the old V5 publishing worker and subtitle-runner were
already stopped; this release does not silently start either one.

An actual preflight against copies of the effective production configuration found:

1. `replay-fifo-policy-v4.json` is root:root 0640, unreadable by UID/GID 10001.
2. `video-runtime-v5.toml` disables whole-replay acquisition.
3. The acquisition policy's allowed channel list differs from the effective
   runtime's `CHANNELS`.
4. The FIFO policy's planning fingerprint differs from the actual planning policy;
   the acquisition policy's planning fingerprint does match it.

The first two were changed **only in a private temporary test copy**, to identify
the subsequent conflicts. No production authorization, channel ID, policy digest,
timestamp, database row or attempts counter was changed. Real YouTube access did
not start. Resolve the intended policy set against genuine authorization records;
do not copy values from a test fixture or regenerate a receipt just to pass.

The public tree also contains anonymized account/channel values and differs from
effective production source overrides. Replacing production wholesale is unsafe.

## Dependencies and service boundaries

The unchanged API/scheduler receive tasks and discover sources. The generic video
worker owns its legacy handler types. V4 acquisition owns plan/download/readiness/
refresh only. V5 `--publication-only` owns its 32 publication and lookup handlers;
it skips acquisition producers. Legacy V5 `--publish` remains available for the
previous combined architecture, but must not run as a second acquisition owner.
Tweet/system service commands, limits and mounts are unchanged by this release.

All workers use the same host `data/hub.sqlite3` and `data/locks/video`. Keep all
five original bind mounts. Do not clone databases to connect production workers,
put SQLite on network storage, remove lock files or restore an old DB over live
tweet results. Temporary databases in acceptance tests are isolated fixtures only.

The new profile `youtube-acquisition` has no dependency that starts a publisher,
no exposed ports, no Bilibili credentials, read-only browser profile and a
read-only Secret at `/run/secrets/youtube-api-key`. It inherits logging rotation,
`unless-stopped`, UID/GID 10001, dropped capabilities and no-new-privileges.
Limits: 1400 MiB, 1.5 CPU, 160 PIDs; tmpfs 192 MiB, stop grace 30 seconds.
`DRY_RUN=true` is not a network-disable switch for authorized acquisition. Once
acquisition policies are enabled, this service can download media. Offline tests
must explicitly use `network_mode: none` and isolated data, as the test driver does.
The existing video/heavy locks serialize heavy work, rather than relying on memory
limits to arbitrate concurrency. Low space defers work; it does not delete media.

Python 3.12, Node 24.22.1, yt-dlp 2026.10.26, yt-dlp-ejs 0.8.0 and FFmpeg/FFprobe
were reused from the inspected image. No new production dependency or startup
upgrade was added. Existing APT/base-image inputs are not completely pinned;
fresh rebuild reproducibility is a known limitation. Qualify and retain immutable
image IDs/digests; a mutable tag alone is not a release record.

## Before migration

1. Re-read live container commands, image IDs, mount paths, restart policies and
   running/stopped state. Capture a **redacted** manifest; never export full Docker
   environment or secret contents. Compare to the audit, because another operator
   may have changed the deployment meanwhile.
2. Check current jobs, V4/V5 tables, unfinished publications, WAL/integrity and
   historical attempts without mutation. Use SQLite's online backup API for a
   consistent private recovery snapshot and verify it separately. Do not copy only
   the live `.sqlite3` file while ignoring its WAL. External backup remains governed
   by the existing backup workflow; do not resume/reset its jobs as part of this release.
3. Resolve the four policy conflicts above and the public-source/live-overlay
   difference. Any authorized policy revision needs an explicit versioned migration
   explaining effects on existing policy-bound jobs. Existing bindings cannot be
   silently recalculated. Preserve held/unknown/historical jobs.
4. Ensure all video executors sharing the DB include scoped Store/QueueView changes
   before enabling concurrent workers. Verify source-qualified retention/launch
   evidence again; old precise code hashes cannot certify changed code.
5. Reproduce the exact upstream lock. The original compositor commit `7b3905c` was
   found locally but unavailable from its public remote during this audit. Publish
   or otherwise supply that genuine commit before promising a fresh public build.
   Run `scripts/materialize_upstream.py` and `scripts/verify_upstream.py`; never
   update lock hashes to accept different bytes.

## Candidate qualification

Build a candidate from the verified existing image and actual source overlays,
applying the scoped Git patch with `git apply --check`. Preserve live configuration
and the X visual-retirement marker. Do not overlay the entire public source tree
onto this live image: it would restore retired files and replace protected source.

Run the public test suite from canonical Git archive bytes in a separate directory;
Windows checkout newlines can invalidate historical exact-hash tests. Baseline
failures must be distinguished from regressions using the same runtime and fixtures.

Run `scripts/test_acquisition_container.py --image QUALIFIED_TEST_IMAGE --compose
/absolute/path/compose.yaml` on a Linux Docker host. It creates its own directory
and Compose project, uses synthetic credentials with networking disabled, checks
hardening/leases/WAL, kills/restarts the container, and exercises the rollback
script. This is not a real YouTube download or publication test.

Then qualify the actual production candidate against the real policy set in a
separate acceptance workspace using authorized public content. Do not expose
cookies/keys, alter human-review receipts or publish anything in this test. A
platform refusal is a stopping condition, not a reason to change account, IP,
fingerprint or proxy.

## Targeted activation (only after all release gates pass)

Use a reviewed production-specific Compose override preserving current mount
paths. Set `LIGHTHOUSE_ACQUISITION_IMAGE` to the qualified immutable image and
`YOUTUBE_API_KEY_SECRET_FILE` to the existing restricted host file. A file-backed
Compose Secret uses host ownership: it must actually be readable by UID/GID 10001.
Do not grant world access, add root group membership or copy keys into `.env`.

Validate with `docker compose ... config --quiet`; avoid printing resolved secret
environment. Preflight using `--acquisition-only --check` before starting a daemon.
Then run **only**:

```sh
docker compose -f compose.yaml -f reviewed-production.yaml \
  --profile youtube-acquisition up -d --no-build --no-deps replay-acquisition-worker
```

Do not use blanket `up`, `down`, `restart` or rebuild all services. V5 separation
requires its own qualified `--publication-only` override; this document is not a
command to restart the currently stopped legacy publisher.

Inspect both boot-scoped health timestamps, lock contention, job types/attempts,
events and actual download receipts. Heartbeat alone does not prove download
success. A downloaded file needs FFprobe, actual stream/packet coverage, size,
duration, SHA-256 and bound `verified.json` checks. A receipt is not publication
approval. Confirm restart recovery without extra download/publish intent before
declaring production accepted.

## Rollback

This additive change requires **no schema/data migration**. Rollback stops only
the new service and retains all database rows, partial files, receipts and links.
It does not reset attempts, requeue unknown publications, restore a stale database,
delete media or start an old stopped worker.

```sh
python scripts/rollback_acquisition.py \
  --container EXACT_NEW_CONTAINER_ID \
  --expected-image-id sha256:RECORDED_64_HEX_IMAGE_ID \
  --expected-data-root /opt/lighthouse-hub/data
# Review the plan, then repeat with --apply.
```

The script validates Compose service label, exact command, immutable image ID and
data mount before stopping by immutable container ID. It refuses another worker
or a changed deployment. Its plan and apply paths were tested against a real
isolated Compose container; the historical foreign processing job remained byte-
equivalent. This proves the additive stop rollback, not an untested wholesale
image/database rollback of the production stack.
Record the expected image identity from that container's Docker `.Image` field;
do not substitute a mutable tag or a different build-stage digest.

If later replacing an existing V5 image, separately qualify restoring that exact
previous image plus its mount manifest. Revert only changed service configuration,
never live DB contents; leave previously stopped services stopped.
