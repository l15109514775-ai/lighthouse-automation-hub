# Download Worker 2.0 — acceptance record, 2026-11-10

## Decision

**Candidate implemented; production activation is blocked. Refactor acceptance is
not complete.** Do not describe the synthetic Docker tests as a real YouTube
download or an operational production download service.

No live service was replaced/restarted and no live task was reset, deleted or
republished. The existing API remained healthy, tweet/scheduler/system containers
continued running, and the previously stopped video worker remained stopped.
No test published content. The final authorized probe made one official YouTube
metadata call and one download-side request; no paid service was enabled.

## Delivered

- Phase 0 architecture/call-site/schema/history audit: `video-worker-audit.md` and
  `video-worker-audit-index.md`; baseline `1272f86`.
- SQL job-type isolation across Store and every in-tree QueueView; Worker supplies
  its exact handler set for claim/recovery. Actual OS-lock ownership is required
  for forced recovery. Existing states and historical adapters remain.
- Expired-lease/attempt fencing for heartbeat/checkpoint/finish; stable error
  classification; bounded, no-follow receipt loading and cancellable SHA-256.
- Profile-gated acquisition service using the existing image, DB, FIFO and adapters;
  per-boot main/producer health, Secret, readonly profile and resource limits.
- Explicit V5 publication-only mode and V4 reuse of V5 group projection, so the
  split architecture does not misclassify historical multipart publication.
- Exact upstream gitlinks/materialization; no replacement lock checksums.
- Linux resource/crash tests, real Compose acceptance driver and scoped rollback
  script; architecture, migration and deployment instructions.

## Tested versus not tested

| Evidence | Result | Scope / limitation |
|---|---|---|
| Phase 1 local tests | 76 passed | Windows, real temporary SQLite/locks |
| Phase 2 local health/isolation | 34 passed | Not a Docker claim |
| Phase 3 local lifecycle/receipt suite | 125 passed, 2 skipped | Linux-only SIGKILL cases skipped locally |
| Linux focused suite | 89 passed, 1 fixture failure | Python 3.12; 124.11 s; readonly restoration failure investigated |
| Corrected Linux resource tests | 2 passed | Actual 1 MiB full tmpfs and actual unprivileged DB permissions; 0.53 s |
| Earlier daily-budget boundary failure | Single-case recheck passed | 1.63 s; original clock-sensitive result retained in logs |
| Rollback guard unit tests | 6 passed | Refuses changed service/command/image/mount; default plan only |
| Final health/rollback local recheck | 8 passed | Includes explicit health-probe connection close; 0.19 s |
| Boot-scoped health/runtime checks | 31 passed, plus 3 focused checks passed | Unique boot identity; other processes cannot mask a stalled producer |
| Legacy fixture correction, Linux | 136 passed | 130 download/monitor tests plus initial six preflight tests; 17.84 s |
| Final preflight/export/generation checks | 27 passed on Linux; 27 passed on Windows | 21 new cases plus six existing generation cases; Linux 2.79 s |
| Writable HOME/XDG browser recheck | 38 passed | Previously failing cases; real Chromium sandbox retained, no network; 259.21 s |
| Explicit publisher error compatibility | Passed | Local pytest plus actual final candidate; no browser launch or publication |
| Full canonical-source regression | 3,971 passed; 431 failed; 184 errors; 1 skipped | 4,587 cases, 2,214.57 s; not a release pass |
| Effective-production candidate Docker | Passed | Synthetic, network disabled; two containers sharing DB and video lock |
| SIGKILL and container restart | Passed | Isolated Docker plus Linux child/SQLite-process tests |
| Online DB backup/restore | Passed in Linux focused suite | Synthetic DB; interrupted WAL transaction rolls back; restored row matches |
| Real YouTube acquisition attempt | **Blocked by platform refusal** | One metadata refresh succeeded; one planning/download-side request returned YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED; no verified video |
| New production service activation | **Not performed** | Release gates not met |
| Production V5 publication-only operation | **Not accepted** | Implemented/tested synthetically; old stopped publisher not restarted |
| Fresh public Dockerfile build | **Not accepted** | Missing public compositor commit; mutable OS package inputs |
| Production-wide rollback | **Not accepted** | Only additive new-container stop rollback has been exercised |

The 90 Linux focused cases have passing evidence across the initial run and the
two-case correction; this is not reported as a single clean 90-case run. The
readonly fault fixture originally restored only the main DB, leaving SQLite's
derived WAL/SHM permissions readonly. Restoring all owned fixture files is correct
fault removal; no production code was allowed to bypass filesystem permissions.

## Full regression / baseline comparison

