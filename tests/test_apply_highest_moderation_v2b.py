"""Pure candidate transform/source-proof checks, never Docker/host writes."""
from copy import deepcopy
from pathlib import Path
import pytest
from scripts.ops import apply_highest_moderation_v2b as m
from scripts.ops import deploy_highest_video_worker_v2 as d

def config():
    return {'name':'Owned','services':{**{key:{'image':'Owned unchanged'} for key in d.CORE},
        'video-worker':d.new_video_service(Path('/owned/key'),{'services/hub/replay_video_browser.py':b'Owned original'})}}

def test_only_video_entry_and_three_readonly_mounts_change_and_original_is_untouched():
    before=config();saved=deepcopy(before);target=d.INSTALL/'deltas'/('moderation-'+m.COMMIT)
    after=m.candidate(before,target,d);assert before==saved
    assert after['services']['video-worker']['command']==['python','-m',m.ENTRY]
    assert all(after['services'][name]==before['services'][name] for name in d.CORE)
    for name in m.PRODUCTION:
        volumes=[v for v in after['services']['video-worker']['volumes'] if v['target']==str(d.ROOT/name)]
        assert len(volumes)==1 and volumes[0]['source']==str(target/name) and volumes[0]['read_only'] is True
    reverse=deepcopy(after);reverse['services']['video-worker']['command']=before['services']['video-worker']['command']
    reverse['services']['video-worker']['volumes']=[v for v in reverse['services']['video-worker']['volumes'] if v['target'] not in {str(d.ROOT/name) for name in m.PRODUCTION if not name.endswith('replay_video_browser.py')}]
    next(v for v in reverse['services']['video-worker']['volumes'] if v['target']==str(d.ROOT/'services/hub/replay_video_browser.py'))['source']=str(d.INSTALL/'code/services/hub/replay_video_browser.py')
    assert reverse==before

@pytest.mark.parametrize('change',['foreign_handler','writable','duplicate_new_target','wrong_entry'])
def test_unrecognized_or_weakened_existing_runtime_is_not_patched(change):
    before=config();volumes=before['services']['video-worker']['volumes'];handler=next(v for v in volumes if v['target'].endswith('replay_video_browser.py'))
    if change=='foreign_handler':handler['source']='/owned/unrecognized.py'
    elif change=='writable':handler['read_only']=False
    elif change=='duplicate_new_target':volumes.append(d.bind('/owned/source',d.ROOT/next(iter(m.PRODUCTION)),True))
    else:before['services']['video-worker']['command']=['python','-m','Owned unrecognized entry']
    with pytest.raises(AssertionError):m.candidate(before,d.INSTALL/'owned',d)

def test_already_tested_scan_delta_source_is_accepted_without_rewriting_old_code():
    before=config();handler=next(v for v in before['services']['video-worker']['volumes'] if v['target'].endswith('replay_video_browser.py'))
    handler['source']=str(d.INSTALL/'deltas/243855ac107715350b59941b4118b415ac416b7d/services/hub/replay_video_browser.py')
    after=m.candidate(before,d.INSTALL/'new',d)
    assert next(v for v in after['services']['video-worker']['volumes'] if v['target'].endswith('replay_video_browser.py'))['source']==str(d.INSTALL/'new/services/hub/replay_video_browser.py')

def test_verified_helper_is_not_reopened_after_hashing(tmp_path,monkeypatch):
    file=tmp_path/'owned.py';raw=b'VALUE=41\n';file.write_bytes(raw);expected=m.hashlib.sha256(raw).hexdigest()
    original=Path.read_bytes
    def read_once(path):
        value=original(path)
        if path==file:file.write_bytes(b'VALUE=999\n')
        return value
    monkeypatch.setattr(Path,'read_bytes',read_once)
    assert m.load(file,expected,'owned_pinned_helper').VALUE==41

def test_preflight_blocks_typed_claim_and_worker_consumption_before_factory():
    text=m.precheck_script(d);compile(text,'owned preflight','exec')
    factory=text.index('worker,service=build_worker')
    assert text.index('Worker.once=Worker.run=refuse')<factory
    assert text.index('QueueView.claim=QueueView.recover=refuse')<factory
    assert text.index('Store.claim=Store.recover=Store.enqueue=refuse')<factory
