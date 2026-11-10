"""Registry isolation with real settings/SQLite; no external requests."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from services.hub.cli import runtime as handlers
from services.hub.worker import Worker


OWNED = {
    'tweet': {'x_discovery', 'tweet_process', 'tweet_web_publish',
              'tweet_web_reconcile', 'tweet_machine_publish',
              'tweet_machine_reconcile', 'x_official_media_ingest_v1',
              'x_official_cached_reparse_v1', 'tweet_repost_check_v1'},
    'video': {'youtube_discovery', 'video_process', 'youtube_download'},
    'system': {'maintenance', 'backup', 'cleanup', 'youtube_feed',
               'youtube_live_monitor', 'bilibili_reconcile', 'bilibili_token_check'},
}


def test_union_preserves_compatibility_and_queues_have_single_owners(runtime):
    settings, store = runtime
    scoped = {queue: handlers(settings, store, queue=queue) for queue in OWNED}
    union = handlers(settings, store)
    assert len(union) == sum(map(len, scoped.values()))
    for queue, methods in scoped.items():
        assert set(methods) == OWNED[queue]
        for key, method in methods.items():
            assert method.__func__ is union[key].__func__


@pytest.mark.parametrize('queue', list(OWNED))
def test_wrong_type_cannot_be_claimed_even_if_row_has_matching_queue(runtime, queue):
    settings, store = runtime
    kind = 'video_process' if queue != 'video' else 'tweet_process'
    ident = store.enqueue('synthetic', 'wrong-type', kind, queue, {}, 'synthetic')
    before = store.get(ident)
    assert Worker(settings, store, handlers(settings, store, queue=queue)).once(queue) is False
    assert store.get(ident) == before


@pytest.mark.parametrize('queue,blocked', [
    ('system', ['services.hub.video_pipeline', 'services.hub.pipelines',
                'services.hub.machine_routes', 'services.hub.replay_parts_v5']),
    ('video', ['services.hub.pipelines', 'services.hub.machine_routes']),
    ('tweet', ['services.hub.video_pipeline', 'services.hub.youtube_download_v4']),
])
def test_unrelated_business_modules_are_not_imported(runtime, queue, blocked):
    settings, _ = runtime
    # A fresh interpreter detects imports hidden by pytest's shared module cache.
    script = '''
import importlib.abc,json,sys
from pathlib import Path
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.cli import runtime
queue,blocked,root,cfg=json.loads(sys.argv[1])
class RejectUnrelated(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname in blocked:raise RuntimeError('UNRELATED_BUSINESS_IMPORT')
sys.meta_path.insert(0,RejectUnrelated())
settings=Settings(Path(root),cfg)
assert runtime(settings,Store(settings.db_path),queue=queue)
'''
    subprocess.run([sys.executable, '-B', '-c', script,
                    json.dumps([queue, blocked, str(settings.root), settings.config])],
                   check=True, timeout=20, cwd=Path(__file__).resolve().parents[1])


@pytest.mark.parametrize('queue', ['', 'moderation', [], 1])
def test_unknown_queue_refused_without_constructing_services(runtime, queue):
    settings, store = runtime
    with pytest.raises(ValueError, match='WORKER_QUEUE_INVALID'):
        handlers(settings, store, queue=queue)
