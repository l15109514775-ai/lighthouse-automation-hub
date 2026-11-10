# Effective acquisition compatibility checkpoint — 2026-11-10

This checkpoint inspects the **currently running acquisition service**, including
its source-control bind mounts. It does not assume that an image tag or a public
Git revision describes every effective file. The immutable image remains
`sha256:0722adba6834512161b932663951d76bb5994873ebd4456662cfa24c51aa7414`.
The compared public revision is `5402be7`; no production code was changed by this
audit. Full file hashes, classifications, policy bindings and test observations
are recorded in [the versioned evidence](releases/acquisition-source-compatibility-20261110.json).

## Source comparison and migration implications

The read-only inventory covers 160 Python, SQL and dependency/project files under
services, adapters and scheduler. Of these, 140 match the public revision, allowing
only line-ending normalization; 20 differ.

| Difference | Files | Observed implication |
|---|---:|---|
| Production identity/display constants | 4 | FIFO, planning, private sample metadata and channel allowlist use the actual authorized account/channels rather than public placeholders. AST equality after the exact known identity/display mapping was verified. |
| Shared budget/HTTP/HTTPS versions | 3 | The acquisition image retains conservative gross budget accounting, no X receipt-credit hook, and a bounded 20-second HTTPS default. The reviewed YouTube callers do not request the newer configurable timeout. Current acquisition tests pass; this does not qualify these versions for the tweet or backup service. |
| Routes not registered as acquisition handlers | 13 | Older CLI, X, website, backup and legacy runtime modules remain in this captured image. The acquisition entry does not register their business handlers. Preserve them; this is not a proof that every possible dynamic import is unreachable or that deletion is safe. |

The 13 modules are named individually in the evidence file. Do not deploy the
entire public tree over the private image to remove these differences. In
particular, substituting public identity placeholders would break existing policy
bindings, while applying an unrelated X/backup update might require modules or
schema not present in this service. Other services use their own independently
qualified images and overlays.

## Actual current-runtime checks

An actual offline, read-only UID10001 container used the qualified image and the
current Worker/source-control overlays. Three focused suites passed **47 tests**:

- `tests/test_budget_http.py`: concurrent reservation limits, refusal before
  networking, provider status classification and preservation of uncertain costs.
- `tests/test_worker_type_isolation.py`: SQL type scopes, claim/recovery isolation,
  competing workers and the real video lock boundary.
- `tests/test_acquisition_recovery_faults.py`: lease fencing, heartbeat failure,
  stable file-error handling, retained partials and corrupt verification evidence.

These use synthetic jobs and real temporary SQLite/files. They do not claim a new
real YouTube download, a fresh full-suite pass, or a new publication. Production
container identities were unchanged. No live credentials were mounted.

A separate network-disabled container inspected current production config/data
through read-only mounts as UID10001. The preflight reported no blockers, SQLite
WAL, 1,324 historical video rows and 474 terminal FIFO rows. FIFO, acquisition and
planning policy digests remain coherent. No initialization, migration, attempt
reset or historical-job update was performed. This read-only observation counts
rows; the earlier migration evidence remains the separate all-column preservation
comparison.

The actual `build_worker(..., mode='acquisition_only')` constructor was then
checked with read-only production bindings. Its handler set is exactly:

```text
youtube_replay_download_v4
youtube_replay_plan_v4
youtube_replay_readiness_v4
youtube_replay_refresh_v4
```

Publication is disabled in that entry. The probe did not call a producer tick,
Worker execution or `Store.init`. Source controls still pause Yuuhi's X and
YouTube identities while leaving Chise enabled. A preflight's
`production_activation_allowed=false` is intentional: configuration validation
cannot grant deployment or content authorization.

## Regression disposition and remaining acceptance

The existing 34 unresolved historical cases remain failed/error in their original
record. The owner's closed-history scope defers six one-off capture/inventory
cases, 22 retired visual/browser cases and six disabled native multipart cases.
This checkpoint does not relabel them as passed, skip them, or add 47 to the
historical evidence-union count. New acquisition and shared-component regressions
must still be fixed when demonstrated.

The private captured base passed a fresh Docker/containerd import; its current
overlay archive separately passed restore verification. These prove a repeatable
captured release, not a fresh build of all public source. Base/APT build pinning,
complete cross-service source provenance and redistribution review are separate
limits. Service stop-only rollback has isolated test evidence; production
reverse-host migration remains documented, not executed.

New-host external backup upload/GET/restore still requires its next within-budget
run. A naturally arriving Chise replay has not yet completed through the new
production queue. Those observations must not be replaced by synthetic tests or
by retrying a platform-restricted source. This is a compatibility checkpoint,
not a declaration that the full goal is complete.

## Whole-running-release declaration check

The later [production runtime audit](production-runtime-audit.md) compared all
eight active services against both current private Compose manifests. Declared
runtime fields matched, as did 33 individual executable-file mounts (30 distinct
host source files), including discovery-health added after this acquisition source
checkpoint. No production service was changed. This closes the selected-field
manifest/overlay drift check; it is not full cross-service source provenance or
an archive/restore test of every service.

## Rebuilt source image on November 11

The production worker has since delivered two Chise recordings with confirmed
public links. The new source-built candidate at `35e695e` was tested separately
from that running worker. Its unmodified public identity constants caused the
actual acquisition constructor to return `REPLAY_FIFO_V4_POLICY_INVALID`
against the production policy. Keep the signed-off production policies intact.

