"""Resume one verified uploaded V5 part after a metadata-only code repair."""
from copy import deepcopy
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.locks import resource_guard
from services.hub.media_processing import atomic_json
from services.hub.replay_parts_v5 import STATE, ReplayPartVideoWeb, reference_body

VERSION = "resume-uploaded-replay-part-after-metadata-fix-v5"
JOB = "14bd3ee4-b7d8-4ffe-bd8b-54a2af94610e"
PARENT = "1e28dd0f-cc2d-4928-bfd6-e373036c193a"
DOWNLOAD = "5af6b351-e73a-4451-b32a-cc845a2892f1"
ATTEMPT = "9e794f91-dfc9-4e22-a5ed-cdf95af215e0"
SOURCE = "EoZFofw4OSU"
ERRORS = ("REPLAY_BROWSER_METADATA_PREPARATION_FAILED",
          "REPLAY_BROWSER_METADATA_READBACK_FAILED")
EVIDENCE = Path(
    "/opt/lighthouse-hub/media/14bd3ee4-b7d8-4ffe-bd8b-54a2af94610e"
)
MANAGER = EVIDENCE / "interrupted-upload-readonly/2026-11-03T19-28-20-120Z/manager.json"
EDITOR = EVIDENCE / "saved-draft-status-readonly/2026-11-03T19-28-55-899Z/dom.json"
MANAGER_SHA = "05ac3164b53627a81652014b1b1c3b1172d9058a22d934d2719799b8040d7622"
EDITOR_SHA = "1332bdd17cdd4ed822647e3e8d402a40effc5b3e0135276d0593c2eb6fa269b4"


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


def document(file, expected_sha):
    file = Path(file)
    need(file.is_absolute() and file.resolve() == file and
         not any(p.is_symlink() for p in (file, *file.parents)), "RESUME_EVIDENCE_PATH_INVALID")
    raw = file.read_bytes()
    need(0 < len(raw) <= 2_000_000 and hashlib.sha256(raw).hexdigest() == expected_sha,
         "RESUME_EVIDENCE_CHANGED")
    return json.loads(raw), {"file": str(file), "bytes": len(raw), "sha256": expected_sha}


def document_ref(file):
    file = Path(file)
    need(file.is_absolute() and file.resolve() == file and
         not any(p.is_symlink() for p in (file, *file.parents)), "RESUME_EVIDENCE_PATH_INVALID")
    raw = file.read_bytes()
    need(0 < len(raw) <= 2_000_000, "RESUME_EVIDENCE_SIZE_INVALID")
    return json.loads(raw), {"file": str(file), "bytes": len(raw),
                             "sha256": hashlib.sha256(raw).hexdigest()}


