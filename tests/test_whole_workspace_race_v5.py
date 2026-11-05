"""Real filesystem renames/growth at the parent's space-estimate boundary."""
import os,stat
from pathlib import Path

import pytest

from adapters.ytdlp_whole_v4 import needed_disk
from services.hub.errors import HubError
from services.hub.media_processing import atomic_json

BUDGET=1_000_000
BINDING={'source_plan':{'required_working_bytes':BUDGET}}


def during_scan(monkeypatch,area,operation):
    original=Path.rglob
    def entries(self,pattern):
        for file in list(original(self,pattern)):
            if self==area:operation(file)
            yield file
    monkeypatch.setattr(Path,'rglob',entries)


def test_real_progress_temporary_rename_during_scan_conservatively_reserves_more(tmp_path,monkeypatch):
    area=tmp_path/'workspace';area.mkdir();media=area/'stream-299.mp4.part';media.write_bytes(b'actual-owned-part')
    temporary=area/'progress.partial.json';temporary.write_bytes(b'{"downloading":true}')
    before=media.read_bytes()
    during_scan(monkeypatch,area,lambda file:temporary.replace(area/'progress.json') if file==temporary else None)
    # The newly renamed metadata is not credited: reserve is larger, not less.
    assert needed_disk(area,BINDING)==BUDGET-len(before)
    assert media.read_bytes()==before and (area/'progress.json').read_bytes()==b'{"downloading":true}'


def test_current_progress_atomic_replacement_and_growing_media_are_normal(tmp_path,monkeypatch):
    area=tmp_path/'workspace';area.mkdir();media=area/'stream-299.mp4.part';media.write_bytes(b'first')
    atomic_json(area/'progress.json',{'bytes':5})
    def replace(file):
        if file.name=='progress.json':atomic_json(file,{'bytes':100,'state':'downloading'})
        elif file==media:
            with media.open('ab') as stream:stream.write(b'-more')
    during_scan(monkeypatch,area,replace)
    actual=needed_disk(area,BINDING)
    assert actual==BUDGET-media.stat().st_size-(area/'progress.json').stat().st_size
    assert media.read_bytes()==b'first-more'


@pytest.mark.parametrize('name',['stream-299.mp4.part','progress.json','other.partial.json'])
def test_missing_media_or_unrecognized_metadata_is_still_rejected(tmp_path,monkeypatch,name):
    file=tmp_path/name;file.write_bytes(b'owned')
    during_scan(monkeypatch,tmp_path,lambda entry:entry.unlink() if entry==file else None)
    with pytest.raises(HubError,match='YOUTUBE_WHOLE_WORKSPACE_INVALID'):needed_disk(tmp_path,BINDING)


@pytest.mark.parametrize('kind',['symlink','unknown_directory','nested_directory'])
def test_existing_workspace_path_protections_remain_enforced(tmp_path,monkeypatch,kind):
    area=tmp_path/'workspace';area.mkdir()
    if kind=='symlink':
        outside=tmp_path/'outside';outside.write_bytes(b'never read');link=area/'progress.partial.json'
        try:link.symlink_to(outside)
        except OSError as error:
            if os.name!='nt' or getattr(error,'winerror',None)!=1314:raise
            # Ubuntu creates the real link. Windows without SeCreateSymbolicLink
            # exercises the no-follow metadata boundary without a skipped case.
            link.write_bytes(b'owned platform seam');original=Path.lstat
            def symlink_mode(file,*args,**kwargs):
                value=original(file,*args,**kwargs)
                return os.stat_result((stat.S_IFLNK|0o600,*tuple(value)[1:])) if file==link else value
            monkeypatch.setattr(Path,'lstat',symlink_mode)
    elif kind=='unknown_directory':(area/'unknown').mkdir()
    else:(area/'parts-v4'/'nested').mkdir(parents=True)
    with pytest.raises(HubError,match='YOUTUBE_WHOLE_WORKSPACE_INVALID'):needed_disk(area,BINDING)


