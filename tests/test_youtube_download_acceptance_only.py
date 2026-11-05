"""Owned replay metadata only. Pilot gate never downloads or changes policy dates."""
from pathlib import Path
import pytest
from test_youtube_download import replay,write_policy,VIDEO,unchanged_old_and_no_publish
from services.hub.config import Settings
from services.hub.errors import HubError
from services.hub.youtube_monitor import YouTubeMonitor


def test_acceptance_only_skips_automatic_before_any_policy_or_payload_access(replay,monkeypatch):
    f=replay;f.settings.config['youtube_download_acceptance_only']=True;service=f.service()
    before=f.store.query('SELECT * FROM jobs');events=f.store.query('SELECT * FROM events')
    def forbidden(*args,**kwargs):raise AssertionError('Automatic pilot must not enter policy/enqueue/download')
    monkeypatch.setattr(service,'_enqueue',forbidden)
    assert service.enqueue_if_eligible(None) is None
    assert f.store.query('SELECT * FROM jobs')==before and f.store.query('SELECT * FROM events')==events
    assert not f.calls and not f.store.query('SELECT * FROM api_calls')


def test_monitor_automatic_route_still_records_metadata_but_creates_no_download_job(replay):
    f=replay;f.settings.config['youtube_download_acceptance_only']=True
    policy=f.settings.root/'config/youtube-download-policy.toml';before=policy.read_bytes()
    assert YouTubeMonitor(f.settings,f.store,None).observe_video(f.raw,f.payload['channel_id'])=='ended'
    assert not f.store.query("SELECT * FROM jobs WHERE job_type='youtube_download'")
    assert policy.read_bytes()==before and not f.calls
    unchanged_old_and_no_publish(f)


def test_explicit_acceptance_stays_authorized_deduplicated_and_separate_from_automatic(replay):
    f=replay;f.settings.config['youtube_download_acceptance_only']=True
    path=write_policy(f,acceptance_video_ids=[VIDEO]);policy_before=path.read_bytes()
    first=f.service().enqueue_acceptance(f.payload);second=f.service().enqueue_acceptance(f.payload)
    assert first==second and first
    rows=f.store.query("SELECT * FROM jobs WHERE job_type='youtube_download'")
    assert len(rows)==1 and rows[0]['job_id']==first and rows[0]['state']=='pending'
    assert f.store.get(first)['payload']['_download_mode']=='acceptance'
    assert f.service().enqueue_if_eligible(f.payload) is None
    assert path.read_bytes()==policy_before and not f.calls
    unchanged_old_and_no_publish(f)


def test_acceptance_only_does_not_create_an_explicit_whitelist_or_bypass_enabled_switch(replay):
    f=replay;f.settings.config['youtube_download_acceptance_only']=True
    with pytest.raises(HubError,match='YOUTUBE_DOWNLOAD_ACCEPTANCE_NOT_AUTHORIZED'):f.service().enqueue_acceptance(f.payload)
    write_policy(f,acceptance_video_ids=[VIDEO]);f.settings.config['youtube_download_enabled']=False
    with pytest.raises(HubError,match='YOUTUBE_DOWNLOAD_DISABLED'):f.service().enqueue_acceptance(f.payload)
    assert not f.store.query("SELECT * FROM jobs WHERE job_type='youtube_download'") and not f.calls
    unchanged_old_and_no_publish(f)


@pytest.mark.parametrize('mode',['missing',False])
def test_default_or_false_preserves_existing_automatic_enqueuing(replay,mode):
    f=replay
    if mode=='missing':f.settings.config.pop('youtube_download_acceptance_only',None)
    else:f.settings.config['youtube_download_acceptance_only']=False
    first=f.service().enqueue_if_eligible(f.payload)
    assert first and f.service().enqueue_if_eligible(f.payload)==first
    assert f.store.get(first)['payload']['_download_mode']=='automatic'
    assert len(f.store.query("SELECT * FROM jobs WHERE job_type='youtube_download'"))==1 and not f.calls
    unchanged_old_and_no_publish(f)


def load_config(tmp_path,monkeypatch,prefix=''):
    source=Path(__file__).resolve().parents[1]/'config/hub.toml';target=tmp_path/'pilot-hub.toml'
    target.write_text(prefix+source.read_text('utf-8-sig'),'utf-8')
    monkeypatch.setenv('HUB_ROOT',str(tmp_path));monkeypatch.setenv('HUB_CONFIG',str(target))
    return Settings.load()


def test_settings_missing_field_has_explicit_false_default(tmp_path,monkeypatch):
    settings=load_config(tmp_path,monkeypatch)
    assert settings.config['youtube_download_acceptance_only'] is False


@pytest.mark.parametrize('value',['true','false'])
def test_settings_accepts_only_literal_boolean_values(tmp_path,monkeypatch,value):
    settings=load_config(tmp_path,monkeypatch,'youtube_download_acceptance_only = '+value+'\n')
    assert settings.config['youtube_download_acceptance_only'] is (value=='true')


@pytest.mark.parametrize('value',['"true"','1','0','1.0','[]','{}'])
def test_settings_rejects_nonboolean_acceptance_only_values(tmp_path,monkeypatch,value):
    with pytest.raises(ValueError,match='acceptance-only'):
        load_config(tmp_path,monkeypatch,'youtube_download_acceptance_only = '+value+'\n')