The canonical-source complete regression run finished with the totals above.
614 existing failed/error cases were compared against unchanged baseline `1272f86`
in the same runtime: 424 failed, 157 had setup errors, and 33 passed (662.321 s).
Thus 581 also failed on baseline; this alone does not prove identical root causes.
The added readonly-fixture failure separately passed after correct fault restoration.
Immutable historical verification hashes are not rewritten to make this release pass.

One confirmed compatibility-test change was the old deployment assertion that
Compose contained exactly one YouTube profile mount. It now proves the existing
mount count is preserved and the new acquisition service is entirely untouched
by that legacy deployment script. Its module plus the one-off tweet-age module
rerun yielded 41 passed and 1 unchanged historical source-attestation failure.
The five tweet-age setup errors from the full run passed in isolation; full-suite
order dependence remains a qualification concern, not evidence of a production fix.

Many browser failures came from the generic test container lacking the existing
browser seccomp profile. That profile matches the public source exactly. A real
representative Chromium test passed with that profile, cap_drop ALL,
no-new-privileges, readonly root and Chromium sandboxing still enabled. 290 affected
Node/compositor cases were rerun with the proper existing browser profile and
256 MiB shared memory: 210 passed, 60 failed and 20 had setup errors (1,291.017 s).
One separate compositor runtime case passed in 14.07 s. No security mechanism was
disabled. This correction is to
the browser test environment, not a change to the acquisition service's privileges.

Across the original 4,587 cases, the union of the full run and corrective targeted
runs is now 4,552 passing, 14 failing, 20 setup errors and one skipped. **This is not
a single clean full-suite run.** All 34 remaining cases also failed or errored on
baseline; none of the baseline-passing cases remains unresolved after the targeted
runs. This does not establish identical causes or remove suite-order concerns.
`video-worker-regression-v2.json` records case IDs and evidence hashes. The final
boot-health change was tested separately and in real Docker, not in a new full run.

The autonomous continuation resolved 127 of the previous 397 cases: 88 fixture
corrections, 38 browser environment cases and one explicit error-code omission.
Legacy download tests now opt into automatic acquisition explicitly instead of
inheriting the example's acceptance-only setting. Config tests remove the exact
fields under test before constructing overrides, so duplicate TOML keys cannot
mask validation. A sanitized wrong-channel fixture was corrected to remain wrong;
the generation test now actually changes the setting instead of depending on an
obsolete comment. No production safety switches or validators were relaxed.

Remaining diagnostic categories: 61 browser/compositor contracts, 54 historical
source attestations, 38 missing fixtures/artifacts, 34 policy/identity fixture
reviews and 83 requiring individual review. These categories are not waivers.
Historical attestations were not rewritten. Two of five HOME/XDG probe samples
still failed for separate compositor/public-description reasons and remain open.

An earlier full run was invalidated by inherited tests in the production base
image; another used Windows checkout bytes incompatible with old exact hashes.
Both are retained as failed/aborted diagnostics, not counted as passing runs. The
canonical run uses `git archive` bytes in `/audit/source`, with original locked
upstream bytes and synthetic config, separate from inherited image source/tests.

## Docker evidence

The final effective-production candidate passed isolated Compose acceptance at:
`/var/tmp/lighthouse-acquisition-sudn2ryu/report.json` on the Docker test host.

Container image identity reported by Docker:
`sha256:166bdd776615601eb159094b735bf047a4414f3b7a72b2e3d7d38d0300bdf6a4`.
It applies the scoped changes over actual production source overrides, retaining
the pre-existing X retirement protection. A full public-source overlay was rejected
by that protection and was not used as a production runtime.

Checks passed: no exposed port, no inherited env-file secrets, UID/GID 10001,
readonly root/profile/key, dropped capabilities, no-new-privileges, memory/CPU/PID
limits, rotated logging, WAL, two-container lock contention, one successful claim
per owned job, SIGKILL/restart with a different health-instance nonce, unrelated
processing row unchanged, rollback plan
does nothing and apply stops only the new container while preserving DB/media.

Synthetic configuration/credentials authorize no production action. The test
container mounts only its private directory, never the live DB or real profiles.

The later offline continuation candidate is
`sha256:5919670cf9656c0b6c41b7797161fe292d81efd2c40c107c88111cf5d433348f`
(`lighthouse-acquisition-candidate:98f62b0`). It extends the above image with only
the read-only preflight tool and explicit publisher error classifier fix; base
identity and layer ancestry were verified. The actual Docker build used no
network or package installation. Its staged-policy preflight and publisher-error
contract were run successfully with networking disabled. The initial SIGKILL/lease evidence above belongs to the prior image. The later
final-image requalification below now also exercises this exact additive image.
Neither image has been activated in production.

