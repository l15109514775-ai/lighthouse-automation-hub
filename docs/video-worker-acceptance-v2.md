# Download Worker 2.0 — acceptance record, 2026-11-10

## Current checkpoint

The [current completion audit](download-worker-completion-audit.md) maps the full
scope to evidence and records the outstanding operational observations. Dated
sections below preserve historical failures as well as later corrections.

The new-host acquisition service is active and has passed isolated real download,
SIGKILL/resume, current-image Docker restart and stop-only rollback acceptance.
The owner-requested Yuuhi pause remains active. On 2026-11-10 a user-reported
missing Chise replay exposed disabled discovery in the **core** configuration,
despite enabled discovery flags in the acquisition configuration. That gap has
now been corrected; the normal official monitor registered two new replays, the
earlier replay is downloading and the reported replay has a completed plan waiting
for its download slot. Neither is claimed delivered by this checkpoint.

Maintenance now persists independent official-discovery health and alerts on a
disabled/stale/erroring upstream, excluding intentionally paused sources. Nine
focused tests passed in the production System Worker image, and a normal
scheduled maintenance result verified the deployed Chise-only check. This closes
the missing upstream-health signal, not full replay delivery or backup acceptance.

See [discovery recovery](youtube-discovery-recovery.md), current release JSON and
the dated sections below. Full goal completion remains unproven, including the
new-host full external-backup restore. The original decision immediately below
is historical and is retained as failure evidence, not current deployment status.

## Original pre-migration decision

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

## Dedicated child configuration regression (2026-11-10)

Real new-host acceptance exposed a subprocess boundary missing from the synthetic
tests: the parent selected the dedicated video configuration but the helper loaded
the disabled default. Download-side HTTP count was zero. Official metadata refresh
succeeded; this is neither successful media acquisition nor a platform refusal.

The fix only adds a validated HUB_CONFIG path to the existing sanitized helper
environment. The new tests start a real Python helper with an explicitly disabled
default configuration and enabled alternate configuration, check the selected
switch, and reject credential/proxy/injection inheritance, missing/outside/relative
paths, directories, oversized files and symlinks. Linux passed all eight cases;
Windows passed seven with the Linux symlink case explicitly skipped.

The related Linux downloader/transport/access selection initially had 134 passes
and one new fixture failure: its template flag had an inline comment or could be
absent, unlike the local ignored configuration. The fixture now explicitly sets
the default false and alternate true independently of the template's original
value; the eight-case rerun passed. Production code was not changed to fix that
fixture issue. This is a targeted evidence union, not a new clean full-suite run.

Runtime image with the fix:
`sha256:0722adba6834512161b932663951d76bb5994873ebd4456662cfa24c51aa7414`.
Its test derivative:
`sha256:e9b04314e4172aef4222e1620cb42b17540eeac778fcd36874284faf9752a14c`.
The base adapter before-hash was verified before replacement; no dependency was
upgraded. The final test-only fixture was overlaid read-only and recorded as
SHA-256 `6aa755ac35de1c6da060c945751cb219f3e46deaba2b65b8c41ed7c6af3e3e43`.
The actual test derivative passed Compose restart, WAL/lock, foreign-task
preservation and stop-only rollback at
`/var/tmp/lighthouse-acquisition-74w4zjj4/report.json`.

Production remains on the original host. A fresh isolated real-source acceptance
uses this fixed runtime; the prior failed test database is retained, not reset.
Its final outcome must be recorded separately before any activation decision.

## Authorized real acquisition and recovery passed (2026-11-10)

The fixed immutable runtime above completed the authorized `c18fHra3k2I`
acquisition on the migration host. A deliberately killed isolated container
retained 649,245,383 bytes. Restarting that same container allowed normal locked
recovery to reclaim the same job, with attempts increasing from one to two.
No state was manually reset; there was no third attempt. The original partial
prefix was preserved and grew. The worker reached `completed`, stage `done`,
with error_code cleared at 2026-11-10 11:42:28 UTC. The test container then stopped.

An independent offline, read-only container inspected and hashed the final file:

