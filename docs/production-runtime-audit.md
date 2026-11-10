# Production release drift audit

`scripts/audit_compose_runtime.py` compares a private rendered Compose release
with live Docker state without recreating containers or invoking application
handlers. It is an operator check; it does not grant deployment authorization.

```sh
python3 scripts/audit_compose_runtime.py \
  --project-name lighthouse-hub \
  --manifest /opt/lighthouse-hub/compose.migrated.json \
  --manifest /opt/lighthouse-hub/compose.acquisition.json \
  --service api --service scheduler --service tweet-worker \
  --service system-worker --service subtitle-ingest \
  --service lighthouse-observer-mcp-v1 \
  --service lighthouse-observer-tunnel-v1 \
  --service replay-acquisition-worker \
  --output /absolute/private/new-runtime-audit.json
```

Choose an existing private output directory and a new absolute report path. The
tool refuses overwrite. Docker and Compose must already be installed. It performs
only Compose rendering, image/container inspection and `sha256sum` inside the
selected running containers. It does not install dependencies, call providers,
read cookies, copy media, start workers or alter databases. Exit 2 means a reported
comparison mismatch; a validation exception means the audit did not complete.
Do not treat either as permission to restart production automatically.

## Compared evidence

- Immutable image identity, running state, explicit command/entrypoint, UID,
  working directory and declared stdin/TTY options.
- Declared read-only root, memory/swap, CPU/PID limits, init, capability/security
  options, supplemental groups, restart policy and log rotation.
- Explicit environment values are compared in memory and never rendered in the
  report. Image-inherited environment is not compared. Compose null entrypoint
  means inherit; it is not an empty override.
- Bind source/destination and read-only mode, including file-backed Docker
  Secrets. Secret contents are never opened. Unsupported mount/secret types stop
  the audit rather than being silently omitted. Auto-named containers are found
  by unique Compose project/service labels.
- Read-only individual Python/SQL/MJS mounts under the hub's services, adapters
  and scheduler: host file SHA-256 must equal the live container's file hash.
  This detects a stale mounted inode after host-side file replacement. These
  executable paths and digests are recorded; profiles/config/media are not read.
- seccomp file content is compared with Docker's embedded JSON by parsed content.
  A path string is not expected to equal expanded JSON; a changed syscall policy
  still fails. No security setting is modified to satisfy this comparison.

The report explicitly excludes network/port declarations, healthcheck commands,
tmpfs contents, directory contents, full image source provenance and database or
provider behavior. A passing report is not complete application acceptance or a
fresh public-source rebuild. Keep private Compose manifests private: unlike this
report they contain runtime environment values.

## Actual checkpoint — 2026-11-10

All **eight active production services** matched the compared declarations.
All **33 individual code mounts / 30 distinct source files** matched their actual
container bytes, including the new System Worker discovery-health modules.
Production container identities remained unchanged. Ten local comparator tests
passed, covering resource drift, environment redaction, stale/unexpected mounts,
stopped containers, inherited entrypoints and seccomp content changes. The audit
itself ran on the real Ubuntu/Docker host; those ten tests ran locally on Windows.

Earlier incomplete attempts remain recorded: explicit `container_name` was absent
for Compose's auto-named acquisition service; a null inherited entrypoint required
handling; and seccomp path-vs-JSON representation initially reported a mismatch.
The final run compares the actual values and passes without changing production.
See [dated runtime evidence](releases/production-runtime-audit-20261110.json).

This file index supplements the qualified acquisition image and overlay archives.
The 30 indexed code files were subsequently captured in private
`/var/lib/lighthouse-migration/runtime-overlays-v1/code-overlays.tar` (389,120 bytes,
SHA-256 `056e40f609eac021374cdd772fe9e82a1be00505f94ac945063bb288f30c2ac6`).
Every file was checked against the audit hash, with before/after inode/size/time
checks. Exact allowlisted members were restored into an isolated directory and
rehashed. A separate local D: copy was transferred over the pinned SSH connection
and archive-hash verified. The manifest binds each file to its original path,
mode/owner and consumer image. The private archive is not committed or claimed safe
for redistribution; no directory, configuration, profile, credential or data
mount was included. This is file restore evidence, not an eight-service container
restore or a CloudBase backup.

These files do not prove full dependency closure. Any future migration must copy
and verify the required overlays as well as the pinned images and current private
configuration, and re-run this check on the destination. The System Worker
discovery-health patch is outside the application tree and must not be omitted
by an application-tree-only deployment copy.
