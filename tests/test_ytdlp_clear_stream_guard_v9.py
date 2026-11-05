"""Pinned extractor plus actual highest child/selection, owned inputs, no network.

License availability is not proof of encrypted selected streams. Clear flags
come from pinned yt-dlp's real per-stream parser, never production patching.
"""
from copy import deepcopy
from pathlib import Path
import json, socket

import pytest
from adapters import ytdlp_highest_v2 as hq
from adapters import ytdlp_replay as legacy
from services.hub.errors import HubError
from test_youtube_highest_v2 import highest, info, selector_binding
from test_ytdlp_highest_transport_v2 import transport


def player_response(binding):
    return {'videoDetails': {'videoId': binding.get('video_id', hq.VIDEO_ID)},
            'playabilityStatus': {'status': 'OK'},
            'streamingData': {'licenseInfos': [{'drmFamily': 'OWNED_DRM_CAPABILITY', 'licenseServerUrl': 'https://owned.invalid/license'}]}}


@pytest.mark.parametrize('status', ['LOGIN_REQUIRED', 'AGE_VERIFICATION_REQUIRED', 'CONTENT_CHECK_REQUIRED', 'AGE_CHECK_REQUIRED'])
def test_actual_access_statuses_remain_blocked_even_with_clear_capability(status):
    body = player_response({}); body['playabilityStatus']['status'] = status
    for guard in (legacy.player_access_guard, hq.clear_stream_player_guard):
        with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'): guard(body)


@pytest.mark.parametrize('reason', ['Please SIGN IN', 'Confirm your age', 'not a bot', 'CAPTCHA required',
    'not available in your country', 'not available in your region', 'This video is DRM protected'])
def test_original_reason_protections_remain_blocked_with_status_ok(reason):
    body = player_response({}); body['playabilityStatus']['reason'] = reason
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'): hq.clear_stream_player_guard(body)


def test_license_infos_only_is_deferred_without_mutation_and_legacy_guard_is_unchanged():
    body = player_response({}); before = deepcopy(body)
    assert hq.clear_stream_player_guard(body) is None and body == before
    with pytest.raises(HubError, match='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED'): legacy.player_access_guard(body)


@pytest.mark.parametrize('track', [0, 1])
@pytest.mark.parametrize('flag', [True, None, 'maybe', 0, '', 'false', [], {}])
def test_selected_formats_require_explicit_clear_boolean_not_absent_or_falsy(track, flag):
    binding = selector_binding(); value = info(binding); value['formats'][track]['has_drm'] = flag
    with pytest.raises(HubError, match='YOUTUBE_HIGHEST_CLEAR_FORMAT_REQUIRED'): hq.select_highest(value, binding)


def test_missing_clear_flag_and_no_clear_options_cannot_reach_media():
    binding = selector_binding(); value = info(binding)
    value['formats'][0].pop('has_drm')
    with pytest.raises(HubError, match='YOUTUBE_HIGHEST_CLEAR_FORMAT_REQUIRED'): hq.select_highest(value, binding)
    value['formats'] = []
    with pytest.raises(HubError, match='YOUTUBE_HIGHEST_EXACT_FORMAT_REQUIRED'): hq.select_highest(value, binding)


@pytest.mark.parametrize('track', [0, 1])
def test_clear_flag_does_not_relax_original_exact_format_or_filesize_binding(track):
    binding = selector_binding(); value = info(binding); value['formats'][track]['filesize'] += 1
    with pytest.raises(HubError, match='YOUTUBE_HIGHEST_FORMAT_SIZE_OR_TRANSPORT_CHANGED'): hq.select_highest(value, binding)
    value = info(binding); value['formats'][track]['format_id'] = '18'
    with pytest.raises(HubError, match='YOUTUBE_HIGHEST_EXACT_FORMAT_REQUIRED'): hq.select_highest(value, binding)


