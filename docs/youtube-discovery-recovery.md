# Official discovery recovery — 2026-11-10

The owner reported that `_nadmR0WzqI` had not started downloading. Direct inspection
found no matching `youtube_videos`, FIFO or source-specific job record. The most
recent official monitoring job predated this replay. A healthy acquisition worker
and empty queue were therefore insufficient evidence that channel discovery worked.

One budget-recorded official metadata request confirmed the video belongs to
Chise, is public, ended at `2026-11-09T10:39:33Z`, and lasts `PT1H39M27S`.
No download was forced for this diagnostic request.

## Cause and deployed correction

The scheduler and system worker load `config/hub.toml`, where
`youtube_live_monitor_enabled` was false. The independent acquisition worker loads
`config/video-runtime-v5.toml`, where it was true. The acquisition producer only
consumes persisted observations; enabling its local flag cannot enable the core
scheduler's channel monitor. The existing configuration preflight checked the
acquisition policy pair and missed this upstream dependency.

The core official monitor is now enabled at the existing five-minute interval.
RSS remains disabled; official `channels`, uploads-playlist and video metadata
requests perform discovery. Both core roles already held the correct authorized
new-server API key, so credentials were not the cause. No budget, account scope,
paid service or platform restriction was changed.

Only scheduler and system-worker were restarted. The private current/mirrored
Compose manifests and original core config were retained for rollback. Source
pause bytes were unchanged, so Yuuhi is skipped and Chise remains enabled. The
first precheck refused the existing list-form Compose environment before any
service or config mutation; the corrected deployment preserved that representation.
No historical jobs, attempts, publication records or policy hashes were reset.

The first natural monitor job after activation completed. It inserted the reported
video as ended and the FIFO producer registered it without a manual queue insert.
An earlier Chise replay, `lWzXr6h0SyM`, reached `youtube_whole_acquiring` with no
error. `_nadmR0WzqI` completed its plan at attempt 1 and waits for the shared heavy
download slot. Its legacy `video_process` discovery row is not a second download;
the stopped legacy video worker was not enabled.

These are point-in-time observations. See
[the dated queue and deployment evidence](releases/youtube-discovery-recovery-20261110.json).
They do not claim either new replay finished, or that Bilibili received a video.

## Required cutover/health acceptance

Future migration acceptance must cover the complete path:

1. Inspect the **actual scheduler and system-worker settings**, not only the
   acquisition container's config. Confirm official monitoring enabled, the
   intended interval, authorized credential availability and source pauses.
2. Observe a normal scheduled monitoring job complete with fresh `source_polls`
   and source records. A heartbeat, enabled timer or constructor-only check is
   not this evidence.
3. Confirm eligible new observations reach FIFO and the scoped planner, then a
   bounded download task. Do not reset history or bypass FIFO to manufacture proof.
4. On platform denial, retain the access gate; do not repeatedly probe or rotate
   infrastructure. Only a verified delivery can close actual-download acceptance.

Rollback of this specific enablement must not silently disable discovery while
advertising an operational automatic pipeline. Preserve current tasks and the
owner's pause; record an explicit discovery stop if the configuration is reverted.

## Persistent discovery health (deployed 2026-11-10)

`services/hub/discovery_health.py` reads the system worker's actual settings,
source pause policy, acquisition heartbeat and official per-channel `source_polls`.
It does not call YouTube, download media, reset jobs or change source controls.
The existing minute-based maintenance handler includes this evidence in its
persisted result and emits deduplicated `pipeline.stalled` events when attention
is required. This is an addition to maintenance, not a change to `/health`.

| Observation | Result |
|---|---|
| Acquisition heartbeat is fresh but official discovery is disabled | `YOUTUBE_DISCOVERY_DISABLED` |
| Active channel has no success within two configured intervals plus 60 seconds, or a future/invalid timestamp | `YOUTUBE_DISCOVERY_OBSERVATION_STALE` |
| Active channel records a provider error | `YOUTUBE_DISCOVERY_PROVIDER_ERROR` |
| Invalid source/configuration or unreadable pause policy | Stable configuration error; attention required |
| All channels intentionally paused | `owner_paused`; neither ready nor an outage |
| Every active channel has recent error-free observations | `ready` |

With the current five-minute poll interval, freshness expires after 660 seconds.
Paused Yuuhi is excluded; Chise must independently have fresh evidence. Arbitrary
stored error text is replaced with a fixed code, and invalid timestamps cannot
leak non-finite JSON values. A database failure is not converted into readiness.
This does not infer that queued downloads or publications have completed.

Production `maintenance.py` was first compared byte-for-byte with the pre-change
repository implementation. Only that module and the new helper were mounted
read-only into System Worker, using the existing pinned `a1337d4d…` image and
preserving every other service/configuration field. Snapshot and mirror live in
the private migration directory. Acquisition and all other running containers
kept their IDs. No database migration or historical task mutation was performed.

Validation: 19 local tests passed (9 discovery tests plus 10 existing MCP/event
tests). The 9 discovery tests also passed in a real isolated Linux container
using the production System Worker image and effective config/source-control
modules, network disabled, synthetic temporary SQLite, no production data or
credentials mounted. After deployment, normal scheduled maintenance job
`ddb1f1e6-3ebd-4fb3-860b-53a65c6f3bd6` completed and persisted Chise `ready` with
Yuuhi excluded. The concurrent download remained processing at attempt 1.
See [scoped evidence](releases/discovery-health-20261110.json).

### Patch-specific rollback

The private directory `/var/lib/lighthouse-migration/discovery-health-v1` retains
`compose.before.json`, candidate manifest, original module, test/deployment reports
and logs. These manifests contain runtime environment values: never publish them.

Before restoring a manifest, compare the current and mirrored manifest SHA-256
with `after_manifest_sha256` in `deployment.json`; also verify the snapshot hash
against `before_manifest_sha256`. If either differs, do not restore a stale whole
manifest: review intervening changes and remove only these two module mounts.
Wait for System Worker to be idle, stop only that service, atomically restore both
manifests with their original owner/mode, validate Compose, then recreate only
`system-worker` with `up -d --no-build --no-deps system-worker`. Verify running
state and unchanged acquisition container ID/lease. The original monitor-enabled
configuration and Yuuhi pause must remain in force. No rollback command may reset
jobs or stop acquisition. Automatic deployment-error rollback was prepared but
was not exercised against the live service because this deployment succeeded;
do not label this patch's rollback as a production rollback drill.