def current_state(settings, store, now):
    service = ReplayPartVideoWeb(settings, store)
    job, binding, state = service.snapshot(JOB, write=False)
    policy = service._validate_snapshot_binding(job, binding, verify_artifact_hash=False)
    service._require_workflow_launch(policy)
    need(job["job_id"] == JOB and job["source_id"] == SOURCE and
         job["job_type"] == "replay_part_publish_v5_p01" and
         job["policy_version"] == "replay-part-video-policy-v5" and
         job["state"] == "awaiting_review" and
         job["stage"] in ("replay_part_uploaded", "replay_part_uploaded_not_clicked") and
         job["attempts"] in (1, 3) and job["error_code"] in ERRORS and
         job["external_publish_id"] is None and job["lease_owner"] is None and job["lease_until"] is None,
         "RESUME_EXACT_JOB_REQUIRED")
    need(binding["parent_job_id"] == PARENT and binding["download_job_id"] == DOWNLOAD and
         binding["part_index"] == 0 and binding["part_count"] == 2 and
         state["state"] == "uploaded" and state["attempt_id"] == ATTEMPT and
         state["private_upload_selections"] == 1 and
         state["public_submission_approved"] is False and state["human_review"] is False and
         job["result"]["publish_performed"] is False and
         state.get("upload_observation", {}).get("same_attempt_observed") is True,
         "RESUME_UPLOAD_CHECKPOINT_CHANGED")
    folder = settings.root / "media" / JOB / service.NATIVE_DIRECTORY / ATTEMPT
    upload_ref = state["upload_evidence"]
    upload_body, upload_actual = document_ref(folder / "upload-observation.json")
    need(upload_actual == upload_ref and upload_body == state["upload_observation"],
         "RESUME_UPLOAD_EVIDENCE_REFERENCE_CHANGED")
    observed = state["upload_observation"]
    coverage = reference_body(
        settings.root,
        observed["upload_coverage"],
        expected=folder / "upload-coverage.json",
    )
    need(coverage == state["upload_coverage"] and
         coverage["version"] == "replay-part-native-upload-coverage-v5" and
         coverage["job_id"] == JOB and coverage["attempt_id"] == ATTEMPT and
         coverage["artifact_sha256"] == binding["artifact_sha256"] and
         coverage["streamed_sha256"] == binding["artifact_sha256"] and
         coverage["streamed_bytes"] == binding["file_bytes"] and
         coverage["whole_file_streamed"] is True and coverage["native_upload_complete"] is True and
         coverage["server_file_hash_verified"] is False and coverage["human_review"] is False,
         "RESUME_UPLOAD_COVERAGE_CHANGED")
    intent, intent_ref = document_ref(
        settings.root / "media" / JOB / "replay-native-intents/video-selection.json"
    )
    # The upload-reference digest binds this exact intent; it is not the file
    # SHA. Recompute the reference instead of treating the intent bytes as a
    # second media hash.
    need(intent["attempt_id"] == ATTEMPT and intent["artifact_sha256"] == binding["artifact_sha256"] and
         intent["part_idempotency_key"] == binding["part_idempotency_key"],
         "RESUME_SELECTION_INTENT_CHANGED")
    native_base = service._native_base(job, binding, state)
    expected_upload_reference = digest({
        **native_base,
        "local_causal_intent": intent_ref,
        "native_upload_complete": True,
        "upload_coverage": observed["upload_coverage"],
    })
    need(observed["upload_reference_sha256"] == expected_upload_reference,
         "RESUME_UPLOAD_REFERENCE_CHANGED")
    manager, manager_ref = document(MANAGER, MANAGER_SHA)
    editor, editor_ref = document(EDITOR, EDITOR_SHA)
    manager_text = manager["manager"]["visible_text"]
    need(manager["target_site_uid"] == manager["observed_uid"] == binding["site_uid"] and
         manager["job_id"] == JOB and manager.get("mutations") is False and
         "全部稿件 4" in manager_text and "草稿 1" in manager_text and
         "进行中 0" in manager_text and "已发布 4" in manager_text and
         "直播回放 P1/2" not in manager_text and SOURCE not in manager_text,
         "RESUME_MANAGER_NOT_CLEAN")
    need(editor["job_id"] == JOB and editor["attempt_id"] == ATTEMPT and
         editor.get("mutations") is False and
         editor["navigation"] == "saved_draft_opened" and
         editor["result"]["editor_ready"] is True and
         any(node["classes"] == "success" and node["text"] == "上传完成"
             for node in editor["result"]["success_nodes"]),
         "RESUME_EDITOR_SESSION_UNAVAILABLE")
    need(not store.query("SELECT 1 FROM publications WHERE job_id=? OR (source_platform='youtube' AND source_id=?)",
                         (JOB, SOURCE)), "RESUME_PUBLICATION_ALREADY_EXISTS")
    active = store.query("SELECT job_id FROM jobs WHERE queue='video' AND "
                         "(state='processing' OR lease_owner IS NOT NULL OR lease_until IS NOT NULL)")
    need(not active, "RESUME_VIDEO_NOT_IDLE")
    plan = {
        "version": VERSION,
        "job_id": JOB,
        "parent_job_id": PARENT,
        "download_job_id": DOWNLOAD,
        "attempt_id": ATTEMPT,
        "attempts_before": job["attempts"],
        "job_state_before": job["state"],
        "job_stage_before": job["stage"],
        "job_error_before": job["error_code"],
        "row_sha256": digest(job),
        "upload_evidence": upload_ref,
        "upload_coverage": observed["upload_coverage"],
        "selection_intent": intent_ref,
        "manager_evidence": manager_ref,
        "editor_evidence": editor_ref,
        "state_before": deepcopy(state),
        "policy_sha256": policy["_sha256"],
        "planned_at": now,
    }
    return job, state, plan