- 2,485,497,021 bytes; 6476.068571 seconds.
- H.264 1920x1080 at 60fps; AAC 44.1kHz stereo.
- SHA-256 `1a6405bb5f7fa3e62f65619563ab083e5b6e08d661c8540da8c2e9b2d1a74a4c`.
- 388,560 video packets and 278,901 audio packets, with verified source-to-merged
  coverage. The delivery manifest and database result reference the same asset.
- Single whole-file passthrough delivery; raw tracks released only after verified
  merge by the existing custody policy. The merged file remains available.
- SQLite integrity check passed; publication rows remained zero. Neither machine
  verification nor this test was recorded as human content review.

Evidence on the migration host is under
`/var/lib/lighthouse-migration/real-acquisition-once-v2/`:
`controlled-restart.json`, `recovery-report.json`, `final-acceptance.json`, and the
job's `verified.json` and `delivery-v5/manifest.json`. The original interrupted
`report.json` is historical evidence; it is not the terminal recovery result.

This closes the real-source acquisition and interruption-recovery test for this
sample. It does not prove production cutover, a new whole-host reboot with live
production, or every long/segmented input. Synthetic segmentation/fault evidence
remains distinct. Production still belongs to the original host, pending final
synchronization, caller routing, configuration deployment and single-owner checks.

## 2026-11-10 production cutover and whole-host reboot

The new host is now the sole production owner. The old host remains accessible
for recovery, but its application containers are stopped, Docker restart
policies set to `no`, Lighthouse timers/services disabled, and subtitle cron
entries fenced with their exact originals preserved. A fresh post-cutover SSH
check confirmed that fence. Do not restart the old stack against its stale DB.

The final stopped-source SQLite snapshot contained **21,372 jobs and 59 tables**,
SHA-256 `c22b6703819249b8db925f7d329b9a1d672857320abc2ce678135782195eedfb`.
Final rsync transferred 182,975,527 bytes in 727 files with no deletion. The
snapshot hash and every original table's count/sorted-row hash matched after
copy and additive migration 018. The four reviewed policies, dedicated API
Secret, corrected ingest boundary and owner-authenticated Bilibili profile were
installed after the delta. No human-review fields were fabricated.

An actual reboot changed boot ID from
`9a5124ec-8ccf-403f-b4bb-816e1d7b85c6` to
`29ae7770-338a-4f01-b381-b66bb408020b`. Eight containers started automatically:
API, scheduler, tweet worker, system worker, subtitle ingest, observer MCP,
observer tunnel and the independent replay acquisition worker. The old ordinary
video worker and subtitle runner remain stopped. Acquisition uses immutable
image `sha256:0722adba6834512161b932663951d76bb5994873ebd4456662cfa24c51aa7414`,
UID/GID 10001, read-only root filesystem, 1.5 CPU, 1400 MiB memory and no public
port. Its instance-bound producer and main heartbeat probe returned zero and
Docker health is healthy. Both `/health` and `/ready` returned HTTP 200.

Post-boot read-only comparison found **all 1,324 historical video job rows
unchanged, across all job columns**. Publication row count stayed 92; no synthetic
publication was submitted. At 12:58 UTC the new host had completed four normal
`x_discovery` and fifteen maintenance jobs without new errors. This does not
claim a new production tweet publication or a naturally arriving new replay has
already completed. The separate authorized real download and SIGKILL recovery
evidence above remains the real acquisition acceptance.

The Shanghai subtitle caller was paused only when no fetch subprocess existed,
then atomically changed to the new host after an authenticated HTTP boundary
check. Its original fetch file and service state were preserved. TAT invocation
`inv-0b12fagf23` confirmed `migrated_and_resumed`, active/enabled polling and the
expected HTTP 400 for deliberately missing job headers. No synthetic subtitle
job was submitted. The existing dot plugin returned a current `ready` snapshot
through the migrated tunnel, including the healthy acquisition container, with
zero provider requests. The temporary cross-host export key has been revoked;
ordinary operations SSH was rechecked successfully.

### Migration faults found and resolved

