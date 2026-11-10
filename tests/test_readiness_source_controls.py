"""Check the V4 numbered job through the normal worker with one owner paused."""
from copy import deepcopy
import json

import pytest

from services.hub.errors import HubError
from services.hub.replay_fifo_v4 import DOWNLOAD_JOB
from services.hub.source_controls import guard_job
from services.hub.worker import Worker
from services.hub.youtube_readiness_v4 import JOB_TYPE, WholeReadinessObservationV4
from test_youtube_readiness_v4 import readiness, fifo, begin, advance


def test_numbered_observation_can_complete_with_other_channel_paused(readiness, monkeypatch):
    f = readiness
    set_paused_channel(f, 'UC' + 'z' * 22)
    begin(f)
    advance(f, monkeypatch)
    child_id, = f.coordinator.tick()['enqueued']
    f.ready = True
    service = WholeReadinessObservationV4(f.s, f.db, coordinator=f.coordinator, downloader_factory=f.factory)
    assert Worker(f.s, f.db, {JOB_TYPE: service.process}).once('video')
    assert f.db.get(child_id)['state'] == f.db.get(f.parent_id)['state'] == 'completed'
    assert f.count == 2 and not f.db.query('SELECT * FROM publications')


def set_paused_channel(f, channel):
    file = f.s.root / 'config/source-controls.json'
    file.write_text(json.dumps({'version': 'source-controls-v1', 'paused_x_handles': [],
                                'paused_youtube_channel_ids': [channel], 'reason': 'test'}))
    file.chmod(0o640)


def test_numbered_readiness_checks_parent_owner_and_rejects_changed_identity(readiness, monkeypatch):
    f = readiness
    begin(f)
    advance(f, monkeypatch)
    child = f.db.get(f.coordinator.tick()['enqueued'][0])
    set_paused_channel(f, child['payload']['channel_id'])
    with pytest.raises(HubError, match='SOURCE_FORWARDING_PAUSED'):
        guard_job(f.s, f.db, child)

    for field, value in (('video_id', 'other000001'), ('channel_id', 'UC' + 'z' * 22),
                         ('_whole_readiness_parent_job_id', child['job_id']),
                         ('_whole_readiness_sequence', 3), ('_whole_readiness_sequence', True)):
        changed = deepcopy(child)
        changed['payload'][field] = value
        with pytest.raises(HubError, match='SOURCE_FORWARDING_IDENTITY_REQUIRED'):
            guard_job(f.s, f.db, changed)
    changed = {**child, 'job_type': DOWNLOAD_JOB}
    with pytest.raises(HubError, match='SOURCE_FORWARDING_IDENTITY_REQUIRED'):
        guard_job(f.s, f.db, changed)
    assert f.count == 1 and not f.db.query('SELECT * FROM publications')
