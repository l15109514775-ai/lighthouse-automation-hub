# Bounded recovery before public submission

`scripts/ops/recover_private_upload.py` is a trusted operator tool, not a
scheduler action or MCP capability. It permits one new native upload attempt
after a reconciled V5 browser failure during private upload. It does **not**
claim that an unfinished private blob has disappeared from Bilibili.

The operator must diagnose the original failure first. Never use this to retry
a platform denial, CAPTCHA or login challenge. The November 11 use was tied to
recorded container OOM evidence, a corrected memory limit, the ordinary manager
and editor inspection, and an authenticated account check. No public submission
or draft-save evidence existed for that failed attempt.

The tool requires the unchanged source/media/policy/launch binding, no lease,
no publication row for the source, exactly one private selection checkpoint,
the matching selection intent, and the precise pre-submit error evidence.
Any draft-save, public-submit, upload-result or previous recovery evidence
refuses the operation. It cannot recover `publish_unknown` or completed work.

Run as application UID/GID 10001 in the qualified publisher runtime. The CLI
acquires the existing video, moderation, producer, FIFO and profile locks.
Cover normalization/QA acquire their own non-reentrant heavy-resource locks.
A busy lock stops the command without changing the task; do
not kill its owner. The default invocation accepts `--job UUID` and
`--operator-reference TEXT` and returns a read-only plan and its SHA-256.
Only a second invocation with the identical arguments and
`--apply-plan-sha256 SHA256` applies that reviewed plan. Supply the qualified
script to the existing runtime; do not launch another browser or publisher.

Application archives the original full job snapshot, error and consumed
selection intent under the job's `private-upload-recovery/<plan-sha>/` directory.
It preserves the payload, source ID, publication idempotency key, authorization,
human-review fields and `jobs.attempts`. The normal worker increments attempts
on its next claim; this is never reset to zero. The old checkpoint and private
selection remain in history. The new native attempt remains held with no
selections of its own. `--prepare-cover` archives the old derived cover and
provenance, copies the hash-verified cached official JPEG into the new attempt,
generates a fresh derived cover and runs the existing full machine verifier.
Only `--prepare-cover --release-plan-sha256 SHA256`, using the original plan
hash, releases the task after successful preflight. Failed QA leaves it held
without consuming another normal worker attempt. The ordinary worker repeats
its full QA and native upload safeguards on claim.
Reapplying the tool is prohibited. No platform call or submit occurs in it.

If killed after the selection intent is archived but before SQLite commits,
the task remains at the exact old unknown checkpoint. With the same locks,
`--restore-interrupted-journal SHA256` restores only that intent if the entire
job is still unchanged and no new intent exists. It does not restore a database
snapshot, requeue work, erase history or affect other jobs. A successful recovery
cannot be undone with this mode. Keep the journal for audit and backup.

The cover cache is job/source specific. The previous attempt's verified
original JPEG may be copied locally, but the resulting new provenance must
identify the new attempt's file. The old derived cover and provenance remain
intact in the recovery journal. The producer, machine verifier and browser all
retain their strict current-attempt checks. This makes no claim of a fresh HTTP
download. There is no cross-attempt exception in the production cover verifier.

Acceptance evidence: 18 local tests passed, with the real SIGKILL case skipped
on Windows; all 19 passed in the qualified Linux Docker image with no network
or production mounts. Coverage includes actual cover generation and the existing
machine cover verifier, plus rejection before release when QA fails. The initial
production recovery consumed two preparatory claims before complete cover
integration was resolved; neither reached a browser or another video selection.
The incomplete cross-attempt cache exception was rolled back before further work.
The final tool holds the job until preflight succeeds, avoiding that extra-claim
failure mode. See
`releases/private-upload-recovery-20261111.json` and
the superseded `releases/publication-cover-cache-20261111.json`; final tested
source, actual strict-runtime rollback and successful full production preflight
are in `releases/publication-cover-rebind-20261111.json`.

This is not evidence that the 11.2 GB upload has completed. Its new upload,
submission and public verification still need runtime evidence.
