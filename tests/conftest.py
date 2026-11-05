from copy import deepcopy
from pathlib import Path
import tomllib
import pytest
from services.hub.config import Settings
from services.hub.db import Store


@pytest.fixture
def runtime(tmp_path):
    root = Path(__file__).resolve().parents[1]
    cfg = tomllib.loads((root / 'config/hub.toml').read_text('utf-8'))
    settings = Settings(tmp_path, deepcopy(cfg))
    settings.prepare()
    store = Store(settings.db_path)
    store.init()
    return settings, store


def enqueue(store, kind='test', queue='tweet', source_id='123'):
    return store.enqueue('x', source_id, kind, queue, {'synthetic': True}, 'human-review-v1')
