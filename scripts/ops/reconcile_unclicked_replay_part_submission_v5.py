"""Return one provably unclicked V5 submit reservation to the saved-draft state."""
from copy import deepcopy
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

from services.hub.db import encode
from services.hub.locks import resource_guard
from services.hub.media_processing import atomic_json
from services.hub.replay_parts_v5 import STATE, ReplayPartVideoWeb

VERSION = "reconcile-unclicked-replay-part-submission-v5"
JOB = "14bd3ee4-b7d8-4ffe-bd8b-54a2af94610e"
PARENT = "1e28dd0f-cc2d-4928-bfd6-e373036c193a"
ATTEMPT = "9e794f91-dfc9-4e22-a5ed-cdf95af215e0"
SOURCE = "EoZFofw4OSU"
KEY = "6d1977a6d70fc64169f11b0f13c09f7fb023c7879c455ca8081a9127fdd011fd"
RESERVATION = Path(
    "/opt/lighthouse-hub/media/14bd3ee4-b7d8-4ffe-bd8b-54a2af94610e/"
    "replay-browser-part-v5/9e794f91-dfc9-4e22-a5ed-cdf95af215e0/reservation.json"
)
INTENT = Path(
    "/opt/lighthouse-hub/media/14bd3ee4-b7d8-4ffe-bd8b-54a2af94610e/"
    "replay-native-intents/public-submit.json"
)
SUBMIT_SCREENSHOT = RESERVATION.parent / "immediately-before-submit.png"
MANAGER = Path(
    "/opt/lighthouse-hub/media/14bd3ee4-b7d8-4ffe-bd8b-54a2af94610e/"
    "interrupted-upload-readonly/2026-11-03T19-28-20-120Z/manager.json"
)
EDITOR = Path(
    "/opt/lighthouse-hub/media/14bd3ee4-b7d8-4ffe-bd8b-54a2af94610e/"
    "saved-draft-status-readonly/2026-11-03T19-28-55-899Z/dom.json"
)
MANAGER_SHA = "05ac3164b53627a81652014b1b1c3b1172d9058a22d934d2719799b8040d7622"
EDITOR_SHA = "1332bdd17cdd4ed822647e3e8d402a40effc5b3e0135276d0593c2eb6fa269b4"
RESERVATION_SHA = "c5fd2b8a6d15b8ec35387c5567af83957d93a891362c7370b258dd73ba8e97a7"


def need(value, code):
    if not value:
        raise ValueError(code)


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(wire(value)).hexdigest()


def stable_plan_sha(plan):
    value = deepcopy(plan)
    value.pop("planned_at", None)
    return digest(value)


def read(file, expected_sha=None):
    file = Path(file)
    need(file.is_absolute() and file.resolve() == file and
         not any(p.is_symlink() for p in (file, *file.parents)), "RECONCILE_EVIDENCE_PATH_INVALID")
    raw = file.read_bytes()
    need(0 < len(raw) <= 2_000_000, "RECONCILE_EVIDENCE_SIZE_INVALID")
    sha = hashlib.sha256(raw).hexdigest()
    if expected_sha is not None:
        need(sha == expected_sha, "RECONCILE_EVIDENCE_CHANGED")
    return json.loads(raw), {"file": str(file), "bytes": len(raw), "sha256": sha}


def row_with_json(row):
    value = dict(row)
    for key in ("payload", "result"):
        value[key] = json.loads(value[key])
    return value