def pinned_formats(binding, *, encrypted_itag=None):
    """Run the installed, pinned, actual YoutubeIE raw format parser offline."""
    import yt_dlp
    from yt_dlp.extractor.youtube import _video as video
    from yt_dlp.utils import determine_protocol
    from yt_dlp.version import __version__
    assert __version__ == legacy.YTDLP_VERSION == '2026.10.26'
    streams = []
    for expected in binding['source_plan']['selected_formats']:
        is_video = expected['format_id'] == '299'
        raw = {'itag': int(expected['format_id']), 'contentLength': str(expected['filesize']),
               'url': 'https://rr1---owned.googlevideo.com/videoplayback?owned=1',
               'mimeType': 'video/mp4; codecs="avc1.64002a"' if is_video else 'audio/mp4; codecs="mp4a.40.2"',
               'quality': 'hd1080' if is_video else 'tiny', 'approxDurationMs': str(int(binding['duration'] * 1000))}
        if is_video: raw.update(width=1920, height=1080, fps=60)
        if expected['format_id'] == encrypted_itag: raw['drmFamilies'] = ['OWNED_DRM']
        streams.append(raw)
    sd = {'licenseInfos': [{'drmFamily': 'OWNED_DRM_CAPABILITY'}], 'adaptiveFormats': streams,
          video.STREAMING_DATA_CLIENT_NAME: 'tv_samsung',
          video.STREAMING_DATA_FETCH_GVS_PO_TOKEN: lambda **kwargs: None,
          video.STREAMING_DATA_IS_PREMIUM_SUBSCRIBER: False,
          video.STREAMING_DATA_PLAYER_TOKEN_PROVIDED: False,
          video.STREAMING_DATA_AVAILABLE_AT_TIMESTAMP: None}
    with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'extractor_args': {'youtube': {'skip': ['hls', 'dash']}}}) as ydl:
        extractor = video.YoutubeIE(ydl)
        values = list(extractor._extract_formats_and_subtitles(hq.VIDEO_ID, [{'streamingData': sd}], None, 'was_live', binding['duration']))
    formats = [v for v in values if 'format_id' in v]
    # YoutubeDL's ordinary process_video_result supplies protocol after the
    # extractor yields each format. Use that same pinned utility; leave the
    # parser's actual boolean has_drm and all source identity fields intact.
    for fmt in formats: fmt['protocol'] = determine_protocol(fmt)
    return formats


@pytest.mark.parametrize('encrypted_itag', [None, '299', '140'])
def test_real_pinned_parser_uses_per_format_drm_families_despite_license_infos(monkeypatch, encrypted_itag):
    def no_network(*args, **kwargs): raise AssertionError('Owned format parser must not open sockets')
    monkeypatch.setattr(socket, 'create_connection', no_network)
    binding = selector_binding(); formats = pinned_formats(binding, encrypted_itag=encrypted_itag)
    assert [f['format_id'] for f in formats] == ['299', '140']
    assert {f['format_id']: f['has_drm'] for f in formats} == {f: f == encrypted_itag for f in ('299', '140')}
    value = info(binding); value['formats'] = formats
    if encrypted_itag:
        with pytest.raises(HubError, match='YOUTUBE_HIGHEST_CLEAR_FORMAT_REQUIRED'): hq.select_highest(value, binding)
    else: assert [f['format_id'] for f in hq.select_highest(value, binding)] == ['299', '140']


