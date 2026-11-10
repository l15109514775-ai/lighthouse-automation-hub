# Per-source forwarding pause

`config/source-controls.json` is an operational control, separate from immutable
source policy and content authorization. An absent file preserves existing
behavior. A malformed or unreadable file fails closed with
`SOURCE_CONTROLS_INVALID`; install it atomically, readable by runtime UID/GID
10001 (production uses root:10001, mode 0640), without symlinks or group/world
write permissions. Do not rely on the process umask to grant group read.

Example of the owner-requested deployment on 2026-11-10:

```json
{
  "version": "source-controls-v1",
  "paused_x_handles": ["yuuhi_sendo"],
  "paused_youtube_channel_ids": ["UCuDY3ibSP2MFRgf7eo3cojg"],
  "reason": "Owner requested Yuuhi forwarding pause; keep Chise active"
}
```

X discovery skips paused handles before external requests. YouTube feed and live
monitoring skip paused channel IDs. Worker dispatch checks primary tweet authors
and publication parents, and resolves YouTube job identities through existing
metadata. A paused queued job enters `awaiting_review` with
`SOURCE_FORWARDING_PAUSED`; missing identity fails closed. Read-only replay
publication lookup remains available. Quoted authors do not replace the primary
author when deciding whether a tweet is paused. An already executing handler is
not cancelled by editing this file; deploy or pause at a safe checkpoint.

No historical records, attempts, publication receipts, authorization policy,
deduplication keys or queues are deleted. Resume only on an owner request by
removing the corresponding pause entries. Previously held jobs require separate
review; removing the pause must not imply permission to flood old content.

## Deployed evidence

The three production Worker implementations differ. Their original files were
preserved and patched with the small dispatch/discovery insertions independently,
then mounted read-only from `/usr/local/lib/lighthouse-source-controls`. The
core and acquisition Compose manifests and boot manifests retain these mounts.
Existing image identities, secrets, resource limits and other services were not
changed. Overlay hashes and per-role test logs are stored with that directory's
manifest. Private rollback copies and reports are under
`/var/lib/lighthouse-migration/source-pause-deployment`.

- Five focused tests passed locally; the same five passed in each of three
  actual Linux Docker runtime variants (15 executions, synthetic inputs,
  network disabled). These are not real YouTube download tests.
- Live probes confirmed Yuuhi paused and Chise enabled in all three services.
- The initial install was affected by umask 077, producing mode 0600. Three new
  jobs stopped before handlers with `SOURCE_CONTROLS_INVALID`. Mode was corrected
  to 0640. Each exact job was verified to have no API calls or publication records,
  then recovered in one audited transaction retaining attempts and old error
  events. All three completed at attempt 2; no configuration errors remain.
- The recovered normal X discovery completed: Yuuhi `owner_paused`, zero external
  requests; Chise `observed`, catchup complete, no new posts in that poll. This is
  live discovery evidence, not evidence of a new publication.
- No active forwarding jobs existed at the deployment checkpoint. No new replay
  download or publication was initiated for acceptance of this pause.

To roll back code, first quiesce the affected workers and validate manifest
copies against any later deployments. Restoring pre-pause code removes the pause
protection: it must not be done while the owner's pause remains in force unless
the affected forwarding pipelines stay stopped. Keep the pause file and reports
for audit; do not alter historical publication status or reset attempts.