- Docker `top -eo comm` omitted the PID column required by Docker; adding
  `pid,comm` fixed the idle-receiver check before stopping any target service.
- A broad database filename glob also matched two historical backup copies.
  The promotion guard stopped, the inert partial move was restored, and the
  script was restricted to `hub.sqlite3`, `-wal`, and `-shm`. Those historical
  backup copies were not modified. Failed-attempt evidence is preserved.
- Final rsync restored `tweet-runtime-v4.toml` to root:root 0640, which the source
  process had already cached but the new API could not read. Only its group was
  changed to 10001, retaining contents and mode; future promotion does this too.
- The observer's original `OpenStdin=true` had been omitted in Compose and its
  process exited on EOF. `stdin_open: true` restores the required behavior. The
  runtime generator now preserves stdin, tty and supplemental groups. Docker's
  legacy `StdinOnce` has no direct Compose field; this difference is recorded,
  not asserted identical. A real dot query verified the migrated transport.

### Explicit limits and remaining acceptance

- Daily backup is active/enabled, next scheduled for **2026-11-11 03:30 Asia/Shanghai**.
  The last full verified backup displayed by dot still predates migration.
  New-host HEAD/GET and a tiny PUT/GET canary passed earlier; they are not a
  full archive restoration. New-host full backup/restore evidence is outstanding.
- The previously failed incremental-watch service and legacy replay-reconcile
  timer were not revived. Historical errors remain in the database/status.
- Four preactivation rollback plan tests passed on Windows and in a real
  network-disabled, read-only Linux container. This is not an executed production
  rollback. Since the target now has new writes, reverse migration must preserve
  the target's current DB, media and publication records. The preactivation
  `restore_fenced_source.py` path is no longer applicable.
- Runtime artifacts use a pinned captured base plus hash-checked changes and
  separately exported locked source. This is not a claim that the entire
  historical production stack has been freshly rebuilt from the public repo.
- No new clean full-suite run is claimed. Earlier targeted test results,
  intentionally retired compatibility failures and their scope remain recorded.
  Final goal-wide regression/reproducibility audit and reverse-migration
  documentation remain open; migration success alone does not close the goal.

Evidence on the new host: `/var/lib/lighthouse-migration/final-promotion.json`,
`final-database-fingerprints.json`, `production-reboot-before.json`,
`production-reboot-after.json`, `production-release-verification.json`,
`core-compatibility-fix/report.json` and `rollback-plan-tests/report.json`.
Local copies are under `work/zhiyin-migration-20261110/evidence-*`.

### Backup-entry audit correction (2026-11-10 13:09 UTC)

The optional `--verify-recovery` command returned
`DAILY_RECOVERY_SUCCESSOR_REFUSED` / `ABSENT_TWO_LATEST_COPIES`. That command checks
the historical **absent-latest recovery branch**. Current production has a
completed `latest.json`; the normal daily entry instead validates that completed
record. Running the applicable `--verify-only` and `validate_previous` checks
passed, including the deployed helper pins. This is **not an active backup
blocker** and no production code or backup records were changed to make it pass.
The ledger has eleven verified operations and no uploading/unknown operation.
The deployed retention wrapper differs from the public copy in pins for an
already-deployed retention-cap transition; preserve that deployed graph rather
than overwriting it with the older public helper. Its wrapper SHA-256 is
`86d1c800d3fe18828a651eaea7eb94b386a1c9333c4a0bc17c54842da981e5bf`.
Evidence: `backup-current-entry.json` is the applicable preflight result;
the recovery diagnostic is retained with its inapplicability explained. This
read-only check made zero external requests and did not create a new backup.
Full new-host backup/restore acceptance remains outstanding.

### Post-cutover full backup attempt — budget refusal (2026-11-10)

One real normal-entry execution was started and then observed to terminate:
systemd invocation `156ecadfcc064cbb8d5cd2d25e2a4e32`, exit status 2,
operation `3284d90b-9844-4a01-85ba-37ac0eb4758f`, daily record
`aed00779-ad1a-421d-8375-b4535a633ff6`. The ledger's authoritative error is
`CLOUDBASE_BACKUP_BUDGET_EXCEEDED`. The outer wrapper's generic
`DAILY_RECOVERY_SUCCESSOR_REFUSED` does not identify the underlying failure.