@pytest.mark.parametrize('phase', ['client', 'initial'])
def test_actual_child_hooks_allow_clear_selection_without_changing_clients_or_retry_options(transport, monkeypatch, phase):
    from yt_dlp.extractor.youtube._video import YoutubeIE
    f = transport; defaults = YoutubeIE._DEFAULT_AUTHED_CLIENTS
    body = player_response(f.binding); before = deepcopy(body)
    monkeypatch.setattr(YoutubeIE, '_extract_player_response', lambda *args, **kwargs: body)
    def extract(ydl, url, **kwargs):
        assert kwargs['download'] is False
        assert tuple(ydl.params['extractor_args']['youtube']['player_client']) == defaults
        assert ydl.params['retries'] == ydl.params['extractor_retries'] == ydl.params['fragment_retries'] == 0
        assert not ydl.params.get('allow_unplayable_formats') and ydl.params['geo_bypass'] is False
        ie = ydl._ies['Youtube']
        if phase == 'initial':
            monkeypatch.setattr(ie, '_search_json', lambda *args, **kwargs: body)
            # Named parent extraction seam: exercise actual initial guard, then
            # return without the pinned base method's network-only extraction.
            monkeypatch.setattr(YoutubeIE, '_extract_player_responses', lambda *args, **kwargs: ([body], None))
            assert ie._extract_player_responses(list(defaults), f.binding['video_id'], 'owned webpage', 'web', {}, False)[0] == [body]
        else: assert ie._extract_player_response('tv_samsung', f.binding['video_id']) is body
        value = info(f.binding); value['formats'] = pinned_formats(f.binding)
        return value
    monkeypatch.setattr(f.ytdlp.YoutubeDL, 'extract_info', extract)
    part = f.folder / 'stream-299.mp4.part'; part.write_bytes(b'Owned original partial remains byte-identical')
    assert hq.child_download(f.ticket) == 1 and f.result()['error_code'] == 'OWNED_MEDIA_STAGE_NOT_EXECUTED'
    assert body == before and YoutubeIE._DEFAULT_AUTHED_CLIENTS == defaults
    assert part.read_bytes() == b'Owned original partial remains byte-identical'
    assert f.media_calls == ['owned-stop'] and not f.calls and not f.sockets
    assert not f.db.query('SELECT * FROM api_calls') and not f.db.query('SELECT * FROM publications')
    assert 'access_diagnostic' not in f.result()


@pytest.mark.parametrize('flag', [True, None, 'missing'])
def test_actual_child_stops_before_media_and_retains_partial_for_drm_or_unknown(transport, monkeypatch, flag):
    f = transport
    def extract(ydl, url, **kwargs):
        value = info(f.binding)
        if flag == 'missing': value['formats'][0].pop('has_drm')
        else: value['formats'][0]['has_drm'] = flag
        return value
    monkeypatch.setattr(f.ytdlp.YoutubeDL, 'extract_info', extract)
    part = f.folder / 'stream-299.mp4.part'; part.write_bytes(b'Owned retained partial')
    assert hq.child_download(f.ticket) == 1 and f.result()['error_code'] == 'YOUTUBE_HIGHEST_CLEAR_FORMAT_REQUIRED'
    assert not f.media_calls and not f.calls and not f.sockets and not f.db.query('SELECT * FROM api_calls')
    assert part.read_bytes() == b'Owned retained partial'
    assert not (f.folder / 'original.mp4').exists() and not (f.folder / 'verified.json').exists()


@pytest.mark.parametrize('flag', [False, True, None, 'missing'])
def test_common_child_gate_checks_custom_whole_selector_before_media_start(transport, monkeypatch, flag):
    f = transport; called = []
    monkeypatch.setattr(f.ytdlp.YoutubeDL, 'extract_info', lambda *args, **kwargs: info(f.binding))
    def owned_selector(value, binding):
        # Named whole/natural-selector seam: these production routes supply a
        # callback instead of select_highest. Exercise the real common child
        # gate even when a callback supplies a non-clear or unknown stream.
        selected = value['formats']
        if flag == 'missing': selected[0].pop('has_drm')
        else: selected[0]['has_drm'] = flag
        return selected
    def before_media(*args): called.append('media-start')
    part = f.folder / 'stream-299.mp4.part'; part.write_bytes(b'Owned retained whole-source partial')
    assert hq.child_download(f.ticket, selector=owned_selector, media_start_guard=before_media) == 1
    if flag is False:
        assert f.result()['error_code'] == 'OWNED_MEDIA_STAGE_NOT_EXECUTED' and called == ['media-start']
        assert f.media_calls == ['owned-stop']
    else:
        assert f.result()['error_code'] == 'YOUTUBE_HIGHEST_CLEAR_FORMAT_REQUIRED' and not called and not f.media_calls
    assert part.read_bytes() == b'Owned retained whole-source partial'
    assert not f.calls and not f.sockets and not f.db.query('SELECT * FROM api_calls')


@pytest.mark.parametrize('value', [None, [], {}, [None], [{'has_drm': 'maybe'}]])
def test_common_clear_gate_rejects_empty_and_unknown_selected_routes(value):
    with pytest.raises(HubError, match='YOUTUBE_HIGHEST_CLEAR_FORMAT_REQUIRED'): hq.require_clear_selected_formats(value)
