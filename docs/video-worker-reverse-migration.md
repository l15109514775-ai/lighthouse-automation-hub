# Reverse migration after production activation

## Scope and evidence

This is the runbook for moving the **current production state** to a recovery
host. It is not permission to boot the old source against its stale database.
The migration target began production writes on 2026-11-10. Therefore the
preactivation `scripts/restore_fenced_source.py` must refuse and must not be
patched to ignore `target_activated` or `ever-activated.json`.

Forward migration, reboot and isolated acquisition stop rollback have actual
evidence. This reverse-host procedure has **not been executed in production**.
Its existence does not prove that reverse migration or a full image downgrade
has passed acceptance. If only acquisition is broken, prefer stopping that
service while leaving healthy tweet/status services running.

On November 11 the fenced former production host received and restored two
current SQLite snapshots in a private temporary directory. The source used
the SQLite backup API under one read transaction per database. Schema, row
identities, typed values, integrity and foreign keys matched for all 61 Hub
tables (85,224 rows) and both retention tables (29 rows). Both completed
publications retained their URLs, attempts and deletion receipts. Eight files
referenced by the native publication observations matched their stored hashes.
The receiver checked its original database and WAL before and after, and the
source and receiver removed their temporary transfer material.
[Drill results](releases/fenced-standby-restore-20261111.json).

To compare two private SQLite copies locally, run
`python3 scripts/verify_recovery_database.py reference.sqlite3 restored.sqlite3`.
The verifier compares table fingerprints and also includes views, triggers and
indexes in its schema comparison. It exits with an error for an unsupported
virtual table or an inaccessible row ID. This drill used separate snapshot
times for the two databases and checked their common publication receipts.
A production takeover still requires writer fencing, consistent final media
and configuration, and the activation checks below.

## 1. Prepare a destination without enabling writers

- Record immutable image IDs, effective private Compose manifests, overlays,
  application UID/GID, restricted API-key files, policy hashes and systemd/cron
  state from the current owner. Keep credentials in private transport/storage.
  Run the [runtime drift audit](production-runtime-audit.md) and transfer the
  required external code overlays, including discovery-health; copying only the
  application tree omits files under `/usr/local/lib`. The audit indexes selected
  file mounts but does not itself copy them or cover every dependency.
- Validate destination disk capacity against the current DB, required media,
  partial downloads, receipts, profiles and retained recovery copies. Preserve
  the operational free-space floor; never delete unverified source material to
  make a recovery fit.
- Transfer and verify the exact current images. Prefer the same qualified image
  over an old image with unknown current-schema compatibility. Keep the
  destination owner marker absent and all producers/consumers/timers fenced.
- Copy the current retention release with its two referenced, root-owned
  evidence files under `config/retention-evidence-bffb95aadde36c50/`.
  The config bundle and previous release hashes are recorded in the
  [November 11 retention acceptance](releases/long-replay-retention-20261111.json).
  Verify the release using the effective publisher code before enabling its
  maintenance loop. Retain the SQLite deletion receipts for completed groups.
- Include the separately enabled publication service, its paired
  `compose.publication.json` and `/etc/lighthouse-hub/publication-enabled.json`,
  `lighthouse-publication.service`, and the restricted owner-display bridge.
  The current strict cover module is the image version; do not reapply the
  superseded cover-cache overlay. Preserve private-upload recovery journals,
  archived selection intents, prior cover assets and current-attempt provenance
  with their task directories. Those records prevent a copied task from being
  mistaken for an unused upload attempt. See
  [the qualified recovery](private-upload-recovery.md).
- The current publication service also mounts the three qualified draft-save
  acknowledgement sources under
  `/usr/local/lib/lighthouse-publication/draft-save-ack-v3`. Preserve their exact
  hashes and the current Compose/enable-marker pair from
  `releases/draft-save-ack-release-20261111.json`; the earlier memory-3g runtime
  archive alone predates this fix. Preserve preparation histories and consumed
  native intents. They are not disposable upload cache.
  The current private `publication-runtime-draft-save-ack-v3.tar` contains 31
  runtime/configuration files (SHA-256 `6185d92f1d1393a4452942009cf0dc37976ccde6d4499b2e1cef4923ced5e0b2`).
  All entries were restored and hashed in isolation, and the local private copy
  matched. This is a source/configuration bundle, not an image, DB/media backup
  or an executed reverse-host migration.
- Preserve the root-owned host-monitor library and its fixed systemd override
  from `releases/publication-host-monitor-20261111.json`. The application-tree
  copy is not the effective privileged entry. Verify ownership, parent-directory
  permissions and hashes before enabling the monitor on the destination.
