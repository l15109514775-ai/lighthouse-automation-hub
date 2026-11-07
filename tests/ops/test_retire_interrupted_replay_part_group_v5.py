from datetime import datetime, timezone
from pathlib import Path
import hashlib
import importlib.util
import json

import pytest

path = Path(__file__).resolve().parents[2] / "scripts/ops/retire_interrupted_replay_part_group_v5.py"
spec = importlib.util.spec_from_file_location("owned_retire_interrupted_group", path)
op = importlib.util.module_from_spec(spec)
spec.loader.exec_module(op)


def write(path, value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def evidence(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    common = {
        "job_id": op.CHILDREN[0],
        "attempt_id": "2f9a943c-2b62-4a2d-a54d-86cd9613249f",
        "target_site_uid": op.UID,
        "observed_uid": op.UID,
        "observed_at": now,
        "mutations": False,
        "file_choosers": 0,
    }
    manager = {
        "version": "bili-interrupted-upload-readonly-manager-v1",
        **common,
        "manager": {
            "url": "https://member.bilibili.com/platform/upload-manager/article",
            "visible_text": "全部稿件 4\n已发布 4\n旧稿件",
        },
    }
    editor = {
        "version": "bili-interrupted-upload-readonly-editor-v1",
        **common,
        "editor": {
            "url": "https://member.bilibili.com/platform/upload/video/frame",
            "cards": [],
            "file_inputs": [
                {"visible": True, "accept": ".mp4,.mkv", "file_count": 0},
                {"visible": True, "accept": ".txt", "file_count": 0},
            ],
        },
    }
    folder = tmp_path / "evidence"
    folder.mkdir()
    manager_sha = write(folder / "manager.json", manager)
    editor_sha = write(folder / "editor.json", editor)
    monkeypatch.setattr(op, "EVIDENCE", folder)
    monkeypatch.setattr(op, "MANAGER_SHA", manager_sha)
    monkeypatch.setattr(op, "EDITOR_SHA", editor_sha)
    return manager, editor


def test_clean_platform_evidence_requires_both_empty_native_surfaces(tmp_path, monkeypatch):
    evidence(tmp_path, monkeypatch)
    value = op.clean_platform_evidence(datetime.now(timezone.utc).timestamp())
    assert value["manager_declared_all"] == 4
    assert value["manager_declared_published"] == 4
    assert value["candidate_title_or_source_absent"] is True
    assert value["visible_file_rows"] == value["selected_files"] == 0
    assert value["platform_mutations"] == 0


def test_clean_platform_evidence_rejects_existing_selected_file(tmp_path, monkeypatch):
    manager, editor = evidence(tmp_path, monkeypatch)
    editor["editor"]["file_inputs"][0]["file_count"] = 1
    monkeypatch.setattr(op, "EDITOR_SHA", write(op.EVIDENCE / "editor.json", editor))
    with pytest.raises(ValueError, match="RETIRE_EDITOR_NOT_CLEAN"):
        op.clean_platform_evidence(datetime.now(timezone.utc).timestamp())


def test_plan_hash_excludes_observation_generation_time():
    plan = {"version": op.VERSION, "planned_at": 1, "fixed": ["a", "b"]}
    later = {**plan, "planned_at": 2}
    assert op.stable_plan_sha(plan) == op.stable_plan_sha(later)