def apply(settings, store, plan, plan_sha):
    need(plan["version"] == VERSION and stable_plan_sha(plan) == plan_sha,
         "RESUME_EXACT_PLAN_REQUIRED")
    now = time.time()
    with store.transaction() as db:
        current = db.execute("SELECT * FROM jobs WHERE job_id=?", (JOB,)).fetchone()
        need(current is not None, "RESUME_JOB_MISSING")
        row = dict(current)
        for key in ("payload", "result"):
            row[key] = json.loads(row[key])
        need(digest(row) == plan["row_sha256"], "RESUME_JOB_CHANGED")
        result = deepcopy(row["result"])
        marker = {
            "version": "uploaded-metadata-fix-resume-v5",
            "plan_sha256": plan_sha,
            "attempt_id": ATTEMPT,
            "attempts_before": row["attempts"],
            "attempts_reset": False,
            "upload_observation_reused": True,
            "reupload_performed": False,
            "public_submission_created": False,
            "repair_error": plan["job_error_before"],
            "manager_evidence": plan["manager_evidence"],
            "editor_evidence": plan["editor_evidence"],
        }
        result["uploaded_metadata_fix_resume_v5"] = marker
        changed = db.execute(
            """UPDATE jobs SET state='pending',stage='uploaded_repair_resume',error_code=NULL,
               result=?,next_run=0,updated_at=?,lease_owner=NULL,lease_until=NULL
               WHERE job_id=? AND state=? AND stage=?
               AND attempts=? AND error_code=? AND external_publish_id IS NULL
               AND lease_owner IS NULL AND lease_until IS NULL AND payload=? AND result=?""",
            (encode(result), now, JOB, plan["job_state_before"], plan["job_stage_before"],
             plan["attempts_before"], plan["job_error_before"],
             encode(row["payload"]), encode(row["result"])),
        ).rowcount
        need(changed == 1, "RESUME_JOB_CAS_FAILED")
        db.execute("INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)",
                   (now, "owner-authorized-maintenance", "replay_uploaded_metadata_fix_resume_v5",
                    JOB, encode(marker)))
    after = store.get(JOB)
    need(after["state"] == "pending" and after["stage"] == "uploaded_repair_resume" and
         after["error_code"] is None and after["attempts"] == plan["attempts_before"] and
         after["payload"] == row["payload"] and after["external_publish_id"] is None,
         "RESUME_POSTCONDITION_FAILED")
    result = {
        "version": VERSION,
        "state": "pending_same_uploaded_attempt",
        "plan_sha256": plan_sha,
        "job_id": JOB,
        "attempts": after["attempts"],
        "attempt_id": ATTEMPT,
        "upload_reused": True,
        "reupload_performed": False,
        "public_submission_created": False,
        "after_row_sha256": digest(after),
    }
    folder = settings.root / "media" / JOB / "uploaded-metadata-fix-resume-v5"
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = folder / (plan_sha + ".json")
    atomic_json(target, result)
    target.chmod(0o600)
    return {**result, "result_file": str(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply-plan-sha256")
    args = parser.parse_args()
    need(os.getuid() == os.getgid() == 10001, "RESUME_APP_UID_REQUIRED")
    from services.hub.config import Settings

    settings = Settings.load()
    need(settings.root == Path("/opt/lighthouse-hub"), "RESUME_EXACT_ROOT_REQUIRED")
    from services.hub.db import Store

    store = Store(settings.db_path)
    with resource_guard(*(settings.root / "data/locks" / name for name in
                          ("video", "moderation", "replay-multipart-producer-v4", "replay-fifo-dispatch-v4"))):
        _, _, plan = current_state(settings, store, time.time())
        plan_sha = stable_plan_sha(plan)
        if args.apply_plan_sha256 is None:
            result = {
                "state": "verified_uploaded_metadata_fix_resume_plan",
                "plan_sha256": plan_sha,
                "job_id": JOB,
                "attempts_before": plan["attempts_before"],
                "upload_reused": True,
                "reupload_performed": False,
                "public_submission_created": False,
            }
        else:
            need(args.apply_plan_sha256 == plan_sha, "RESUME_EXACT_PLAN_REQUIRED")
            result = apply(settings, store, plan, plan_sha)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