## Configuration and source portability continuation

Production read-only checks found 474 terminal FIFO records (14 completed, 458
excluded, two existing), and zero candidates eligible for an unstarted rebind.
All rows retain their historical policies. Unreadable bindings are reported as
unknown; exact matching was inspected using byte-identical private readable copies.

A staged bundle fixes permissions, the two-channel acquisition scope and the FIFO
planning fingerprint, while retaining authorization references/dates and leaving
the actual acquisition switch off. Actual candidate policy validators passed;
preflight reports only the intentional `YOUTUBE_WHOLE_DISABLED`. Production files
and DB remain unchanged. This completes offline policy preparation, not deployment.

The new source exporter produced two byte-identical committed-source archives.
All 1,428 manifest entries and 281 locked upstream files were verified after
transfer to the Linux server. This provides an offline alternative to fetching the
unavailable public compositor ref. It does not resolve mutable APT inputs or prove
a clean public Dockerfile build. Details and archive hash are in the deployment doc.

## One authorized real acquisition attempt

After the initial preflight, the user authorized the existing server for a real
test, explicitly requesting no repeated attempts if access remained restricted.
The final candidate used an isolated database/media directory and coherent
test-only configuration copies, retaining the existing rights/session evidence
and recording this test authorization. Production policies and bindings were
unchanged. The real API Secret and existing browser profile were mounted readonly.
No scheduler or publication handler was enabled; maximum attempts was one.

For the previously authorized source `c18fHra3k2I`, the metadata refresh completed
once. The planning job then failed once with `YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED`.
The result marked the failure non-retryable. Recorded provider counts were one
`youtube` call and one `youtube_download_http` call. The container was stopped,
restart policy was `no`, restart count was zero, and there were no MP4 files or
publication rows. Report: `/var/tmp/lighthouse-real-once-pxn7zpx1/report.json`.
This proves refusal handling, **not successful media acquisition or verification**.
The error does not distinguish an IP restriction from other platform restrictions.

A preceding monitor-startup fixture error was corrected only after stopping its
container and confirming zero jobs and zero API calls. It was not a second
platform access attempt. Further download tests on this server are stopped; a
future permitted environment still needs genuine download/recovery acceptance.

## Production blockers and required next steps

1. Apply the prepared policy bundle only after the remaining release gates pass.
   Live files remain unchanged: acquisition is off, policy files are unreadable by
   UID 10001, channel scope and planning fingerprint still differ. Recheck source
   hashes/active-task inventory before migration; never recalculate old job bindings.
2. Qualify future acquisition/publication/shared-component behavior. Per the
   subsequent owner scope decision below, closed one-off and retired-route tests
   are recorded without repairing or treating them as a new-service release gate.
   They are not passing evidence; old proofs cannot qualify changed live code.
3. Complete image supply-path qualification. The exact offline source export and
   immutable-base overlay path are verified, but the public compositor ref remains
   unavailable and clean Dockerfile APT/base inputs are not fully version-pinned.
   No production dependency was added.
4. Requalify production source/launch/retention evidence for the changed modules.
   Public anonymized configuration and live overrides are not interchangeable.
5. After the access restriction is legitimately resolved, run authorized real-content
   download acceptance without repeated probes on this server, then targeted acquisition
   deployment and V5 publication separation. Preserve platform refusal behavior;
   no account/IP/proxy/fingerprint switching or CAPTCHA bypass.

No historical code was deleted: production/maintenance/history independence was
not established for any old adapter. This is intentional compatibility retention,
not a claim that the cleanup and production acceptance phases are finished.

## Evidence and commits

Private local evidence is under the task workspace's
`work/video-worker-v2-audit/` (source inventory, test logs, sanitized preflight
result and SQLite/Docker reports). It is not shipped as a secret-bearing public
runtime bundle. Production config copies and browser/key mounts were never added
to Git.

Implementation commits: `5a9e396` (audit), `41c26b9` (scope/locks), `4d47fd8`
(container), `b11fb1e` (lease/receipt/recovery), `23681d0` (resource/rollback tests
and deployment gate), `e7c3101` (health connection cleanup), `10434ff` (legacy
Compose assertion), `b09d1c0` (boot-specific health identity). These are local
commits; this task has not pushed them. Continuation: `08eeae0` (fixture corrections,
read-only preflight and locked source export), `98f62b0` (explicit oversized-image
error compatibility). Documentation commits record evidence separately.

## Further fixture qualification (offline, 2026-11-10)

