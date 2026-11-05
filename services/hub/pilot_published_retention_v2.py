"""Exact completed Highest pilot only; reuse the existing three-file core.

This adapter never grants publication, invents formal launch, deletes a remote
post, refreshes a timestamp, or claims CloudBase contains the original movie.
"""
import hashlib,json
from pathlib import Path

from .video_source_retention_v2 import VideoSourceTracksRetentionV2,PUBLISHED,fail
from .archive_source_retention_v2 import _root_protected

JOB='594aed06-3aa1-45f4-90f0-1db49a602395'
DOWNLOAD='d28685f1-03fc-4a12-9d85-5dbf14107f82'
SOURCE='9T5tn1WGlJg'
BV='BV1JExh6QELr'
ATTEMPT='150c1074-4aed-4f5d-b87a-83cf2840af24'
PARENT='88d9f5e39ad4d655e0943b49882ec9e4771e31354c7e4221f9bbb4805d97c110'

# Exact filenames from the independently qualified NAV5 current45 manifest.
# The new protected attestation binds their actual hashes; old38/74 cannot.
RUNTIME_SOURCES=(
 'adapters/bilibili_video_guard.mjs','adapters/browser_replay_video.mjs','adapters/media_tools.py',
 'adapters/recovered_submit_gate.mjs','adapters/replay_highest_contract_v2.mjs','adapters/replay_native_driver.mjs',
 'adapters/safe_https.py','adapters/short_video_metadata_chain.mjs','adapters/short_video_saved_draft_navigation_control.mjs',
 'adapters/youtube.py','adapters/youtube_feed.py','adapters/ytdlp_highest_v2.py','adapters/ytdlp_replay.py',
 'scheduler/service.py','scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs',
 'services/hub/budget.py','services/hub/config.py','services/hub/cover_normalizer_v4.py','services/hub/db.py',
 'services/hub/errors.py','services/hub/evidence.py','services/hub/locks.py','services/hub/maintenance.py',
 'services/hub/media_processing.py','services/hub/outcomes.py','services/hub/recovered_submit_ticket.py',
 'services/hub/replay_highest_coverage_v2.py','services/hub/replay_highest_limits_v2.py',
 'services/hub/replay_highest_moderation_v2b.py','services/hub/replay_highest_video_v2.py',
 'services/hub/replay_highest_worker_runtime_v2.py','services/hub/replay_machine_checks.py',
 'services/hub/replay_moderation_common.py','services/hub/replay_packet_cache_v1.py','services/hub/replay_video_browser.py',
 'services/hub/replay_video_cover.py','services/hub/replay_video_web.py','services/hub/replay_worker_runtime.py',
 'services/hub/short_video_web.py','services/hub/worker.py','services/hub/youtube_download.py',
 'services/hub/youtube_download_v2.py','services/hub/youtube_highest_plan_v2.py','services/hub/youtube_monitor.py',
 'vendor/subtitle-cloud-worker/observer/store.py')


class PilotPublishedRetentionV2(VideoSourceTracksRetentionV2):
    RELEASE_FILENAME='video-source-retention-highest-pilot-current45-v3-release.json'
    TEST_VERSION='video-source-retention-highest-pilot-current45-v3-ubuntu-tests'
    TEST_COUNT=43
    TEST_GROUPS={'tests.test_video_source_retention_v2':19,'tests.test_video_source_retention_tracks_v2':12,
        'tests.test_video_retention_scheduler_v2':6,'tests.test_pilot_published_retention_v2':6}
    RELEASE_EXTRA_SOURCES=tuple(dict.fromkeys((*VideoSourceTracksRetentionV2.RELEASE_EXTRA_SOURCES,
        *RUNTIME_SOURCES,'services/hub/pilot_published_retention_v2.py')))

    @staticmethod
    def _target_context(context):
        job,binding,state,source,publication=context
        if (job['job_id']!=JOB or job['job_type']!='replay_highest_video_publish_v2' or job['source_id']!=SOURCE or
                source['job_id']!=DOWNLOAD or source['job_type']!='youtube_download_v2' or source['source_id']!=SOURCE or
                binding['download_job_id']!=DOWNLOAD or binding['source_id']!=SOURCE or
                state['attempt_id']!=ATTEMPT or job['attempts']!=3 or
                publication['external_id']!=BV or job['external_publish_id']!=BV or
                set(k for k in job['result'] if k.startswith('replay_') and k.endswith('_web_v2'))!={'replay_highest_video_web_v2'}):
            fail('VIDEO_RETENTION_HIGHEST_PILOT_SCOPE_REQUIRED')
        return context

    def _context(self,ident,connection=None):
        if ident!=JOB:fail('VIDEO_RETENTION_HIGHEST_PILOT_SCOPE_REQUIRED')
        return self._target_context(super()._context(ident,connection))

    def authorize(self,principal,**kwargs):
        if kwargs.get('scope')!=PUBLISHED or kwargs.get('job_id') is not None:
            fail('VIDEO_RETENTION_HIGHEST_PILOT_SCOPE_REQUIRED')
        return super().authorize(principal,**kwargs)

    def _release(self,plan):
        if plan.get('job_id')!=JOB or plan.get('scope')!=PUBLISHED:fail('VIDEO_RETENTION_HIGHEST_PILOT_SCOPE_REQUIRED')
        checksum=super()._release(plan)
        try:
            file=self.root/'config'/self.RELEASE_FILENAME;_root_protected(file)
            raw=file.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=checksum:raise ValueError()
            record=json.loads(raw);ref=record['test_attestation'];evidence=Path(ref['file']);_root_protected(evidence)
            data=evidence.read_bytes()
            if hashlib.sha256(data).hexdigest()!=ref['sha256']:raise ValueError()
            value=json.loads(data);source_root=Path(__file__).resolve().parents[2]
            actual={n:hashlib.sha256((source_root/n).read_bytes()).hexdigest() for n in RUNTIME_SOURCES}
            if (value.get('current_runtime_parent_archive_sha256')!=PARENT or value.get('current_runtime_source_sha256')!=actual or
                    value.get('qualification_scope')!='highest_pilot_current45_only_natural_not_qualified'):raise ValueError()
            return checksum
        except (OSError,KeyError,ValueError,TypeError):fail('VIDEO_RETENTION_RELEASE_REQUIRED')
