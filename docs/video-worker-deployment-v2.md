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
timestamp, database row or attempts counter was changed. That initial preflight
made no network request. A later explicitly authorized single acquisition attempt
was refused by the platform and stopped; see the acceptance report. Resolve the
intended policy set against genuine authorization records;
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

## Offline checks and portable source

`python -m services.hub.acquisition_preflight` is a read-only inspection entry,
not a worker. Run it in the exact candidate image as UID/GID 10001, with production
data/config mounted read-only and networking disabled. It reports permission,
channel and planning-policy conflicts, plus aggregated historical task state.
Exit 2 means configuration is blocked; exit 0 means only that these checks passed.
`production_activation_allowed` remains false regardless: this cannot certify
real downloads, rights, publication approval or complete regression acceptance.

Missing/unreadable policy bindings are reported as unknown, not mismatches.
Completed/excluded/existing FIFO records retain their original policy hashes.
An active record with a different policy remains a reconciliation blocker. No
schema initialization, task recovery, automatic rebinding or policy writes occur.

For transfer to a future server, export a committed source snapshot locally:

```sh
python scripts/export_locked_source.py --revision COMMIT \
  --output /absolute/private/directory/lighthouse-source.tar.gz
```

This combines canonical committed source with the exact 281 locked upstream files,
using original Git objects already available locally. It exports no Git history,
uncommitted files, live configuration or browser profiles. It rejects unsafe paths
and tracked private configuration rather than silently packaging them. The archive
includes a file-hash manifest and preserves original upstream lock hashes. Missing
Git objects cause failure; no fetching or replacement of locks occurs.

Two actual exports of `08eeae0` were byte-identical (46,016,696 bytes, SHA-256
`065db9902cebd0be3e866c916a2e51f7d46822f8b4e600caa5c00d7b88ba0cfd`).
The transferred archive passed all 1,428 manifest file checks and the original
281-file upstream verifier in a real, network-disabled Linux container. This is
a **source checkpoint**, not a rebuilt or qualified production image. It predates
the later publisher-error fix; export the final reviewed commit for deployment.
The missing public submodule ref remains a fresh-clone issue, but this qualified
offline source path no longer depends on it. APT/base-image pinning remains open.

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

## Reproducing the offline compatibility tests

Keep the existing browser seccomp profile, UID 10001, readonly root,
cap_drop ALL and no-new-privileges. Provide writable private HOME/XDG directories
on tmpfs plus 256 MiB shared memory. Use a short pytest basetemp such as
`/testdata/t`; long nested pytest paths can exceed Chromium's Unix socket limit.
These settings are for isolated tests, not privileges to add to the download
service or a reason to disable Chromium sandboxing.

Historical archive tests accept `LIGHTHOUSE_TEST_ARTIFACT_DIR`, an absolute
non-symlink directory holding the exact archives registered in
`tests/ops/historical_artifacts.py`. The default is the original workspace outputs
directory; an independent checkout must explicitly supply the archived inputs.
The locator checks registered names, bounds and fixed SHA-256. It performs no
network access and never regenerates proofs. These legacy inputs are not shipped
in the public repository; missing them is a real reproducibility limitation.

`Dockerfile.api-only` was restored with its immutable-base requirement and existing
visual-retirement guard. It has static compatibility evidence only; it was not
built, used to replace a service, or presented as the new download image.
The two restored CloudBase templates are disabled examples with placeholder
resources and no credentials; they do not authorize backup access.

The later retention tests also require the registered private
`historical-retention-v2c-source-inputs.tar`. It contains only ten original reviewed
source/unit inputs, with provenance retained outside Git. It is not a deployable
release or a replacement for successful current-runtime qualification.

The isolated GUI smoke additionally requires an owned root-readonly
`/opt/lighthouse-hub/config/bilibili-video-handoff-v5.json` containing exactly
`version`, `enabled` and `ttl_seconds` (60 in the test), plus an empty writable
media tmpfs for UID/GID 10001. It starts actual GUI tools and checks loopback-only
ports inside the container; never map those ports onto the host or mount live
profiles for this test. The no-progress player test explicitly allows the real
observer deadline to elapse; all other generic test deadlines stay unchanged.

## Current release decision after the owner's scope clarification

Do not spend further release work repairing already-closed one-off task scripts or
retired visual fixtures unless a current call path depends on them. The 34 recorded
legacy failures are not claimed green, but no longer block this additive worker
by themselves. Shared DB/lock/lease safety, current acquisition/delivery and unknown
publication reconciliation remain mandatory. Preserve all nonterminal old records.

The final immutable candidate now has direct Compose restart/rollback proof; see
the final-image section in the acceptance record. This closes the earlier evidence
gap between the base candidate and the additive preflight/error-classifier image.
It does not qualify real downloads or authorize startup against live data.
Continue to keep the private staged acquisition switch false until the remaining
real-content and production-policy gates are satisfied. No repeat probe of the
restricted current server is part of this release plan.

## Current portable source checkpoint

For the forthcoming host migration, follow the owner's updated
[new-download-only acceptance scope](new-server-download-handoff.md). The full
source checkpoint below is archival build input, not an instruction to install
historical operators or retired workflows on the new host.

Commit `399f43deadfed9ac7a2de109eeed82d7c166c504` was exported after the owner's
closed-history scope clarification as
`lighthouse-download-worker-399f43d-source.tar.gz` (46,043,389 bytes), SHA-256
`cbaffd35057e3bb645d41a895b9913da4dc84a75d5094993d539f8143d839c0d`.
All manifest files and 281 locked upstream files were verified locally. The
archive is retained in the private D: task evidence directory and contains no
live configuration, credential files or Git history. It includes the final
preflight/error-classifier implementation, unlike the earlier 08eeae0 checkpoint.
This is a portable source artifact, not a fresh image build or real-download pass.
