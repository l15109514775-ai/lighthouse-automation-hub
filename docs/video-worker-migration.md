# Video worker migration log

## Phase 0 — audit

Baseline 1272f86; audit committed as 5a9e396. Read-only production inventory,
source comparison, call index and SQLite schema recorded. No production changes.

## Phase 1 — task isolation

Store and all six custom claim/recover implementations accept an optional
`job_types` whitelist. Worker supplies exactly its handler keys. SQL combines
this whitelist with existing policy/platform/ordinal guards. Empty means none;
None preserves legacy direct callers. Forced recovery now requires actual
thread-local OS queue-lock ownership; ordinary expired recovery remains available.
No database migration, task reset, attempt rewrite or publishing policy change.

Validation: 76 tests passed on Windows/Python 3.13 (real temporary SQLite and OS
locks), including existing V1/V2/V3/V4/V5 behavior, duplicate claims, concurrent
workers, stale recovery, unknown publication protection and producer liveness.
Linux/Python 3.12/container acceptance remains pending. Existing recovery tests
now acquire the actual queue lock instead of bypassing the documented contract.
A dedicated test verifies missing, wrong-queue and other-thread ownership fails.

No old code deleted; no production container restarted. Packaging blockers found:
missing upstream gitlinks and local hub.toml; tests currently use a copy of the
public example. Upstream-lock records Windows newlines for vendor files; exact
compositor commit is present in the old local checkout but absent on GitHub.
These require explicit packaging treatment before a reproducible fresh build.