- Check source-IP restrictions for YouTube API and CloudBase, profile usability,
  peer ingress firewall rules and the status tunnel. A fresh hostname or loaded
  image does not establish these permissions. Request any additional account
  authorization before activation; do not circumvent platform refusal.
- Use isolated copies for static/configuration tests. Never attach destination
  workers to a second live copy of the production DB.

## 2. Fence the current owner and obtain a consistent transfer

1. Save the originally running/stopped service allowlist and all restart/timer
   states. Pause external ingress callers such as Shanghai's subtitle poller.
2. Stop discovery and other producers first. Observe actual subprocesses,
   leases and publications. Let safe work drain where possible. If an upload was
   dispatched but its result is unknown, retain that unknown state and require
   reconciliation; do not classify it as safe-to-retry to speed migration.
3. Stop consumers and backup/maintenance executors, including the independent
   acquisition and publication units. Preserve a live upload's original task,
   intent and uncertainty if it cannot safely drain; never convert it to a fresh
   upload during migration. Fence Docker restart and host startup paths. Verify live
   process/container state, not just a marker file or old heartbeat.
4. Create a consistent SQLite snapshot through SQLite's backup API. Preserve
   original DB/WAL/SHM as recovery evidence. Do not copy a lone live database file
   or manually delete WAL. Run integrity and foreign-key checks on the snapshot.
5. Capture every table's schema/count and deterministic full-row fingerprint,
   jobs/attempts, publication IDs, source deduplication mappings, queue/FIFO
   ownership, audit/event records, unknown results and retention registrations.
   Record the snapshot hash and freeze time.
6. Transfer that snapshot and required current media/partial files/receipts,
   backup ledger and recovery history, private policy bindings and usable account
   sessions. Use authenticated pinned-host transport and hashes. Do not use a
   destructive rsync `--delete` against the recovery host or roll back from the
   old source's original snapshot.

The current owner stays fenced until the destination has been accepted or the
handoff has been explicitly abandoned before destination writes begin. Evidence
must link both hosts to the same transfer generation.

## 3. Verify the destination before its first write

- Recheck transfer hashes and ownership; preserve UID/GID 10001. Verify all
  original table fingerprints before running any additive migration. Compare
  preserved tables again afterward. A matching `jobs` count alone is inadequate.
- Run offline candidate preflight against read-only data/config. Verify Secrets
  are readable by the intended UID without printing them. Do not initialize an
  empty DB when the expected snapshot is missing.
- Verify job types, review/publish states, attempts and idempotency keys. Do not
  zero leases/attempts or batch-reset states. Expired acquisition leases are
  recovered only by the existing type-scoped owner under the shared video lock.
- Preserve the compatibility baseline of completed historical jobs. Preserve
  unfinished media and `verified.json` provenance; a copied receipt alone does
  not prove its referenced media exists or matches.
- Record an exact allowlist of services to activate. Previously stopped legacy
  services stay stopped. The source fence must be checked immediately before
  writing the new owner marker and starting the destination.

## 4. Activate one owner and validate

Start the reviewed core services and independently qualified acquisition unit.
Restore the publication unit only if it was enabled in the frozen allowlist and
its current authorization, private configuration pair, account and resources
validate. Never start both publication-only and the legacy combined publisher.
Check API readiness, worker and producer heartbeats, lock exclusivity, SQLite
integrity, scoped job recovery and absence of duplicate publication intent.
Switch external callers/tunnel only after the new endpoint is ready and then
resume those callers. Observe a real status query. Run external backup acceptance
only with adequate existing budget and no unresolved upload operation.

Before a reboot acceptance, record the boot ID, owner/enable markers and service
states, safely drain work and then reboot once. Verify the changed boot ID and
actual processes/health after reconnecting. Do not infer a failed reboot or
download from a transient SSH timeout and issue another operation.

## 5. Failure paths

- **Before destination writes:** leave it fenced, verify no worker started, and
  restore only the current source's originally active allowlist using its still
  current state. Retain the failed transfer and all evidence.
- **After destination writes:** it is now authoritative. Do not resume the former
  source. Fence both sides and repeat this procedure using the newest DB/media
  and publication records. Never merge SQLite replicas or discard new writes.
- **Unknown publication:** query the platform through the existing reconciliation
  flow. No blind repost, even if the destination UI shows no result yet.
- **Platform access denial:** stop the affected operation. Do not rotate hosts,
  accounts, fingerprints or proxies to continue a refused request.

Keep a dated acceptance record distinguishing copied/verified data, simulated
tests, actual container execution, real provider operations and untested steps.