def current_state(settings, store, now):
    service = ReplayPartVideoWeb(settings, store)
    job, binding, state = service.snapshot(JOB, write=False)
    policy = service._validate_snapshot_binding(job, binding, verify_artifact_hash=False)
    service._require_workflow_launch(policy)
    need(job["job_id"] == JOB and job["source_id"] == SOURCE and
         job["job_type"] == "replay_part_publish_v5_p01" and
         job["state"] == "publish_unknown" and
         job["stage"] == "short_video_reconcile_required" and
         job["attempts"] == 2 and job["error_code"] == "PUBLISH_UNKNOWN" and
         job["external_publish_id"] is None and job["lease_owner"] is None and job["lease_until"] is None,
         "RECONCILE_EXACT_JOB_REQUIRED")
    need(binding["parent_job_id"] == PARENT and state["state"] == "submit_unknown" and
         state["attempt_id"] == ATTEMPT and state["key"] == KEY and
         "native_preparation" in state and "upload_observation" in state and
         state["public_submission_approved"] is False and state["human_review"] is False,
         "RECONCILE_CHECKPOINT_CHANGED")
    pubs = store.query("SELECT * FROM publications WHERE job_id=? AND idempotency_key=?", (JOB, KEY))
    need(len(pubs) == 1 and pubs[0]["state"] == "unknown" and
         pubs[0]["external_id"] is None and pubs[0]["external_url"] is None and
         pubs[0]["kind"] == "video_website_replay_part_v5_p01",
         "RECONCILE_PUBLICATION_CHANGED")
    registry = store.query("SELECT * FROM replay_part_children_v5 WHERE child_job_id=? AND idempotency_key=?",
                           (JOB, KEY))
    need(len(registry) == 1 and registry[0]["state"] == "unknown" and registry[0]["external_id"] is None,
         "RECONCILE_REGISTRY_CHANGED")
    need(not INTENT.exists() and not INTENT.is_symlink(), "RECONCILE_SUBMIT_INTENT_EXISTS")
    need(not SUBMIT_SCREENSHOT.exists() and not SUBMIT_SCREENSHOT.is_symlink(),
         "RECONCILE_PRE_SUBMIT_SCREENSHOT_EXISTS")
    reservation, reservation_ref = read(RESERVATION, RESERVATION_SHA)
    need(reservation["version"] == "replay-part-native-reservation-v5" and
         reservation["job_id"] == JOB and reservation["attempt_id"] == ATTEMPT and
         reservation["idempotency_key"] == KEY and reservation["can_click_submit"] is True and
         reservation["state"] == "dispatching", "RECONCILE_RESERVATION_CHANGED")
    manager, manager_ref = read(MANAGER, MANAGER_SHA)
    editor, editor_ref = read(EDITOR, EDITOR_SHA)
    manager_text = manager["manager"]["visible_text"]
    need(manager["target_site_uid"] == manager["observed_uid"] == binding["site_uid"] and
         "全部稿件 4" in manager_text and "草稿 1" in manager_text and
         "进行中 0" in manager_text and "已发布 4" in manager_text and
         "直播回放 P1/2" not in manager_text, "RECONCILE_MANAGER_NOT_UNSUBMITTED")
    need(editor["job_id"] == JOB and editor["attempt_id"] == ATTEMPT and
         editor["navigation"] == "saved_draft_opened" and editor["mutations"] is False and
         editor["result"]["editor_ready"] is True and len(editor["result"]["success_nodes"]) == 2 and
         any(node["classes"] == "success" and node["text"] == "上传完成"
             for node in editor["result"]["success_nodes"]),
         "RECONCILE_SAVED_DRAFT_NOT_VERIFIED")
    need(not store.query("SELECT 1 FROM publications WHERE source_platform='youtube' "
                         "AND source_id=? AND state IN ('completed','awaiting_moderation','dispatching')", (SOURCE,)),
         "RECONCILE_OTHER_PUBLICATION_ACTIVE")
    active = store.query("SELECT job_id FROM jobs WHERE queue='video' AND "
                         "(state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)")
    need(not active, "RECONCILE_VIDEO_NOT_IDLE")
    plan = {
        "version": VERSION,
        "job_id": JOB,
        "parent_job_id": PARENT,
        "attempt_id": ATTEMPT,
        "attempts_before": job["attempts"],
        "job_row_sha256": digest(job),
        "publication": dict(pubs[0]),
        "registry": dict(registry[0]),
        "reservation": reservation_ref,
        "manager": manager_ref,
        "editor": editor_ref,
        "state_before": deepcopy(state),
        "provider_submit_clicked": False,
        "planned_at": now,
    }
    return job, plan


