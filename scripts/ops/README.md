# Root backup operations

**2026-11-04 current operator state:** `cloudbase_backup_daily_retention_v2e.py --run-once` is the systemd entry. The active transfer design now uses a 128,000,000 B archive cap, a 900-second per-request socket timeout, 3,600-second inner command limits, and a 75-minute systemd start limit supplied by `lighthouse-cloudbase-backup.service.d/90-transfer-timeout.conf`. The current `cf41b45a… upload_unknown` remains intentionally unreconciled until the audited one-HEAD recovery runs; no full upload was performed while bandwidth was congested. `reconcile_cloudbase_upload_unknown_v1.py` is plan-only by default and requires a reviewed plan SHA plus its installed source SHA before apply. See `docs/CLOUDBASE_BACKUP_REPAIR_20261104.md`.

Current entry as of 2026-11-02: `cloudbase_backup_daily_retention_v2b.py`, installed with `install_archive_retention_v2b.py`. The four adjacent v2b operator sources are byte-exact copies of the actually installed kit. Destructive core sources remain pinned to the actual 139-test release; the separate bounded autorun is pinned to the actual 9-test release. A real owner-instruction record and root-protected release are required. The daily service performs registration, persistent per-file plans/apply and the existing clone prune using the same verified remote GET. The 03:30 timer was preserved; its first natural v2b cleanup is pending. Historical v1e installation and initial acceptance details follow.

Root installation receipts are under `/var/lib/lighthouse-backup-ops/retention-v2b/install-history`. Application-root/config ancestor ownership was hardened without recursive changes; use sudo for protected configuration and deployment changes. `run_cloudbase_backup_retention_v2b.py --prepare-only` is an offline local reconstruction check after source cleanup; it does not upload or perform a remote GET. Preserve the archive-source index and old archive references. Actual deployment and reboot evidence is recorded in `DEPLOYMENT_REPORT.md`.

This folder preserves the exact operator programs used for the isolated CloudBase backup sidecar. It contains no credentials or bulk server evidence. Historical v1d programs remain available for audit; the active installer and pins are v1e. The business component is frozen at `298ac4b0c0dbdfc60c27a3890ed914b71ef6d10c`, using image `sha256:6592ebce93025293ac37e8c990f0f09daa639a0ed95354120a22371d2e9b6d55`; that image is the verified base plus the documented nine-file overlay, not this entire Git tree.

## Install without enabling

Copy the current five Python operator files (`run_cloudbase_backup_sidecar_v1e.py`, `accept_cloudbase_backup_capture_v1.py`, `prune_cloudbase_verified_clones_v1.py`, `cloudbase_backup_daily_v1.py`, `install_cloudbase_backup_v1e_units.py`) to the same root-reviewed staging directory on the server. The installer checks the fixed image, the actual Ubuntu 113/0/0/0 no-OOM attestation, the overlay commit, and four exact helper SHA256 pins. Its default is verify-only:

```sh
sudo python3 install_cloudbase_backup_v1e_units.py --verify-only
sudo python3 install_cloudbase_backup_v1e_units.py --install
```

`--install` puts root-executed code under `/usr/local/lib/lighthouse-backup-ops`, with root-owned parents and no group/other write permission. Root journals live under `/var/lib/lighthouse-backup-ops`. It installs and verifies the canonical service, failure unit and timer, then reloads systemd. It never starts or enables the timer. Existing active/enabled units cause a refusal; changed root-owned files are retained in install-history. The service has a 30-minute limit and runs daily at 03:30 Asia/Shanghai (19:30 UTC on the preceding day), with `Persistent=true`.

The sidecar keeps application UID10001, read-only data/media/logs/config mounts, masked browser profiles, resource limits, and an isolated read-only CloudBase credential file. Main application secrets are not mounted. Only backup state, current canary checks and backup staging are writable. A host SQLite readonly pin runs as UID10001; live WAL databases are never read with `immutable=1`.

## Verify and remove only the new copies

The daily entry point performs exactly one sidecar `--accept`: the existing seven budgeted requests include the live privacy checks, archive/manifest PUT and full GET. The already-budgeted archive GET is teed into a private restore area. Every archive member is hashed; the restored SQLite copy must pass integrity and foreign-key checks. The root orchestrator then runs the fixed pruner with `--dry-run`, followed by `--apply` only after successful independent validation.

Pruning is limited to this operation's three exact staging filenames plus this acceptance's captured archive and restored copies. Receipt, runtime result, restoration report, canary proof, ledger and root cleanup journal remain. It never removes directories recursively, active databases, original tweet/media/log files, profiles, historical raw backups or videos. Each unlink binds SHA, size, inode, device and mtime, with an fsynced intent and per-item result. A partially completed prune resumes from the journal without another upload. Unknown/interrupted acceptance, invalid state, modified files or expired verification stop for reconciliation.

For the first real acceptance, avoid a second seven-request upload on the same daily budget. Once its actual restoration report exists, adopt that precise result without network calls:

```sh
sudo python3 /usr/local/lib/lighthouse-backup-ops/cloudbase_backup_daily_v1.py --adopt-existing-acceptance /opt/lighthouse-hub/backups/cloudbase-restore-check/<actual-acceptance-UUID>/restoration-report.json
sudo systemctl start lighthouse-cloudbase-backup.service
```

Adoption performs a full local dry-run and records `accepted_pending_prune`. The service's next `--run-once` resumes only the validated cleanup. Enable the timer only after this native service run succeeds and its retained reports are checked. A fresh ordinary run later uses seven API requests; do not blindly start another run after an unknown result.

## Limits and actual initial tuning

The source template remains conservative: archive8,000,000B, daily20MB/month100MB, day50/month200 requests. Initial real exports exceeded that archive cap (8,428,664B), and 31 daily runs alone need217 requests. The operator therefore recorded a private initial override, without resetting the ledger: cloudbase and runner archive10,000,000B; day24,000,000B/month800,000,000B conservative network reservations; day50/month250 requests. Uncompressed50MB and disk-floor15GB remain. The recorded private configuration SHA is `8161c4764ea01290b001ce236df512defcfdb3fcc0f59fbeb69533a90a28e63b`; later changes require their own old/new SHA audit. One controlled initial acceptance retry may temporarily use a 36MB daily ceiling after exact HEAD404 reconciliation; retain all charged history and revert to 24MB after that test, before normal scheduling. No new bucket, plan or paid resource is created by these scripts.

The same persistent ledger counts previous/failed/unknown calls against the configured limits. Config reloads and reboots do not reset it. Estimates use the documented Shanghai list rates, not an invoice or verified package discount. Larger archives, growing task counts or exhausted budgets stop the backup; automatic incremental retention is outside this version.

This is a credential-excluded operational backup, not full account-profile recovery. Existing core daily raw backups continue independently; this workflow does not claim those older files have been migrated. Independent status and OnFailure files exist, but core MCP failure notifications are not integrated. Actual deployment, first remote restoration, service success and timer enablement must be recorded separately in DEPLOYMENT_REPORT.md.

## Owned tests

```sh
python -m pytest tests/ops/test_cloudbase_backup_daily_v1.py tests/ops/test_prune_cloudbase_verified_clones_v1.py
```

These tests use owned files and mocked child commands. They never invoke cleanup, credentials, network calls or real system services. Native service execution is a separate server acceptance step.
