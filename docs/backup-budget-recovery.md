# Backup budget deferral after migration

## Failure and scope

The first new-host full backup on 2026-11-10 failed before any external request.
Source-host usage already reserved 354,548,122 bytes of the shared 512,000,000-byte
daily allowance. The runtime correctly refused another full upload/readback, but
the host daily wrapper retained an `accept_failed` pointer requiring explicit
reconciliation. Waiting until tomorrow alone would not remove that guard.

`scripts/ops/cloudbase_backup_budget_recovery.py` adds two scoped behaviors to
the existing daily entry. No adapter, credentials, quota, archive format, source
retention, publication jobs or ledger records are modified:

1. Read the policy/ledger before creating a new archive task. Require headroom
   for the existing seven-request plan at the configured maximum archive size.
   Insufficient headroom returns `budget_deferred`, with no new operation or
   provider call. This is not a successful backup or a new usage reservation.
2. A failed daily can start a successor only with a root-owned attestation binding
   its exact failed history/pointer bytes to the exact failed operation. The
   operation must have `CLOUDBASE_BACKUP_BUDGET_EXCEEDED`, no receipt, no plan and
   zero requests of any state. No preparing/uploading/unknown operation may exist.
   Its timestamps must fall inside the daily's interval. A local packed archive
   may exist because packing precedes preflight; this helper never deletes it.

Unattested/changed failures and any remote intent remain stopped. Verified cleanup
resumes unchanged even with exhausted network budget. `accept_started` and
`reconciliation_required` retain the old guard. No reservations are refunded.

## Proof and audit

The private root-owned directory is
`/var/lib/lighthouse-backup-ops/retention-v2b/daily-state/budget-recovery/`.
`<daily_id>.json` carries version `cloudbase-preupload-budget-proof-v1`, the daily
and operation IDs, exact daily-file SHA-256, canonical operation-record SHA-256,
and explicit zero-request/no-refund/no-success-claim fields. It is created by the
operator after inspecting real evidence; no automatic generic proof issuer was
installed. Failed history and all ledger records remain unchanged. A successor
uses the normal runner's new daily ID rather than relabeling the old failure.

`last-check.json` records the conservative headroom decision. It cannot substitute
for the existing captured-GET restore report, all-entry verification, SQLite
integrity/foreign-key checks or backup success.

## Compatibility and deployed evidence

The v2e entry loads the new helper by fixed SHA-256. Its existing live dependency
pins were preserved, rather than downgraded to older public historical values:

- Daily v2c: `1b34b95010bff2db36a1b4567011a8005263a45659cbd769e2ebcee9473bccb0`.
- Retention runner: `620ed6e8c2b3b94d19bca0e458d9aa6ea075f7c8b21f98c5a229e4e4affd2cfd`.
- Wrapper before: `86d1c800d3fe18828a651eaea7eb94b386a1c9333c4a0bc17c54842da981e5bf`.
- Wrapper deployed: `efdec02339178c1cd50782107b737ed5c6e63827ad8a8b0653998749e8dd2bbb`.
- New helper: `3e4bac49187929e784b4c11171286fb55b37271a45aed776dc67f443f85261f9`.

Deployment held both daily locks and the runner's actual `observer.lock` inode,
verified no live backup sidecar and preserved the original wrapper privately.
The specific failed daily/operation proof was validated against real evidence.

The focused suite passed **36 tests on Windows and 36 in a real Linux Docker
container**, network disabled, read-only root, UID 10001, no live mounts. Coverage
includes real SQLite/WAL reads, a request added after attestation, changed proofs,
unknown operations, day/month headroom and unchanged cleanup/reconciliation.
The older remote-absent reconciliation selection also passed 50 local tests.

Actual host `--verify-only` and `--verify-budget` passed. The real systemd entry
returned `budget_deferred`, exit 0, invocation `285f578d4aa5437d8a2b7ebe15b92d3a`.
Full-row fingerprints of `operations`, `requests`, `plans`, and the failed daily
pointer were unchanged. Zero operations or requests were created. The 03:30
Asia/Shanghai timer remains enabled. This proves safe waiting, not a full backup.

An initial deployment guard rejected the locally packed archive because the first
validator required a null archive. It stopped before modifying production code or
proofs. The correction was tested with both absent and prepared local archives.

## Operation, rollback and remaining acceptance

Use the installed entry's `--verify-budget` for a read-only check. Do not repeatedly
invoke `--run-once`, raise limits, clear failures or issue generic proofs. The early
estimate follows the pinned runtime's seven-request plan, 300,000-byte/request
allowance and 8,192-byte maximum canary. A runtime plan change requires reviewing
this gate and its tests too; runtime per-request budget enforcement remains active.

Evidence and original wrapper are retained under
`/var/lib/lighthouse-migration/budget-recovery-deployment-v2/`. To roll back, first
verify no backup unit/sidecar is active and acquire the same three locks. Check
current/saved wrapper hashes against the after/before hashes above. Restore only
that wrapper atomically with its original ownership/mode. Leave proof, history,
ledger and archives intact. The old entry will safely refuse the failed pointer
again. This rollback is prepared, not executed against production.

Full upload, captured remote GET and restore acceptance still await the next
within-budget run. No remote success has been synthesized.

## Within-budget acceptance started — 2026-11-11

At the actual Asia/Shanghai day rollover, the read-only guard observed zero new
daily usage, sufficient unchanged daily/monthly headroom and no unresolved backup
operation. At 00:00:16 the existing systemd entry was started exactly once:
invocation `036100ba2f34469b8d955f901f519312`, daily
`bf4d9cce-2df7-463a-9aa2-86102b51ea40`, operation
`7e1f6b00-ead5-45ae-8ae4-be0e8cbf2b2e`. The regular 03:30 timer was not changed.

The first checkpoint showed **uploading**, with three completed requests and one
reserved/in-flight request. A live sidecar and increasing acknowledged bytes on
its established connection prove transport progress; a healthy process alone
would not. No restore report exists yet. The old budget failure and all usage
remain unchanged; no refund, limit increase or duplicate retry occurred.

Private request/start/observation records are under
`/var/lib/lighthouse-migration/within-budget-backup-acceptance-v1`; the sanitized
[public checkpoint](releases/backup-acceptance-20261111.json) is explicitly pending.
The unit ultimately timed out at 30 minutes. A single exact-object HEAD returned
404 and the ledger closed that operation as `resolved_remote_absent`.
The migrated unit now has the original 75-minute timeout. The subsequent
03:30 run deferred without dispatching a request. At 05:38 CST the ledger
had no unresolved transfer. The daily network balance was 322,146,851 bytes;
the next plan needs headroom for 386,648,864. The host reported November 12
at 03:30 CST as the next timer. [Reconciliation evidence](releases/backup-timeout-repair-20261111.json).

## Local restore and remaining external verification

An offline container on the new host used the deployed packer, consistent copies
of both SQLite databases and read-only operational inputs. All 1,608 declared
files matched their manifest. The restored database passed integrity and foreign
key checks, including the two completed replay publications and matching media
deletion receipts. The pack used 189,802,522 of the permitted 192,000,000 bytes.
It excludes original videos, deployment configuration and authenticated profiles.
Keep their separate recovery arrangements. [Local restore evidence](releases/backup-local-pack-restore-20261111.json).

The retained archive from the earlier timed-out operation referenced 153
distinct historical path/hash pairs in three prior archives. All sizes and
hashes matched the registered manifests. Those objects still need remote
retrieval to prove a full historical restore. Preserve the migration archives
until the new-host external recovery is verified. Use the captured GET from
a budget-permitted run for acceptance.
