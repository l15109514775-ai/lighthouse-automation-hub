"""Explicit natural-format cleanup contract; no new file or deletion scope.

Only the natural-v3 route accepts the exact producer IDENT contract. Highest-v2
keeps the original numeric restriction. The old38 release cannot enable this
class: its independent release pins this module, producer policy and 46 tests.
"""
from .video_source_retention_v2 import VideoSourceTracksRetentionV2
from .youtube_natural_plan_v3 import IDENT


class NaturalFormatVideoSourceRetentionV3(VideoSourceTracksRetentionV2):
    RELEASE_FILENAME='video-source-retention-natural-formats-v3-release.json'
    RELEASE_EXTRA_SOURCES=(*VideoSourceTracksRetentionV2.RELEASE_EXTRA_SOURCES,
        'services/hub/video_source_retention_natural_formats_v3.py','services/hub/youtube_natural_plan_v3.py')
    TEST_VERSION='video-source-retention-natural-formats-v3-ubuntu-tests-v1'
    TEST_COUNT=46
    TEST_GROUPS={**VideoSourceTracksRetentionV2.TEST_GROUPS,'tests.test_video_source_retention_natural_formats_v3':8}

    def _format_id_allowed(self,value,download_state):
        if download_state=='youtube_download_v3':
            return isinstance(value,str) and IDENT.fullmatch(value) is not None
        return super()._format_id_allowed(value,download_state)