Read-only ledger inspection confirmed **zero requests and no transfer plan** for
this operation. No PUT or GET was dispatched. Existing daily reservations were
354,548,122 bytes / 7 requests against limits of 512,000,000 bytes / 50 requests;
monthly reservations were 2,241,894,688 bytes / 102 requests against limits of
8,000,000,000 bytes / 250 requests. The retained source-host usage counts toward
the same budget. Reservations are conservative network accounting, not an invoice.
No quota, reservation, historical error or ledger row was reset/refunded.

The daily pointer is now `accept_failed`. Its guard correctly prevents an
unreconciled repeat, but also means the enabled 03:30 timer alone cannot restore
normal operation. A narrowly qualified zero-request failure recovery and a later
within-budget full restore acceptance remain outstanding. Do not start another
backup until that recovery is verified. The prior 13:09 preflight was valid then;
it does not describe the subsequent failed pointer.

Evidence: `postmigration-backup-start.json`, `postmigration-backup-observation.json`,
`postmigration-backup-budget.json` and private invocation journal under
`/var/lib/lighthouse-migration`. At 13:20 UTC all eight production containers
still ran with zero restarts; acquisition and API were healthy, the DB integrity
check was `ok`, and no processing job was present at that observation.

### Portable qualified runtime artifact (2026-11-10)

The exact acquisition image was exported without changing production containers:

- Archive: private `/var/lib/lighthouse-migration/portable-acquisition-0722/image.tar`.
- Bytes: **838,561,280**.
- SHA-256: `69bf7bd9dd7201b049cf89692e794a7f52275003cfcd94e6848bab2eae5f9f9d`.
- Platform: Linux amd64; immutable image identity remains
  `sha256:0722adba6834512161b932663951d76bb5994873ebd4456662cfa24c51aa7414`.
- The OCI index-to-platform-manifest-to-config chain was validated against that
  identity; all 20 uncompressed layer hashes matched the running image's rootfs,
  and all 26 descriptor-addressed OCI blobs matched their stored content hashes.
- Actual `docker image load` succeeded on the existing daemon. A new network-none,
  read-only, UID 10001 container found all three selected acquisition/DB/adapter
  modules without mounting live data or secrets. Production container IDs stayed
  unchanged. No download, publication or provider call was made.

The initial local verifier assumed Docker image identity always equals the config
digest and correctly stopped on `CONFIG_ID_MISMATCH`. This daemon uses an OCI
index identity. The corrected verifier followed and checked the descriptor chain;
it reused the already exported archive rather than exporting or rebuilding again.
The config and index hashes were not rewritten to satisfy a check.

This proves a portable, integrity-checked captured runtime and same-daemon load,
**not** a clean-daemon import, fresh public-source rebuild or public redistribution
audit. Keep the archive private. Base/APT pinning and complete source provenance
remain separately tracked. Manifest and load evidence are in `manifest.json` and
`load-report.json` alongside the archive; local private evidence copies are kept
in the migration workspace.

### Guarded backup deferral deployed (2026-11-10)

The zero-request budget failure now has an exact root-owned recovery attestation.
The existing daily entry validates its unchanged daily/operation records, no
requests/plans and no unresolved operation before allowing a future successor.
Insufficient headroom defers before creating another operation. Historical failures
and usage are retained; unknown-upload reconciliation and cleanup are unchanged.
See [the recovery specification and rollback](backup-budget-recovery.md).

The related selection passed 36 local and 36 real Linux Docker tests. The older
remote-absent reconciliation tests passed another 50 locally. Actual systemd
invocation `285f578d4aa5437d8a2b7ebe15b92d3a` returned safe `budget_deferred`, exit 0,
with identical full-row fingerprints for operations/requests/plans and unchanged
failed latest bytes. No operation, request or refund occurred. The next 03:30
timer can now proceed when its existing budget permits; actual remote restore
acceptance remains outstanding. The earlier unreconciled-pointer blocker is
resolved, while the daily budget is deliberately unchanged.