The private source-binding tar contains seven hash-pinned modules from the
qualified release. Four carry channel, account and display identities. Three
preserve the acquisition service's existing budget and HTTP semantics. The
ordinary worker and source-control modules remain supplied by the existing
Compose release. The tar is 163,840 bytes and contains code only. Use the
[binding manifest](releases/rebuilt-acquisition-bindings-20261111.json)
to identify the exact candidate, per-file hashes and required archive.

With these bindings the candidate and production control returned the same
preflight and handler results on a consistent read-only operational database
copy. All 476 terminal FIFO entries were visible. An actual metadata row for
each channel exercised the source guard. It held Yuuhi and allowed Chise.
Both constructors registered just the four acquisition handlers.

An isolated Compose start used the restored tar, an empty synthetic work area
and a fake key. It passed worker lock contention, SIGKILL, restart, health and
stop-only rollback. The prior production image and its two current code mounts
then started against that same synthetic database and became healthy. All three
job rows matched in every column before and after. That database contained
zero publications, FIFO rows and API calls. The operational copy used by the
separate constructor test contained real history. The tests were offline.

The first seven-suite run passed 63 cases and recorded two fixture setup errors.
The crash tests used public example channel IDs with the real FIFO validator.
Mapping those two IDs in the private copy of the synthetic example allowed
eight suites, including the normal multipart worker runtime, to pass all
84 cases. No validation rule was changed. The formal source inventory found
24 changed common files and 18 candidate-only files. These tests establish
acquisition compatibility for the exercised paths. Other services still use
their own releases. [Test evidence](releases/rebuilt-acquisition-compatibility-20261111.json).

## Full public-image regression on November 11

The public candidate collected 4,732 cases. The first run was interrupted at
45% after its test container omitted the four registered historical inputs
and the browser seccomp profile. Its partial JUnit contains 1,955 passes, 84
errors, 128 failures and one skip. A synthetic browser fixture independently
reported a sandbox/namespace denial and passed with the qualified profile.
Supply the four genuine hash-checked archives read-only through
`LIGHTHOUSE_TEST_ARTIFACT_DIR`, the existing browser seccomp profile, a private
display, a short temporary path and a container init when reproducing the run.

One distinct failure was in the public CloudBase source. The backup probe
constructs the CDN host from the storage bucket; the prune-plan verifier had
instead constructed an unrelated example hostname from the environment ID.
The verifier now requires the exact CDN host of the bound bucket, along with
the existing COS host. A wrong-bucket observation with internally consistent
fixture hashes is rejected. The CLI test also installs its disabled template
with private permissions and separately checks rejection of a writable policy.

Seven historical operator modules passed all 184 tests with the four genuine
inputs. The pruning and retention selection passed 53 tests. The separate
archive runtime and one-off audit selection passed 180 tests. The production
backup unit and its qualified private files were untouched. These checks use
local fixtures and provide no CloudBase remote-read evidence.
[Recorded JUnit hashes and run boundaries](releases/source-regression-preconditions-20261111.json).

### Running the same public suite on another host

From a reviewed checkout on a Linux Docker host, pass a locally built image ID,
the absolute directory holding the four registered archives, and an existing
private results directory. Run as UID/GID 10001 with Docker access or as a host
operator who can set the scratch directory's ownership. Use the checkout's
example configuration and browser seccomp profile.

```sh
python3 scripts/run_public_regression.py --image "$image_id" \
  --archives "$archive_dir" --results-parent "$results_dir"
```

The command prints its private output directory immediately. Read `suite.log`
for progress, `run.json` for the terminal exit status and `work/full.xml` for
individual test outcomes. An interrupted or timed-out process can leave a
partial log. Retain it with the corresponding image ID. Production SQLite,
browser profiles, keys and Docker sockets are outside the test container.

Add `--collect-only` to verify the image's fixture pins, sandbox startup and
collection before executing the suite. On the migrated host the launcher
collected 4,732 cases in the unmodified source-built image. A changed archive
was rejected before starting Docker. The separate full regression uses three
reviewed source/test corrections mounted on that image; to reproduce those
corrections with this launcher, build the tested checkout into the image first.
Use one `--test tests/path/test_module.py` argument per file or pytest node
to isolate failures. On this host 22 selected rollback, type isolation and
browser upload tests passed. Running the retired two-source capture module
through the same launcher returned exit code 1. Six tests passed and the
two documented one-off cases failed with `SOURCE_REQUEST_NOT_SUPPORTED`.
[Launcher qualification](releases/public-regression-launcher-20261111.json).

### Numbered replay observation held for an active channel

Production replay `2lILwnrnKTE` reached the second readiness observation.
The queue identified it as `2lILwnrnKTE:readiness:2`. The source pause guard
looked up that entire key in `youtube_videos.video_id`, rejected it, and the
coordinator recorded a hold before media dispatch. The parent's legacy
`upload_pending` state at stage `readiness_hold_v4` does not indicate an upload.

For V4 readiness observations, the corrected guard verifies the exact numbered
key, the video plan's primary key, type, queue, policy, original video and channel
against the official metadata row. The existing handler separately validates
the wait, observation sequence, lease and full payload. Other YouTube task
types retain their existing lookup. On a consistent copy of the operational
database the incumbent guard reproduced the hold. The corrected guard in both
images accepted this child, rejected a different parent and retained the
owner's pause on the other channel.

The acquisition-only Compose change is staged on the migrated host. The
rollback declaration rendered identically to the active configuration. Sixteen
tests passed in an isolated copy of the incumbent runtime and 31 passed in
the patched candidate. The original readiness test module remains intact for
pinned historical attestations. The held job and its attempts, FIFO row and
review history were preserved. No provider call was made for this qualification.
[Case and release hashes](releases/readiness-child-channel-20261111.json).
