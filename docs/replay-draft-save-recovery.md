# Saved replay draft acknowledgement

Bilibili may navigate directly to its draft manager after the one-use private
save click, without retaining the old saved toast. A missing toast does not
justify selecting the video or clicking save again.

The native driver preserves the existing toast acknowledgement. When the page
redirects, or a previous save intent exists without a final preparation receipt,
it checks the same attempt's video-selection, cover-completion and save intents,
the previous bound preparation observation, and the original upload fingerprint.
It then navigates the saved draft manager, reads back the exact metadata,
requires native upload completion and a visible applied cover, and records the
existing strict preparation schema. The acknowledgement is separate evidence;
it does not add fields to the Python QA receipt or claim a remote file hash.
Previous screenshots/observations are archived before refreshing them.

Native pagination is bounded to 60 cards across at most six pages. Duplicate
titles, incomplete inventory, changed source/metadata, absent cover, a public
submit intent or platform protection stop recovery. A generic title href is
clicked normally; any popup's official draft URL and title must be observed
before reusing that URL in the controlled tab. No hidden API or guessed draft ID
is used. An already concrete native href remains supported.
Repeated ASCII spaces may collapse in a card's rendered text; the title
attribute and the final editable title stay exact. A fresh `about:blank` tab
enters only the fixed official manager before normal origin/account checks.

The part-session preparation IPC deadline is 900 seconds to accommodate the
existing full-file checks and bounded draft navigation. Upload concurrency,
retry limits, publication reservation expiry, task attempts and authorization
semantics are unchanged. Reaching the preparation deadline still stops the
operation; it never permits another upload or public click.

## Current held task

The November 11 large-file acceptance retains native attempt
`341aec7a-8f2e-484e-8e4a-d8b49f824ff7` and job
`82a818a5-6ac2-44e6-adbd-88a3a279e0f8`. Its observed saved draft is `4104806`.
All three private intents remain consumed. There is no public-send intent or
publication row at the last inspection. The job's four attempts must remain in
history; deploying this fix alone does not silently retry an exhausted task.
Any bounded maintenance continuation must use that same uploaded draft and the
existing machine QA, source controls, grant and single-submit reservation.

## Qualification and rollback

`tests/test_replay_draft_save_acknowledgement.py` uses real generated media,
SQLite and routed Chromium to exercise redirect acknowledgement, lost receipt,
blank startup tab, collapsed title rendering, wrong source, missing cover,
changed intent, consumed submit, challenge,
cross-page duplicate and invalid popup. The SQLite integration also passes the
actual Python QA/reservation pipeline and checks one submission. These are
synthetic site tests, not live publication evidence.

The initial 40-check regression run passed. Real prepare-only attempts then
identified the blank startup tab and unrelated-card whitespace cases; those
attempts stopped without submission. The follow-up runs passed 12 and then 18
checks respectively, with overlap (not 70 unique tests). The final three source
files are deployed in the healthy independent publisher; all other container
IDs and the held job stayed unchanged. See
[dated qualification/deployment evidence](releases/draft-save-ack-release-20261111.json).
Real preparation, bounded continuation and final publication are separate
acceptance steps and must be recorded from their own results.

Promote only the three qualified runtime files in a versioned, read-only source
overlay for the independent publication service. Save the prior Compose and
enable-marker pair before promotion. Require idle video/moderation/profile
locks; do not restart unrelated containers. Rollback restores that exact pair
and restarts only the independent publisher. Never restore an older database,
erase intents, delete the uploaded draft or reset attempts as part of rollback.
