"""Actual owned track bytes/packet chains, no platform or publication access."""
from copy import deepcopy
from pathlib import Path
import pytest

from test_short_video_web import short_video
from test_video_source_retention_v2 import owned
from test_video_source_retention_tracks_v2 import tracks,natural_tracks,bind_receipt_and_native,planned,allow
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.video_source_retention_natural_formats_v3 import NaturalFormatVideoSourceRetentionV3


def renamed_ids(f,identifiers,*,rename=True):
    """Rebuild honest owned protocol bindings, not a real acceptance record."""
    source=f['store'].get(f['source']);state='youtube_download_v3' if 'youtube_download_v3' in source['result'] else 'youtube_download_v2'
    cp=source['result'][state];selected=cp['binding']['source_plan']['selected_formats']
    for number,(record,value) in enumerate(zip(selected,identifiers)):
        previous=Path(f['file']).parent/('stream-'+record['format_id']+'.'+record['ext'])
        if rename:
            target=previous.with_name('stream-'+value+'.'+record['ext']);previous.rename(target)
            f['video' if number==0 else 'audio']=target
        record['format_id']=value
    for value in (cp['source_asset'],cp['receipt']):
        value['source_plan']=deepcopy(cp['binding']['source_plan']);value['selected_formats']=deepcopy(selected)
    for record,value in zip(cp['receipt']['tracks'],identifiers):record['format_id']=value
    with f['store'].transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(source['result']),f['source']))
    bind_receipt_and_native(f)
    f['service']=NaturalFormatVideoSourceRetentionV3(f['settings'],f['store']);return f


def test_natural_suffix_ids_reuse_exact_producer_contract_and_delete_only_three_bound_files(tracks,monkeypatch):
    f=renamed_ids(natural_tracks(tracks),('video_137-high','audio_141-drc'));allow(f,monkeypatch)
    before=f['store'].query('SELECT * FROM publications');plan=planned(f)
    assert [Path(v['file']).name for v in plan['candidates']]==['stream-video_137-high.mp4','stream-audio_141-drc.m4a','original.mp4']
    result=f['service'].apply(plan)
    assert result['local_files_deleted']==3 and result['source_tracks_deleted']==2
    assert all(not Path(v['file']).exists() for v in plan['candidates'])
    assert f['public'].exists() and (f['file'].parent/'verified.json').exists()
    assert f['store'].query('SELECT * FROM publications')==before and f['service'].apply(plan)==result


def test_highest_v2_suffix_id_remains_rejected_even_by_new_class(tracks):
    f=renamed_ids(tracks,('299-high','140'))
    with pytest.raises(HubError,match='VIDEO_RETENTION_TRACK_RECEIPT_CHANGED'):planned(f)
    assert f['video'].exists() and f['audio'].exists() and f['file'].exists()


@pytest.mark.parametrize('invalid',['../137','137/alt','137.0','x'*65,'中文'])
def test_natural_invalid_identifier_cannot_escape_canonical_track_path_or_producer_contract(tracks,invalid):
    f=renamed_ids(natural_tracks(tracks),(invalid,'141'),rename=False)
    with pytest.raises(HubError,match='VIDEO_RETENTION_TRACK_RECEIPT_CHANGED'):planned(f)
    assert f['video'].exists() and f['audio'].exists() and f['file'].exists()


def test_valid_suffix_does_not_accept_a_passed_flag_or_old_release_filename(tracks):
    f=renamed_ids(natural_tracks(tracks),('137-h264','141-aac'));plan=planned(f)
    old=f['settings'].root/'config/video-source-retention-tracks-v2-release.json';old.write_text('{"passed":true}','utf-8')
    assert f['service'].RELEASE_FILENAME!='video-source-retention-tracks-v2-release.json'
    with pytest.raises(HubError,match='VIDEO_RETENTION_RELEASE_REQUIRED'):f['service'].apply(plan)
    assert f['video'].exists() and f['audio'].exists() and f['file'].exists()