### Remaining regression cases reclassified against the active scope

All 34 original unresolved case IDs retain their original failed/error results.
Current scope classification is six closed one-off capture/inventory cases,
22 retired X visual/browser fixture cases and six disabled native multipart UI
cases. Named-reference inspection found no use of the two one-off operators in
services/adapters/scheduler. The original multipart mutation gate remains false.
These static observations supplement the owner's explicit closed-history scope;
they are not proof that every dynamic import was analyzed again.

An actual read-only probe of the migrated tweet container confirmed
`x_api_enabled=true`, `x_web_enabled=false` and successful validation of the image's
visual-retirement marker and protected files. No retired route was re-enabled.
`video-worker-regression-v2.json` now records per-case disposition and current
activation separately from the historical regression snapshot. No failure was
changed to passed or skipped, and no new clean full-suite run is claimed. Current
Store/lock/lease, acquisition/delivery, tweet/backup sharing and unknown-publication
safety remain release requirements.

### Clean Docker/containerd import acceptance (2026-11-10)

A genuinely empty Docker image store and a separately started, empty containerd
content store loaded the qualified private acquisition archive. All 20 rootfs
layers and image identity matched. An actual offline read-only UID10001 container
recorded 48 Python distributions and 450 Debian packages; only loopback existed.
Both test daemons stopped and production container IDs remained unchanged. No live
mounts, provider requests, download, publication or production restart occurred.

The first attempt failed and remains recorded: Docker auto-connected to host
containerd across mount namespaces. A separate Docker namespace alone did not
prove isolated content storage. The corrected tool explicitly binds Docker to a
private containerd root/state/socket in the same isolated namespace. The second
run passed. Temporary stores were removed after stopped-process/mount checks;
original archive and reports are retained. See portable-image-verification.md.

The current acquisition service's two read-only source-control modules and pause
file were separately captured into a deterministic 20480-byte private overlay tar,
SHA256 6562b2db375231b9a677889279795814936877d8918dc2e3bc6f5b33e7794184.
Base worker hash, staged after-hash and current in-container bytes matched. The
versioned release manifest records this effective configuration without credentials.
Overlay-bundle redeployment and a fresh public Dockerfile build are not yet proven.

### Effective overlay archive restoration accepted (2026-11-10)

The preceding overlay redeployment limitation is now resolved for isolated restore:
`test_acquisition_overlay_bundle.py` extracted the exact allowlisted archive into
a fresh directory and ran the qualified 0722 image with those restored files.
All three hashes and operational pause values matched; all five synthetic
source-control/Worker tests passed. Six local archive safety tests passed (exact
allowlist, archive/member digest changes, traversal, symlinks and duplicates).
Production container IDs stayed unchanged. No live DB, profile, secret or provider
request was used. The release JSON now includes fixture hashes and restore evidence.
This does not qualify a fresh public-tree rebuild or production reverse migration.

### Effective-source compatibility checkpoint (2026-11-10)

Read-only inspection compared 160 current acquisition source/SQL/dependency files
with public revision 5402be7: 140 match; 20 are classified and hash-bound. Four
identity/display differences are AST-identical after exact known placeholder
mapping; three concern shared transport/accounting versions; thirteen concern
routes not registered as acquisition handlers. No file was overwritten or deleted.

Actual current-image tests passed 47 budget/HTTP, task-isolation and recovery-fault
cases. A readonly production-config/data preflight passed, and actual constructor
inspection registered exactly the four V4 acquisition job types, with publication
disabled. Producer tick, Worker execution and Store initialization were not called.
Historical row counts remain 1324 video / 474 terminal FIFO. This count observation
does not replace the earlier all-column migration fingerprint comparison.

Details and remaining limits: video-worker-current-compatibility.md and the
versioned source-compatibility JSON under docs/releases. The 34 deferred historical
failures remain failures/errors; no clean full-suite result or fresh public-tree
build is claimed. Production containers were unchanged and provider requests zero.