def apply(settings, store, plan, plan_sha):
    need(plan["version"] == VERSION and stable_plan_sha(plan) == plan_sha,
         "RECONCILE_EXACT_PLAN_REQUIRED")
    now = time.time()
    folder = settings.root / "media" / JOB / "unclicked-submit-reconcile-v5"
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    record = folder / (plan_sha + ".json")
    atomic_json(record, plan)
    record.chmod(0o600)
    record_ref = {"file": str(record), "bytes": record.stat().st_size,
                  "sha256": hashlib.sha256(record.read_bytes()).hexdigest()}
    with store.transaction() as db:
        current = db.execute("SELECT * FROM jobs WHERE job_id=?", (JOB,)).fetchone()
        need(current is not None, "RECONCILE_JOB_MISSING")
        row = row_with_json(current)
        need(digest(row) == plan["job_row_sha256"], "RECONCILE_JOB_CHANGED")
        pub = db.execute("SELECT * FROM publications WHERE job_id=? AND idempotency_key=?",
                         (JOB, KEY)).fetchone()
        need(pub is not None and dict(pub) == plan["publication"], "RECONCILE_PUBLICATION_CAS_FAILED")
        child = db.execute("SELECT * FROM replay_part_children_v5 WHERE child_job_id=? AND idempotency_key=?",
                           (JOB, KEY)).fetchone()
        need(child is not None and dict(child) == plan["registry"], "RECONCILE_REGISTRY_CAS_FAILED")
        result = deepcopy(row["result"])
        state = result[STATE]
        need(state["state"] == "submit_unknown", "RECONCILE_STATE_CAS_FAILED")
        state = {**state, "state": "uploaded"}
        marker = {
            "version": "unclicked-submit-reconcile-v5",
            "plan_sha256": plan_sha,
            "record": record_ref,
            "attempts_before": row["attempts"],
            "attempts_reset": False,
            "publication_row_removed": True,
            "external_id_was_null": True,
            "submit_intent_absent": True,
            "pre_submit_screenshot_absent": True,
            "manager_draft_count": 1,
            "manager_published_count": 4,
            "provider_submit_clicked": False,
            "publication_reused": False,
        }
        result[STATE] = state
        result["unclicked_submit_reconcile_v5"] = marker
        changed = db.execute(
            """UPDATE jobs SET state='pending',stage='replay_part_uploaded_not_clicked',
               error_code=NULL,result=?,next_run=0,updated_at=?,lease_owner=NULL,lease_until=NULL
               WHERE job_id=? AND state='publish_unknown' AND stage='short_video_reconcile_required'
               AND attempts=? AND error_code='PUBLISH_UNKNOWN' AND payload=? AND result=?
               AND external_publish_id IS NULL AND lease_owner IS NULL AND lease_until IS NULL""",
            (encode(result), now, JOB, plan["attempts_before"], encode(row["payload"]),
             encode(row["result"])),
        ).rowcount
        need(changed == 1, "RECONCILE_JOB_CAS_FAILED")
        need(db.execute("UPDATE replay_part_children_v5 SET state='prepared',external_id=NULL "
                        "WHERE child_job_id=? AND idempotency_key=? AND state='unknown' AND external_id IS NULL",
                        (JOB, KEY)).rowcount == 1, "RECONCILE_REGISTRY_UPDATE_FAILED")
        need(db.execute("DELETE FROM publications WHERE job_id=? AND idempotency_key=? "
                        "AND state='unknown' AND external_id IS NULL AND external_url IS NULL",
                        (JOB, KEY)).rowcount == 1, "RECONCILE_PUBLICATION_DELETE_FAILED")
        db.execute("INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)",
                   (now, "owner-authorized-maintenance", "replay_part_unclicked_submit_reconciled_v5",
                    JOB, encode(marker)))
    after = store.get(JOB)
    need(after["state"] == "pending" and after["stage"] == "replay_part_uploaded_not_clicked" and
         after["error_code"] is None and after["attempts"] == plan["attempts_before"] and
         after["result"][STATE]["state"] == "uploaded",
         "RECONCILE_POSTCONDITION_FAILED")
    need(not store.query("SELECT 1 FROM publications WHERE job_id=? AND idempotency_key=?", (JOB, KEY)),
         "RECONCILE_PUBLICATION_NOT_REMOVED")
    result = {
        "version": VERSION,
        "state": "pending_uploaded_not_clicked",
        "plan_sha256": plan_sha,
        "job_id": JOB,
        "attempts": after["attempts"],
        "provider_submit_clicked": False,
        "publication_row_removed": True,
        "record": record_ref,
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-plan-sha256")
    args = parser.parse_args()
    need(os.getuid() == os.getgid() == 10001, "RECONCILE_APP_UID_REQUIRED")
    from services.hub.config import Settings
    from services.hub.db import Store

    settings = Settings.load()
    need(settings.root == Path("/opt/lighthouse-hub"), "RECONCILE_EXACT_ROOT_REQUIRED")
    store = Store(settings.db_path)
    with resource_guard(*(settings.root / "data/locks" / name for name in
                          ("video", "moderation", "replay-multipart-producer-v4", "replay-fifo-dispatch-v4"))):
        _, plan = current_state(settings, store, time.time())
        plan_sha = stable_plan_sha(plan)
        if args.apply_plan_sha256 is None:
            result = {"state": "verified_unclicked_submit_reconcile_plan",
                      "plan_sha256": plan_sha, "job_id": JOB,
                      "attempts_before": plan["attempts_before"],
                      "provider_submit_clicked": False}
        else:
            need(args.apply_plan_sha256 == plan_sha, "RECONCILE_EXACT_PLAN_REQUIRED")
            result = apply(settings, store, plan, plan_sha)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
