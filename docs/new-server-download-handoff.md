# New-server download acceptance scope

Owner decision, 2026-11-10: the forthcoming server migration targets **new
download jobs**. Closed historical jobs do not require executable recovery
compatibility. This supersedes the original requirement to retain every old
recovery operator. It does not authorize deleting production records or bypassing
current media, identity, lease, resource or publication checks.

## Required runtime

Keep the existing V4 acquisition entry, FIFO/readiness/planning, current yt-dlp
adapters, FFmpeg/FFprobe verification, Store, locks, leases, health and events.
Acquisition owns only its four job types and never uploads to Bilibili. Publishing
remains a separate service with existing deduplication and reconciliation.

Do not migrate old one-off repair operators, test-upload programs, historical
overlay archives, frozen recovery fixtures or retired X visual workflows into a
new acquisition-only deployment merely to satisfy old tests. They may remain in
Git history and development evidence. Their diagnostics are not release gates
unless an actual current execution path uses them.

Version suffixes are not a deletion criterion. A read-only dependency inspection
still found these live edges:

- V4 acquisition imports V3 quality checks and V1 channel/source helpers.
- The V4 downloader uses the V2 download engine and common yt-dlp helpers.
- Shared OS locking loads the upstream observer store by its explicit file path.
- V4 FIFO synchronization uses the V5 multipart publication projection when its
  tables exist. That projection validates child registries and publication
  bindings; removing it would misinterpret current multipart results.

These are current dependencies, not obligations to revive old jobs. A reduced
runtime package must prove these paths remain available, including child-process
entry points and dynamically loaded files. The existing 46 MB source checkpoint
is a full source archive, **not** a qualified reduced runtime package.

## New server release gates

The subsequent [download access protection](youtube-download-access-policy.md)
adds migration 018 and durable request pacing / manual stops. Build a new image
with these changes; the older candidate's acceptance does not cover them. Keep
the current download service disabled until the new-host gates below pass.

1. Record the new server's OS, disk capacity, application UID, image digest and
   runtime versions. Transfer secrets separately; do not use public example
   channel IDs, target UID or authorization references as production settings.
2. Validate effective configuration and one acquisition owner. Keep existing
   publication/deduplication history available to the publishing service; no
   automatic resetting, re-enqueueing or copying an independently writable
   second task database.
3. Run the scoped SQLite and real-container tests on the new host: job-type
   isolation, shared lock, lease fencing, restart recovery, disk admission,
   verified delivery and stop-only rollback. Prior host results are evidence,
   not a substitute for host qualification.
4. Use one authorized new source for real acquisition. Check audio/video streams,
   duration, size, content, SHA-256 and the bound verification receipt. Validate
   interruption/recovery without creating another source job or publishing it.
5. Enable scheduling only after these gates pass. A platform refusal stops the
   real test with its stable error code; do not repeatedly retry or change
   accounts, IPs, proxies or fingerprints to defeat the restriction.

## Current evidence and remaining work

The four previously staged policy files are now preserved in a private, versioned
transfer bundle, alongside their exact pre-migration versions and a manifest.
See the deployment document's **Private policy transfer checkpoint**. This removes
dependence on the old server's temporary staging directory; it is not a complete
configuration backup, a lean runtime image or permission to start a download.

The existing immutable candidate passed isolated Compose lifecycle/rollback tests
and the 15-case type-isolation selection, including a newer job behind old rows.
See [the acceptance report](video-worker-acceptance-v2.md) for exact evidence.
No test in this scope update changes those results or adds a real-download pass.

Still required: qualify any subsequently reduced runtime artifact, configure and
test the new host, and complete the authorized real acquisition/recovery test.
The previous real-source attempt returned `YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED`;
it was not retried. The new server connection is not yet provided. No new worker
has been activated on production, and no live task, publication or secret has
been removed as part of this scope decision.
