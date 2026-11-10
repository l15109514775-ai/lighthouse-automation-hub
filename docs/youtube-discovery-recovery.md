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
