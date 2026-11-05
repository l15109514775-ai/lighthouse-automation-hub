import json
import time

import pytest

from services.hub.errors import HubError
from services.hub.machine_tweets import MachineTweets, owner_backfill_authorization


class Store:
    def __init__(self):
        self.tombstones = set()

    def query(self, sql, args=()):
        if "source_tombstones" in sql:
            return [(1,)] if args and args[1] in self.tombstones else []
        return []


class Settings:
    def __init__(self, root):
        self.root = root
        self.config = {}


class Service(MachineTweets):
    version = "machine-tweet-v4-api-relations"

    def __init__(self, settings, store):
        self.settings = settings
        self.store = store

    def job_policy(self):
        return self.version

    def policy(self):
        return {"max_post_age_seconds": 7200, "activation_at": 0}


def write_auth(tmp_path, entries):
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    (config / "owner-backfill-authorizations.json").write_text(
        json.dumps({"version": "owner-backfill-authorizations-v1", "authorizations": entries}),
        "utf-8",
    )


def job_for(source_id, age_seconds):
    posted = time.time() - age_seconds
    from datetime import datetime, timezone
    created = datetime.fromtimestamp(posted, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"source_id": source_id, "policy_version": "machine-tweet-v4-api-relations",
            "payload": {"post": {"created_at": created}}}


def test_authorization_is_parsed_and_bounded(tmp_path):
    now = time.time()
    write_auth(tmp_path, [{"source_id": "1999999999999999999", "authorized_at": now - 60,
                           "not_after": now + 3600, "reference": "owner-chat-2026-11-05"}])
    auth = owner_backfill_authorization(Settings(tmp_path), "1999999999999999999")
    assert auth and auth["reference"] == "owner-chat-2026-11-05"
    assert owner_backfill_authorization(Settings(tmp_path), "other") is None


def test_malformed_authorization_is_ignored(tmp_path):
    write_auth(tmp_path, [{"source_id": "1999999999999999999", "authorized_at": 1, "not_after": 2}])
    assert owner_backfill_authorization(Settings(tmp_path), "1999999999999999999") is None


def test_expired_window_raises_without_authorization(tmp_path):
    write_auth(tmp_path, [])
    service = Service(Settings(tmp_path), Store())
    with pytest.raises(HubError) as error:
        service.fresh(job_for("1999999999999999999", 9000))
    assert error.value.code == "OWNER_TWO_HOUR_WINDOW_EXPIRED"


def test_authorization_allows_expired_window(tmp_path):
    now = time.time()
    write_auth(tmp_path, [{"source_id": "1999999999999999999", "authorized_at": now - 60,
                           "not_after": now + 3600, "reference": "owner-chat-2026-11-05"}])
    service = Service(Settings(tmp_path), Store())
    assert service.fresh(job_for("1999999999999999999", 9000))["max_post_age_seconds"] == 7200


def test_expired_authorization_does_not_allow_window(tmp_path):
    now = time.time()
    write_auth(tmp_path, [{"source_id": "1999999999999999999", "authorized_at": now - 7200,
                           "not_after": now - 60, "reference": "owner-chat-2026-11-05"}])
    service = Service(Settings(tmp_path), Store())
    with pytest.raises(HubError):
        service.fresh(job_for("1999999999999999999", 9000))


def test_authorization_bypasses_only_its_own_tombstone(tmp_path):
    now = time.time()
    write_auth(tmp_path, [{"source_id": "1999999999999999999", "authorized_at": now - 60,
                           "not_after": now + 3600, "reference": "owner-chat-2026-11-05"}])
    store = Store()
    store.tombstones.update({"1999999999999999999", "1999999999999999996"})
    service = Service(Settings(tmp_path), store)
    service.require_source_not_cleared({"source_id": "1999999999999999999"})
    with pytest.raises(HubError) as error:
        service.require_source_not_cleared({"source_id": "1999999999999999996"})
    assert error.value.code == "OWNER_BACKLOG_CLEARED"