@pytest.mark.parametrize('kind',['size','entry_count'])
def test_budget_and_entry_count_limits_remain_enforced(tmp_path,kind):
    if kind=='size':
        with (tmp_path/'stream-299.mp4.part').open('wb') as stream:stream.truncate(BUDGET+8_388_609)
        code='YOUTUBE_WHOLE_WORKSPACE_SIZE_LIMIT'
    else:
        for n in range(257):(tmp_path/str(n)).touch()
        code='YOUTUBE_WHOLE_WORKSPACE_INVALID'
    with pytest.raises(HubError,match=code):needed_disk(tmp_path,BINDING)


@pytest.mark.parametrize('format_id,ext',[('299','mp4'),('140','m4a')])
def test_real_bound_track_completion_rename_is_counted_once(tmp_path,monkeypatch,format_id,ext):
    partial=tmp_path/f'stream-{format_id}.{ext}.part';complete=tmp_path/f'stream-{format_id}.{ext}'
    partial.write_bytes(b'owned complete track')
    binding={'source_plan':{'required_working_bytes':BUDGET,'selected_formats':[
        {'format_id':format_id,'ext':ext,'filesize':partial.stat().st_size}]}}
    def entries(self,pattern):
        assert self==tmp_path;partial.replace(complete);yield partial;yield complete
    monkeypatch.setattr(Path,'rglob',entries)
    assert needed_disk(tmp_path,binding)==BUDGET-complete.stat().st_size
    assert complete.read_bytes()==b'owned complete track'


@pytest.mark.parametrize('change',['wrong_size','wrong_format','missing_master'])
def test_missing_media_needs_its_exact_bound_complete_target(tmp_path,monkeypatch,change):
    name='original.mp4' if change=='missing_master' else 'stream-299.mp4.part'
    partial=tmp_path/name;partial.write_bytes(b'owned');target=tmp_path/'stream-299.mp4'
    binding={'source_plan':{'required_working_bytes':BUDGET,'selected_formats':[
        {'format_id':'298' if change=='wrong_format' else '299','ext':'mp4','filesize':6 if change=='wrong_size' else 5}]}}
    during_scan(monkeypatch,tmp_path,lambda entry:entry.replace(target) if entry==partial else None)
    with pytest.raises(HubError,match='YOUTUBE_WHOLE_WORKSPACE_INVALID'):needed_disk(tmp_path,binding)


@pytest.mark.parametrize('name',['selection','download-result','extractor-observation-12345678-1234-4234-8234-123456789abc'])
def test_other_actual_child_metadata_writer_renames_are_bounded(tmp_path,monkeypatch,name):
    partial=tmp_path/(name+'.partial.json');target=tmp_path/(name+'.json');partial.write_bytes(b'{"owned":true}')
    during_scan(monkeypatch,tmp_path,lambda entry:entry.replace(target) if entry==partial else None)
    assert needed_disk(tmp_path,BINDING)==BUDGET
    assert target.read_bytes()==b'{"owned":true}'


@pytest.mark.parametrize('change',['invalid_observation_uuid','oversized_destination','missing_destination'])
def test_missing_metadata_only_accepts_actual_writer_and_bounded_regular_destination(tmp_path,monkeypatch,change):
    name='extractor-observation-not-a-uuid' if change=='invalid_observation_uuid' else 'selection'
    partial=tmp_path/(name+'.partial.json');target=tmp_path/(name+'.json');partial.write_bytes(b'owned')
    if change=='oversized_destination':
        with partial.open('wb') as stream:stream.truncate(262145)
    def move(entry):
        if entry==partial:
            entry.unlink() if change=='missing_destination' else entry.replace(target)
    during_scan(monkeypatch,tmp_path,move)
    with pytest.raises(HubError,match='YOUTUBE_WHOLE_WORKSPACE_INVALID'):needed_disk(tmp_path,BINDING)