A further 160 originally unresolved cases now pass. Linux runs recorded 132/132
identity/QA/source cases, 144/144 historical-input/backup cases, and 55/56 config
and browser cases. The remaining browser case failed because its Chromium Unix
socket path exceeded the platform limit; it passed separately (1/1) with a short
`--basetemp=/testdata/px`. No sandbox restriction was relaxed. Three new artifact
locator tests are outside the original 4,587 cases. Exact XML hashes and resolved
case IDs are in `video-worker-regression-v2.json`; intermediate failures remain
in private evidence. This is not a fresh full-suite pass.

Corrections restore missing disabled examples, a frozen synthetic QA matrix,
consistent fixture identities, the delayed visibility-check manifest contract,
and historical source inputs. Existing strict policy checks, reviewed evidence
hashes, actual production authorization, task state and service configuration are
unchanged. The historical retention parser's exact original bytes include the
public subtitle-account UID; this scope is intentionally documented, not a new
authorization. The restored API-only Docker recipe has only static test evidence,
not a build/deployment claim. Old operators whose genuine reviewed source still
differs remain failed; current fixes are not reverted to make old proofs pass.

Read-only production observation again found the API healthy, scheduler/tweet/
system running, and the pre-existing video container stopped. SQLite quick_check
was `ok`, journal mode WAL. These observations do not establish video end-to-end
acceptance. No production file, database row, publication or cleanup was changed.

## Historical recovery, video evidence and GUI continuation

Another 71 originally unresolved cases passed after the above 160-case round:

- Linux metadata/description/legacy deployment selection: 73 passed, one original
  frozen-source attestation still failed. Restored exact public visible-description
  fixture bytes (SHA-256 `e00885130b7d63d75c9d0f11c0d2b312bd4ac5631dcc3d6758d94bee0e8d9378`)
  instead of changing its original metadata digest or character-position assertions.
  Example-title assertions now match the existing example channel names.
- Linux native-browser environment selection: 19 passed, two failed. The retired
  reply fixture still contains an invalid anonymized handle. The no-progress
  playback case was killed by the generic 40-second test timeout before its real
  bounded observer returned; a later explicit 100-second pytest budget (child
  limit still 80 seconds) passed. No runtime timeout, check or playback claim changed.
- Historical recovery: 24/24 passed on Windows and Linux, including three new
  cases proving current source is rejected without changing jobs or partial files.
  Exact old source bytes are test data, not rollback replacements for runtime code.
- GUI, playback and frozen-parser qualification: 26/26 passed in Linux. The GUI
  test used actual Xvfb/x11vnc/websockify/Chromium in a network-disabled container,
  an owned root-readonly three-key config and empty media tmpfs. No live profile,
  cookie or DB was mounted; no host port was published. The test verified loopback
  listeners and removal of temporary authentication files. Owned DOM removal of a
  synthetic challenge is test data, not bypass of a real platform challenge.
- Historical retention builder: 35/35 passed in Linux (38/38 including locator
  checks locally), including a new current-operator rejection case. Its original
  reviewed inputs were recovered from Git history and a retained old service unit;
  all operator digests remain unchanged. No live backup, install or cleanup ran.

Remaining 39 original-suite cases are 28 retired/public-anonymization X fixture
cases, six old disabled native multipart mutation scenarios, and five historical
source-attestation cases. They are not silently skipped or waived. The multipart
mutation route is intentionally disabled because its old UI assumption creates
batch posts; it must not be re-enabled to make those old positive tests pass.
A separately reviewed test-contract update is needed to assert that quarantine,
while preserving historical fixtures and read-only reconciliation coverage.
The public anonymized X identities need their own consistent fixture/source policy;
this download refactor does not broaden inactive runtime account scopes.

All totals remain an evidence union over 4,587 original cases, not a clean final
full-suite run. Three artifact-locator checks and four new current-source rejection
checks are additional to that original population. Public source publication,
production activation and another real YouTube download have not occurred.

## Owner scope clarification and final-image verification (2026-11-10)

The owner explicitly clarified that closed historical tasks need no repair when
they cannot affect future work. Before this clarification, the five remaining
historical deployment-source assertions were corrected using genuine archived
inputs; their full Linux selection passed 146/146. Seven new member-validation
and current-source rejection cases are additional to the original suite. No
source pin, current operator or live configuration was changed. A preliminary
Windows run had ten unrelated CRLF exact-byte failures; canonical Linux passed.

