"""Retire one interrupted V5 group and let normal admission create a fresh pair.

This operator never resets an old attempt, reuses an uncertain upload, edits
media, or submits directly. It first proves that every delivery byte still
matches and that a fresh Bilibili read-only observation has no draft or
candidate publication. Apply is CAS-bound to a dry-run plan.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import argparse
import hashlib
import json
import os
import stat
import time
import uuid

from services.hub.db import encode
from services.hub.media_processing import atomic_json
from services.hub.replay_parts_v5 import (
    GROUP_JOB,
    ReplayPartGroupsV5,
    ReplayPartVideoWeb,
    STATE,
    verified_delivery,
)

VERSION = "retire-interrupted-replay-part-group-v5"
SOURCE = "EoZFofw4OSU"
DOWNLOAD = "5af6b351-e73a-4451-b32a-cc845a2892f1"
PARENT = "2b51011b-d035-41e1-a5fb-e7c1366604bd"
CHILDREN = (
    "900579c1-705a-42d2-8e0e-968124536e03",
    "98b626c9-663f-4c42-bfe5-360a9ce3f715",
)
UID = "1000000000000000"
PARTS_SHA = "087b2340276cd472fa754e7dc55008dd2efd8ee62b0d36e52d6060ef38d1a58d"
POLICY_SHA = "1118403498c66bb3f21c0682ab86e801e91abcd740be759d170bc8415ef0f773"
EVIDENCE = Path(
    "/opt/lighthouse-hub/media/900579c1-705a-42d2-8e0e-968124536e03/"
    "interrupted-upload-readonly/2026-11-03T17-37-35-779Z"
)
MANAGER_SHA = "a04828a54e7b3b882bef9d06ae480cdf740d5d0f414aaa0b82e05fed6221beb2"
EDITOR_SHA = "6b5123eb9ca742337e49c6adbbb85ee87f619c820687393b11e63d8263ba8718"
OWNER_RESPONSE = "你现在继续修复直播回放不能自动上传的问题，并进行完整的验收，确保工作流可以正常进行。"
OWNER_REFERENCE = "chat:2026-11-03:repair-replay-auto-upload-and-validate"


def need(value, code):
    if not value:
        raise ValueError(code)


def wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(wire(value)).hexdigest()


def safe_regular(file, maximum=2_000_000):
    file = Path(file)
    need(file.is_absolute() and file.resolve() == file and
         not any(p.is_symlink() for p in (file, *file.parents)), "RETIRE_EVIDENCE_PATH_INVALID")
    info = file.lstat()
    need(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= maximum, "RETIRE_EVIDENCE_SIZE_INVALID")
    return file


def reference(file):
    file = safe_regular(file)
    raw = file.read_bytes()
    return {"file": str(file), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def pinned_document(file, expected):
    ref = reference(file)
    need(ref["sha256"] == expected, "RETIRE_EVIDENCE_CHANGED")
    try:
        value = json.loads(Path(file).read_bytes())
    except (ValueError, UnicodeError):
        raise ValueError("RETIRE_EVIDENCE_INVALID") from None
    return value, ref


def clean_platform_evidence(now):
    manager, manager_ref = pinned_document(EVIDENCE / "manager.json", MANAGER_SHA)
    editor, editor_ref = pinned_document(EVIDENCE / "editor.json", EDITOR_SHA)
    observed = []
    for value in (manager, editor):
        moment = datetime.fromisoformat(value["observed_at"].replace("Z", "+00:00"))
        need(moment.tzinfo is not None and 0 <= now - moment.timestamp() <= 7200,
             "RETIRE_PLATFORM_EVIDENCE_EXPIRED")
        need(value["target_site_uid"] == value["observed_uid"] == UID and
             value["job_id"] == CHILDREN[0] and
             value.get("mutations") is False and value.get("file_choosers") == 0,
             "RETIRE_PLATFORM_BOUNDARY_CHANGED")
        observed.append(moment.timestamp())
    text = manager["manager"]["visible_text"]
    need(manager["manager"]["url"] == "https://member.bilibili.com/platform/upload-manager/article" and
         "全部稿件 4" in text and "已发布 4" in text and
         "直播回放 P1/2" not in text and SOURCE not in text,
         "RETIRE_MANAGER_NOT_CLEAN")
    uploaders = editor["editor"]["file_inputs"]
    need(editor["editor"]["url"] == "https://member.bilibili.com/platform/upload/video/frame" and
         editor["editor"]["cards"] == [] and
         len([item for item in uploaders if item["visible"] and ".mp4" in item["accept"].lower().split(",")]) == 1 and
         all(item["file_count"] == 0 for item in uploaders),
         "RETIRE_EDITOR_NOT_CLEAN")
    return {
        "manager": manager_ref,
        "editor": editor_ref,
        "observed_at": min(observed),
        "manager_declared_all": 4,
        "manager_declared_published": 4,
        "candidate_title_or_source_absent": True,
        "visible_file_rows": 0,
        "selected_files": 0,
        "platform_mutations": 0,
    }


def job_digest(row):
    return digest(dict(row))


def current_state(settings, store, now):
    service = ReplayPartVideoWeb(settings, store)
    policy = service.policy()
    groups = ReplayPartGroupsV5(settings, store)
    groups.initialize()
    launch = groups.require_launch(policy)
    delivery = verified_delivery(settings, store, DOWNLOAD, verify_all=True)
    parent = store.get(PARENT)
    children = [store.get(ident) for ident in CHILDREN]
    group = store.query("SELECT * FROM replay_part_groups_v5 WHERE parent_job_id=?", (PARENT,))
    registry = store.query("SELECT * FROM replay_part_children_v5 WHERE parent_job_id=? ORDER BY part_index", (PARENT,))
    fifo = store.query("SELECT * FROM replay_fifo_v4 WHERE source_id=? AND download_job_id=?", (SOURCE, DOWNLOAD))
    need(len(group) == 1 and len(registry) == 2 and len(fifo) == 1,
         "RETIRE_EXACT_REGISTRY_REQUIRED")
    need(all(row is not None for row in (parent, *children)), "RETIRE_EXACT_JOBS_REQUIRED")
    need(parent["job_type"] == GROUP_JOB and parent["state"] == "upload_pending" and
         parent["stage"] == "replay_parts_children_queued" and parent["attempts"] == 0 and
         parent["external_publish_id"] is None and parent["lease_owner"] is None and parent["lease_until"] is None,
         "RETIRE_PARENT_STATE_CHANGED")
    need(children[0]["job_type"] == "replay_part_publish_v5_p01" and
         children[0]["state"] == "awaiting_review" and
         children[0]["stage"] == "replay_part_uploading" and
         children[0]["error_code"] == "REPLAY_PART_PRIVATE_UPLOAD_RECONCILIATION_REQUIRED" and
         children[0]["attempts"] == 4 and children[0]["external_publish_id"] is None and
         children[0]["lease_owner"] is None and children[0]["lease_until"] is None,
         "RETIRE_P01_STATE_CHANGED")
    need(children[1]["job_type"] == "replay_part_publish_v5_p02" and
         children[1]["state"] == "pending" and children[1]["stage"] == "replay_part_queued" and
         children[1]["attempts"] == 0 and children[1]["error_code"] is None and
         children[1]["external_publish_id"] is None and children[1]["lease_owner"] is None and
         children[1]["lease_until"] is None, "RETIRE_P02_STATE_CHANGED")
    p01_state = children[0]["result"][STATE]
    p02_state = children[1]["result"][STATE]
    need(p01_state["state"] == "uploading" and p01_state["public_submission_approved"] is False and
         p02_state["state"] == "prepared" and p02_state["public_submission_approved"] is False,
         "RETIRE_INTERRUPTED_CHECKPOINTS_CHANGED")
    need(group[0]["source_id"] == SOURCE and group[0]["target_uid"] == UID and
         group[0]["download_job_id"] == DOWNLOAD and group[0]["parts_manifest_sha256"] == PARTS_SHA and
         group[0]["policy_sha256"] == POLICY_SHA and group[0]["part_count"] == 2 and
         group[0]["state"] == "publishing", "RETIRE_GROUP_STATE_CHANGED")
    need([(row["part_index"], row["child_job_id"], row["state"], row["external_id"]) for row in registry] ==
         [(0, CHILDREN[0], "prepared", None), (1, CHILDREN[1], "prepared", None)],
         "RETIRE_CHILD_REGISTRY_CHANGED")
    need(fifo[0]["candidate_id"] == "1c64e81b-c3ec-4218-b7c0-e0235ebc918c" and
         fifo[0]["state"] == "hold" and fifo[0]["reason"] == "REPLAY_PART_CHILD_REQUIRES_RECONCILIATION" and
         fifo[0]["publication_job_id"] == PARENT, "RETIRE_FIFO_STATE_CHANGED")
    need(not store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?", (SOURCE,)) and
         not store.query("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=?", (SOURCE,)),
         "RETIRE_PUBLICATION_OR_TOMBSTONE_EXISTS")
    active = store.query("SELECT job_id FROM jobs WHERE queue='video' AND "
                         "(state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)")
    need(not active, "RETIRE_VIDEO_NOT_IDLE")
    clean = clean_platform_evidence(now)
    need(delivery["delivery"]["processing_model"] == "lossless_segmented_v4" and
         len(delivery["delivery"]["outputs"]) == 2, "RETIRE_TWO_PART_DELIVERY_REQUIRED")
    return {
        "version": VERSION,
        "source_id": SOURCE,
        "download_job_id": DOWNLOAD,
        "parent_job_id": PARENT,
        "children": list(CHILDREN),
        "group": dict(group[0]),
        "registry": [dict(row) for row in registry],
        "fifo": dict(fifo[0]),
        "jobs": [dict(parent), *(dict(row) for row in children)],
        "job_sha256": {row["job_id"]: job_digest(row) for row in (parent, *children)},
        "delivery_manifest_sha256": delivery["manifest"]["sha256"],
        "delivery_model": delivery["delivery"]["processing_model"],
        "delivery_output_sha256": [row["sha256"] for row in delivery["delivery"]["outputs"]],
        "clean_platform": clean,
        "policy_sha256": policy["_sha256"],
        "launch_policy_sha256": delivery["plan"]["policy_sha256"],
        "launch_binding_sha256": digest(launch),
        "owner_response": OWNER_RESPONSE,
        "owner_reference": OWNER_REFERENCE,
        "planned_at": now,
    }


def archive_record(settings, plan, plan_sha):
    folder = settings.root / "media" / DOWNLOAD / "replay-part-retirement-v5" / plan_sha
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    need(folder.resolve() == folder and not any(p.is_symlink() for p in (folder, *folder.parents)),
         "RETIRE_ARCHIVE_PATH_INVALID")
    file = folder / "record.json"
    atomic_json(file, plan)
    file.chmod(0o600)
    return reference(file)


def stable_plan_sha(plan):
    value = deepcopy(plan)
    value.pop("planned_at", None)
    return digest(value)


def retire(settings, store, plan, plan_sha):
    need(plan["version"] == VERSION and stable_plan_sha(plan) == plan_sha,
         "RETIRE_EXACT_PLAN_REQUIRED")
    record_ref = archive_record(settings, plan, plan_sha)
    history_suffix = "_retired_v5_" + plan_sha[:12]
    now = time.time()
    with store.transaction() as db:
        need(db.execute("UPDATE pipelines SET paused=1 WHERE name='video' AND paused=0").rowcount == 1,
             "RETIRE_PIPELINE_PAUSE_CAS_FAILED")
        for row in plan["jobs"]:
            new_type = row["job_type"] + history_suffix
            marker = {
                "version": "replay-part-interrupted-group-retirement-v5",
                "plan_sha256": plan_sha,
                "record": record_ref,
                "old_job_sha256": plan["job_sha256"][row["job_id"]],
                "old_attempts": row["attempts"],
                "old_state": row["result"].get(STATE, {}).get("state"),
                "new_attempts": 0,
                "platform_retry_performed": False,
            }
            result = deepcopy(row["result"])
            result["replay_part_retirement_v5"] = marker
            code = "REPLAY_PART_GROUP_SUPERSEDED"
            changed = db.execute(
                """UPDATE jobs SET job_type=?,state='failed',stage='superseded_after_interrupted_upload',
                   result=?,error_code=?,updated_at=?,next_run=0,lease_owner=NULL,lease_until=NULL
                   WHERE job_id=? AND job_type=? AND state=? AND attempts=? AND payload=? AND result=? AND updated_at=?
                   AND error_code IS ? AND stage=? AND lease_owner IS NULL AND lease_until IS NULL""",
                (new_type, encode(result), code, now, row["job_id"], row["job_type"], row["state"],
                 row["attempts"], encode(row["payload"]), encode(row["result"]), row["updated_at"],
                 row["error_code"], row["stage"]),
            ).rowcount
            need(changed == 1, "RETIRE_JOB_CAS_FAILED")
        need(db.execute("DELETE FROM replay_part_children_v5 WHERE parent_job_id=? AND child_job_id IN (?,?)",
                        (PARENT, *CHILDREN)).rowcount == 2, "RETIRE_CHILD_DELETE_CAS_FAILED")
        need(db.execute("DELETE FROM replay_part_groups_v5 WHERE parent_job_id=? AND source_id=? AND target_uid=? AND state='publishing'",
                        (PARENT, SOURCE, UID)).rowcount == 1, "RETIRE_GROUP_DELETE_CAS_FAILED")
        need(db.execute("""UPDATE replay_fifo_v4 SET state='download_completed',reason=NULL,publication_job_id=NULL,updated_at=?
                           WHERE candidate_id=? AND source_id=? AND download_job_id=? AND state='hold'
                           AND reason='REPLAY_PART_CHILD_REQUIRES_RECONCILIATION' AND publication_job_id=?""",
                        (now, plan["fifo"]["candidate_id"], SOURCE, DOWNLOAD, PARENT)).rowcount == 1,
             "RETIRE_FIFO_RESET_CAS_FAILED")
        audit = {
            "version": VERSION,
            "plan_sha256": plan_sha,
            "record": record_ref,
            "old_parent_job_id": PARENT,
            "old_children": list(CHILDREN),
            "old_attempts": [row["attempts"] for row in plan["jobs"]],
            "new_attempts_are_fresh_jobs": True,
            "same_attempt_retried": False,
            "clean_platform_evidence": plan["clean_platform"],
            "media_moved_or_deleted": False,
            "publications_created": 0,
        }
        db.execute("INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)",
                   (now, "owner-authorized-maintenance", "replay_part_interrupted_group_retired_v5",
                    PARENT, encode(audit)))
    groups = ReplayPartGroupsV5(settings, store)
    try:
        new_parent = groups.create(DOWNLOAD)
        with store.transaction() as db:
            need(db.execute(
                """UPDATE replay_fifo_v4 SET publication_job_id=?,state='publication_queued',
                   reason=NULL,updated_at=?
                   WHERE candidate_id=? AND source_id=? AND download_job_id=?
                   AND publication_job_id IS NULL AND state='download_completed'""",
                (new_parent, time.time(), plan["fifo"]["candidate_id"], SOURCE, DOWNLOAD),
            ).rowcount == 1, "RETIRE_FIFO_NEW_GROUP_CAS_FAILED")
        new_group = store.query("SELECT * FROM replay_part_groups_v5 WHERE parent_job_id=?", (new_parent,))
        new_children = store.query("SELECT * FROM replay_part_children_v5 WHERE parent_job_id=? ORDER BY part_index", (new_parent,))
        new_jobs = [store.get(row["child_job_id"]) for row in new_children]
        need(len(new_group) == 1 and len(new_children) == 2 and new_group[0]["part_count"] == 2 and
             new_group[0]["parts_manifest_sha256"] == PARTS_SHA and
             [job["job_type"] for job in new_jobs] == ["replay_part_publish_v5_p01", "replay_part_publish_v5_p02"] and
             all(job["state"] == "pending" and job["attempts"] == 0 for job in new_jobs),
             "RETIRE_FRESH_GROUP_INVALID")
        need(not store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?", (SOURCE,)),
             "RETIRE_FRESH_GROUP_HAS_PUBLICATION")
        with store.transaction() as db:
            need(db.execute("UPDATE pipelines SET paused=0 WHERE name='video' AND paused=1").rowcount == 1,
                 "RETIRE_PIPELINE_UNPAUSE_CAS_FAILED")
            db.execute("INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)",
                       (time.time(), "owner-authorized-maintenance", "replay_part_fresh_group_created_v5",
                        new_parent, encode({
                            "old_parent_job_id": PARENT,
                            "old_children": list(CHILDREN),
                            "new_children": [row["child_job_id"] for row in new_children],
                            "part_count": 2,
                            "attempts": [0, 0],
                            "media_reused_after_full_verification": True,
                            "platform_upload_started_by_operator": False,
                        })))
    except BaseException:
        # The reviewed retirement stands, but the pipeline remains paused.
        raise
    return {
        "state": "superseded_and_fresh_group_queued",
        "plan_sha256": plan_sha,
        "record": record_ref,
        "old_parent_job_id": PARENT,
        "old_children": list(CHILDREN),
        "new_parent_job_id": new_parent,
        "new_children": [row["child_job_id"] for row in new_children],
        "pipeline_paused": False,
        "publications_created": 0,
        "media_deleted": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-plan-sha256")
    args = parser.parse_args()
    need(os.getuid() == os.getgid() == 10001, "RETIRE_APP_UID_REQUIRED")
    from services.hub.config import Settings
    from services.hub.db import Store
    from services.hub.locks import resource_guard

    settings = Settings.load()
    need(settings.root == Path("/opt/lighthouse-hub"), "RETIRE_EXACT_ROOT_REQUIRED")
    store = Store(settings.db_path)
    with resource_guard(*(settings.root / "data/locks" / name for name in
                          ("video", "moderation", "replay-multipart-producer-v4", "replay-fifo-dispatch-v4"))):
        plan = current_state(settings, store, time.time())
        plan_sha = stable_plan_sha(plan)
        if args.apply_plan_sha256 is None:
            result = {
                "state": "verified_retirement_and_fresh_group_plan",
                "plan_sha256": plan_sha,
                "old_parent_job_id": PARENT,
                "old_children": list(CHILDREN),
                "delivery_output_sha256": plan["delivery_output_sha256"],
                "clean_platform": plan["clean_platform"],
                "publications_created": 0,
                "media_moved_or_deleted": False,
            }
        else:
            need(args.apply_plan_sha256 == plan_sha, "RETIRE_EXACT_PLAN_REQUIRED")
            result = retire(settings, store, plan, plan_sha)
        result_dir = settings.root / "media" / DOWNLOAD / "replay-part-retirement-runs"
        result_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        result_file = result_dir / (str(uuid.uuid4()) + ".json")
        atomic_json(result_file, result)
        result_file.chmod(0o600)
        print(json.dumps({"result_file": str(result_file), **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
