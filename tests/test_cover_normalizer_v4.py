"""Real, owned FFmpeg images only. No source download, model or public post."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

import pytest

from services.hub import cover_normalizer_v4 as cover
from services.hub.errors import HubError


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_jpeg(path, size='1280x720'):
    assert shutil.which('ffmpeg'), 'FFmpeg is required for these real image tests'
    graph = ('color=c=white:s='+size+',drawgrid=w=20:h=720:t=10:c=black,'
        'drawbox=x=0:y=0:w=160:h=160:color=red:t=fill,'
        'drawbox=x=1120:y=0:w=160:h=160:color=lime:t=fill,'
        'drawbox=x=0:y=560:w=160:h=160:color=blue:t=fill,'
        'drawbox=x=1120:y=560:w=160:h=160:color=yellow:t=fill')
    subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i', graph,
        '-frames:v', '1', '-threads', '1', '-c:v', 'mjpeg', '-q:v', '2', '-y', str(path)],
        check=True, capture_output=True, timeout=20)


@pytest.fixture
def owned(tmp_path):
    root = tmp_path/'hub';job = str(uuid.uuid4());attempt = str(uuid.uuid4())
    source = root/'media'/job/'short-video-attempts'/attempt/'cover-source-official-maxres.jpg'
    source.parent.mkdir(parents=True)
    make_jpeg(source)
    return {'root': root, 'job_id': job, 'source_video_id': 'abcdefghijk', 'source_file': source,
            'source_sha256': sha(source), 'source_url': 'https://i.ytimg.com/vi/abcdefghijk/maxresdefault.jpg'}


def normalize(args, **kwargs):
    return cover.normalize_cover(**args, **kwargs)


def rgb(path, *, foreground=False, source=False):
    filter_graph = 'format=rgb24'+(',crop=1280:720:0:120' if foreground else '')
    result = subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
        '-i', str(path), '-vf', filter_graph, '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
        check=True, capture_output=True, timeout=20)
    assert len(result.stdout) == 1280*(720 if foreground or source else 960)*3
    return result.stdout

def pixel(raw, x, y, width=1280):
    at = (y*width+x)*3
    return tuple(raw[at:at+3])


def assert_no_generated_pair(args):
    destination = args['root']/'media'/args['job_id']/'cover-assets-v4'
    assert not (destination/'top-bottom-blur-cover.png').exists()
    assert not (destination/'provenance.json').exists()
    assert not list(destination.glob('.*.partial.*'))


def test_real_ffmpeg_adds_only_top_bottom_blur_and_preserves_every_original_rgb_byte(owned):
    before = owned['source_file'].read_bytes()
    result = normalize(owned);output = Path(result['output_file'])
    assert owned['source_file'].read_bytes() == before and sha(owned['source_file']) == owned['source_sha256']
    record = json.loads(Path(result['provenance_file']).read_bytes())
    assert cover.png_dimensions(output.read_bytes()) == (1280, 960)
    assert record['version'] == 'lighthouse-derived-cover-v4' and record['policy_version'] == 'top-bottom-blur-v1'
    assert record['input']['width'] == 1280 and record['input']['height'] == 720
    assert record['output'] == {'file':str(output),'width':1280,'height':960,'mime_type':'image/png',
        'bytes':output.stat().st_size,'sha256':sha(output)}
    assert record['transform']['foreground_rect'] == {'x':0,'y':120,'width':1280,'height':720}
    assert record['transform']['safe_crop'] == {'x':0,'y':0,'width':1280,'height':960}
    assert record['transform']['background'] == {'width':1280,'height':960,'blur_sigma':35}
    assert record['transform']['foreground_fit'] == 'original_size' and record['transform']['foreground_padding'] == 'none'
    assert record['image_edited'] is record['no_text_regenerated'] is True and record['official_original_bytes'] is False
    assert record['ffmpeg_version'].startswith('ffmpeg version ')
    # Decode both real files independently; every RGB byte of all 921600 source
    # pixels must survive at its original size and x=0,y=120, with no new text.
    original = rgb(owned['source_file'], source=True)
    assert rgb(output, foreground=True) == original
    image = rgb(output)
    middle = image[120*1280*3:840*1280*3]
    assert middle == original
    top = [pixel(image,x,50)[0] for x in range(400,800)]
    bottom = [pixel(image,x,910)[0] for x in range(400,800)]
    foreground = [pixel(image,x,420)[0] for x in range(400,800)]
    assert max(top)-min(top)<30 and max(bottom)-min(bottom)<30
    assert min(top)>30 and min(bottom)>30 and max(foreground)-min(foreground)>150


def test_same_bytes_and_ffmpeg_version_produce_same_png_for_another_owned_job(owned):
    first = normalize(owned)
    second = dict(owned);second['job_id'] = str(uuid.uuid4())
    source = owned['root']/'media'/second['job_id']/'short-video-attempts'/str(uuid.uuid4())/'cover-source-official-maxres.jpg'
    source.parent.mkdir(parents=True);source.write_bytes(owned['source_file'].read_bytes());second['source_file'] = source
    other = normalize(second)
    assert first['output_sha256'] == other['output_sha256']
    assert first['provenance']['ffmpeg_version'] == other['provenance']['ffmpeg_version']


def test_verified_repeat_is_a_byte_preserving_noop_without_invoking_ffmpeg(owned, monkeypatch):
    first = normalize(owned); p = Path(first['provenance_file']);before = p.read_bytes();mtime = p.stat().st_mtime_ns
    def forbidden(*args, **kwargs):
        raise AssertionError('Verified cache must not execute any process')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    repeat = normalize(owned)
    assert repeat['reused'] is True and repeat['output_sha256'] == first['output_sha256']
    assert p.read_bytes() == before and p.stat().st_mtime_ns == mtime


def test_bad_source_hash_is_rejected_without_any_generated_output(owned):
    source = owned['source_file'].read_bytes();owned['source_sha256'] = '0'*64
    with pytest.raises(HubError, match='COVER_NORMALIZE_SOURCE_HASH_MISMATCH'):
        normalize(owned)
    assert owned['source_file'].read_bytes() == source
    assert_no_generated_pair(owned)


def test_non_image_source_is_rejected_without_modification(owned):
    owned['source_file'].write_bytes(b'Owned invalid image fixture')
    owned['source_sha256'] = sha(owned['source_file'])
    with pytest.raises(HubError, match='COVER_NORMALIZE_SOURCE_INVALID'):
        normalize(owned)
    assert_no_generated_pair(owned)


def test_real_wrong_sized_jpeg_is_rejected(owned):
    make_jpeg(owned['source_file'], '320x180');owned['source_sha256'] = sha(owned['source_file'])
    with pytest.raises(HubError, match='COVER_NORMALIZE_SOURCE_INVALID'):
        normalize(owned)
    assert_no_generated_pair(owned)


def test_missing_ffmpeg_reports_real_missing_dependency_and_keeps_source(owned):
    before = owned['source_file'].read_bytes()
    with pytest.raises(HubError, match='COVER_NORMALIZE_FFMPEG_MISSING'):
        normalize(owned, ffmpeg='owned-missing-ffmpeg-'+uuid.uuid4().hex)
    assert owned['source_file'].read_bytes() == before
    assert_no_generated_pair(owned)


def test_real_ffmpeg_decode_failure_cleans_only_new_partials_and_preserves_original(owned):
    # Plausible SOF with no image scan: header size passes, real decoder fails.
    broken = b'\xff\xd8\xff\xc0\x00\x11\x08\x02\xd0\x05\x00\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00\xff\xd9'
    assert cover.jpeg_dimensions(broken) == (1280, 720)
    owned['source_file'].write_bytes(broken);owned['source_sha256'] = sha(owned['source_file'])
    with pytest.raises(HubError, match='COVER_NORMALIZE_FFMPEG_FAILED'):
        normalize(owned)
    assert owned['source_file'].read_bytes() == broken
    assert_no_generated_pair(owned)


def test_publish_failure_after_real_render_cleans_its_own_outputs_and_keeps_source(owned, monkeypatch):
    before = owned['source_file'].read_bytes();real = cover.os.link;calls = []
    def fail_second_link(source, destination):
        calls.append(destination)
        if len(calls) == 2:
            raise OSError('Owned destination failure')
        return real(source, destination)
    monkeypatch.setattr(cover.os, 'link', fail_second_link)
    with pytest.raises(HubError, match='COVER_NORMALIZE_FFMPEG_FAILED'):
        normalize(owned)
    assert len(calls) == 2 and owned['source_file'].read_bytes() == before
    assert_no_generated_pair(owned)


def test_existing_partial_pair_is_never_overwritten_or_deleted(owned):
    destination = owned['root']/'media'/owned['job_id']/'cover-assets-v4'
    destination.mkdir(mode=0o700);output = destination/'top-bottom-blur-cover.png';output.write_bytes(b'Owned prior file')
    with pytest.raises(HubError, match='COVER_NORMALIZE_EXISTING_OUTPUT_CONFLICT'):
        normalize(owned)
    assert output.read_bytes() == b'Owned prior file'
    assert not (destination/'provenance.json').exists()


def test_tampered_cached_output_is_preserved_but_not_reused(owned):
    first = normalize(owned);output = Path(first['output_file']);output.write_bytes(output.read_bytes()+b'owned change')
    changed = output.read_bytes()
    with pytest.raises(HubError, match='COVER_NORMALIZE_EXISTING_OUTPUT_CONFLICT'):
        normalize(owned)
    assert output.read_bytes() == changed and sha(owned['source_file']) == owned['source_sha256']


def test_external_source_change_during_real_render_stops_and_cleans_new_outputs(owned, monkeypatch):
    real = subprocess.run;before = owned['source_file'].read_bytes()
    def mutate_after_render(command, *args, **kwargs):
        result = real(command, *args, **kwargs)
        if '-filter_complex' in command:
            owned['source_file'].write_bytes(before+b'Owned external concurrent change')
        return result
    monkeypatch.setattr(subprocess, 'run', mutate_after_render)
    with pytest.raises(HubError, match='COVER_NORMALIZE_SOURCE_CHANGED'):
        normalize(owned)
    assert owned['source_file'].read_bytes() == before+b'Owned external concurrent change'
    assert_no_generated_pair(owned)


def test_cached_provenance_cannot_claim_unedited_official_bytes(owned):
    first = normalize(owned);document = Path(first['provenance_file']);record = json.loads(document.read_bytes())
    record['official_original_bytes'] = True;document.write_text(json.dumps(record), encoding='utf-8')
    with pytest.raises(HubError, match='COVER_NORMALIZE_EXISTING_OUTPUT_CONFLICT'):
        normalize(owned)


@pytest.mark.parametrize('change', ['other_job', 'other_name', 'other_url'])
def test_input_scope_and_original_source_url_are_bound(owned, change):
    source = owned['source_file'];before = source.read_bytes()
    if change == 'other_job':
        owned['job_id'] = str(uuid.uuid4())
    elif change == 'other_name':
        other = source.with_name('other.jpg');other.write_bytes(before);owned['source_file'] = other
    else:
        owned['source_url'] = owned['source_url'].replace('maxresdefault', 'hqdefault')
    with pytest.raises(HubError):
        normalize(owned)
    assert source.read_bytes() == before
    assert_no_generated_pair(owned)


def test_runtime_rgb_verification_rejects_valid_png_with_changed_foreground(owned,monkeypatch):
    real=subprocess.run;before=owned['source_file'].read_bytes()
    def replace_generated_image(command,*args,**kwargs):
        result=real(command,*args,**kwargs)
        if '-filter_complex' in command:
            real(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=1280x960',
                '-frames:v','1','-threads','1','-pix_fmt','rgb24','-c:v','png','-y',command[-1]],check=True,capture_output=True,timeout=20)
        return result
    monkeypatch.setattr(subprocess,'run',replace_generated_image)
    with pytest.raises(HubError,match='COVER_NORMALIZE_RGB_VERIFICATION_FAILED'):normalize(owned)
    assert owned['source_file'].read_bytes()==before;assert_no_generated_pair(owned)


def test_publish_failure_never_unlinks_another_replacement_inode(owned,monkeypatch):
    real=cover.os.link;calls=[];replacement=b'Owned operator replacement must survive'
    def replace_before_second_link(source,destination):
        calls.append(destination)
        if len(calls)==2:
            calls[0].unlink();calls[0].write_bytes(replacement)
            raise OSError('Owned injected post-link failure')
        return real(source,destination)
    monkeypatch.setattr(cover.os,'link',replace_before_second_link)
    with pytest.raises(HubError,match='COVER_NORMALIZE_FFMPEG_FAILED'):normalize(owned)
    assert Path(calls[0]).read_bytes()==replacement and sha(owned['source_file'])==owned['source_sha256']
    assert not (Path(calls[0]).parent/'provenance.json').exists()
    assert not list(Path(calls[0]).parent.glob('.*.partial.*'))


def test_module_cli_runs_from_the_single_v4_file_without_network_or_model(owned):
    command=[sys.executable,'-m','services.hub.cover_normalizer_v4']
    for key,value in owned.items():command+=['--'+key.replace('_','-'),str(value)]
    before=owned['source_file'].read_bytes()
    result=subprocess.run(command,cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=90)
    assert result.returncode==0,result.stdout+result.stderr
    record=json.loads(result.stdout)
    assert record['reused'] is False and record['provenance']['version']=='lighthouse-derived-cover-v4'
    assert cover.png_dimensions(Path(record['output_file']).read_bytes())==(1280,960)
    assert owned['source_file'].read_bytes()==before