The remaining 34 original cases are retained as unresolved historical diagnostics:
28 retired X/browser and one-off inventory fixtures, plus six positive scenarios
for the intentionally disabled old native multipart UI. They are not repaired,
skipped, or represented as passed. Work on replacing those six tests was reverted
at the owner's clarification; the original specimens and disabled runtime remain.
There is no promise of a single clean full-suite run. This scope decision does not
waive tests of Store, locks, leases, current acquisition, delivery, publication
reconciliation, or shared tweet/backup components.

Read-only live verification confirmed `x_api_enabled=true`, `x_web_enabled=false`
and the visual-retirement marker on the actual running tweet container. The two
one-off inventory scripts have no direct references in services/adapters/scheduler.
The new V4 queue owns only plan/download/readiness/refresh; publication stays V5.
No historical browser source was restored into the production image.

Read-only live inventory also found nine old pending rows (one `video_process`,
eight `youtube_discovery`) and six review holds (one source-revision history,
five V4 readiness). Thus "historical work is closed" is not taken as permission
to relabel/delete database records. A new real-SQLite test uses synthetic IDs with
this exact type/state/count shape: old rows and attempts stay byte-equivalent,
review holds never execute, and a later new acquisition job runs without starvation.
The complete isolation selection passed 15/15 on Windows and in the **actual final
candidate image**, with networking disabled and no live database mounted.

The exact final image
`sha256:5919670cf9656c0b6c41b7797161fe292d81efd2c40c107c88111cf5d433348f`
now passed its own real Compose lifecycle acceptance at
`/var/tmp/lighthouse-acquisition-vrcoyfjf/report.json`: hardening/resource limits,
readonly Secret/profile, WAL, two containers sharing one physical video lock,
SIGKILL/restart with a new boot identity, untouched foreign job, and guarded rollback
stopping only the new container while retaining its DB/media. The test used owned
synthetic policy/key/data, no network, no downloads, no publication and no live
mount. The test container was stopped after acceptance; it is not a deployed service.

Outstanding release work is the production-specific policy/launch qualification
and authorized successful real-content acquisition/recovery. The only real source
attempt was refused and is not repeated. Fresh public-image reconstruction remains
a separate reproducibility limitation; the immutable existing-image route has
actual container evidence and does not require enabling a paid API or changing IP.

## Migration-host candidate qualification (2026-11-10)

The new Ubuntu 24.04 host passed an actual host reboot, with key SSH/sudo,
firewall and Docker restored. Production ownership remains on the original host.
No acquisition service has been enabled for real work on the migration host.

An incremental candidate uses the immutable image above and six canonical runtime
files from commit `54e16a72b0e7c21d89c03bfa616458800eaccc76`: the highest-download
adapter, acquisition preflight/runtime, access policy, credential loader and
migration 018. It preserves the existing CLI/publication modules and dependency
versions. This is not a claim that the public repository reconstructs the entire
private production image. No dependency installation occurs at container startup.

Candidate image:
`sha256:689e955a9654f9c4ea80ecd8adfdb67627fba55e857a4aaa2b3b6f0fa1be8240`.
Its test-only derivative (fixtures/scripts added, no production data):
`sha256:66089c763b34ae31a6930d72d5c27213c6fa423b07ae404b663357effd1823ac`.

The derivative passed 178 tests across 14 current download/isolation/recovery
modules in an actual Docker container: network none, UID/GID 10001, read-only
root, capabilities dropped, no-new-privileges and explicit resource limits.
The final derivative also passed Compose lifecycle checks for Secret permissions,
WAL, two-container locking, SIGKILL/restart, preservation of foreign tasks and
stop-only rollback. Its synthetic evidence is in
`/var/tmp/lighthouse-acquisition-p2vkjwgv/report.json` on the migration host.
All test containers were stopped. No YouTube download or publication occurred.

Three synthetic fixture helpers now derive their channel identities from the
candidate's existing channel scope, rather than hardcoding public placeholder
IDs. No production authorization or identity guard was relaxed. Their full
Windows selection passed 84 tests; the Linux selection above also exercised the
helpers. Tests need a repository-root working directory.

Earlier attempts are retained as failures: 40 failed/98 passed/52 errors, then
19 failed/159 passed. An overbroad CLI overlay depended on an absent X module and
was withdrawn. Fixture IDs were corrected; the synthetic media work directory
was moved from a 512 MiB tmpfs to the actual host disk, so the real 15 GiB resource
gate stays active. These results do not supersede the unresolved historical
diagnostics documented above or claim a clean full-suite run.

Still outstanding: effective production-policy qualification, migration 018 on
the final database, authorized real-content acquisition/recovery and production
cutover with one owner. Backup-object read/write and hash checks on the migration
host passed separately; they are not a full backup restore qualification.
