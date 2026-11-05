from pathlib import Path
import re
import tomllib
from adapters.assets import download
from adapters.video import VideoProcessor
from adapters.youtube import YouTubeOfficial
from .db import encode
from .errors import HubError
from .locks import resource_guard
from .maintenance import Maintenance
from .youtube_monitor import YouTubeMonitor
from .bilibili_service import BilibiliService
from .media_processing import MediaProcessing
from .evidence import sha256_file


class VideoPipeline:
    def __init__(self, settings, store, http):
        self.settings, self.store = settings, store
        self.youtube = YouTubeOfficial(http)
        self.processor = VideoProcessor(settings.root)
        self.maintenance = Maintenance(settings, store)
        self.monitor = YouTubeMonitor(settings,store,http)
        self.bilibili = BilibiliService(settings,store,http)
        self.processing = MediaProcessing(settings,store)

    def discovery(self, job):
        count = 0
        folder = self.settings.root / 'media' / job['job_id']
        folder.mkdir(parents=True, exist_ok=True)
        for source in self.settings.config['sources']:
            result = self.youtube.replays(source['youtube_handle'], source['youtube_channel_id'], job['job_id'])
            (folder / (source['x_handle'] + '-youtube.json')).write_text(encode(result), 'utf-8')
            for video in result['replays']:
                self.monitor.observe_video(video['raw'],source['youtube_channel_id'])
                count += 1
            if not result['catchup_complete']:
                self.store.event('review.required', {'job_id':job['job_id'], 'code':'YOUTUBE_SCAN_BOUND_REACHED'},
                                 dedup_key='youtube-bound:' + source['youtube_channel_id'])
        return {'completed_replays_seen':count, 'max_recent_uploads_per_channel':250}

    def rights(self, video_id):
        file = self.settings.root / 'config/rights.toml'
        if not file.is_file():
            return None
        try:
            grants = tomllib.loads(file.read_text('utf-8'))['grants']
            matches=[grant for grant in grants if grant.get('video_id') == video_id]
            if len(matches)>1: raise ValueError()
            return matches[0] if matches else None
        except (ValueError,KeyError,AttributeError,TypeError):
            raise HubError('MEDIA_RIGHTS_CONFIGURATION_INVALID',review=True) from None

    def process(self, job):
        video = job['payload']
        folder = self.settings.root / 'media' / job['job_id']
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'metadata.json').write_text(encode(video), 'utf-8')
        grant = self.rights(video['video_id'])
        if not grant or not {'download','process'} <= set(grant.get('actions', [])):
            if job['result'].get('source_asset'):
                raise HubError('MEDIA_RIGHTS_REVOKED',review=True)
            return {'disposition':'metadata_only', 'url':video['url'], 'rights_authorized':False,
                    'download_performed':False,'publish_performed':False}
        if (not grant.get('evidence') or not grant.get('authorized_by')
                or not re.fullmatch('[0-9a-f]{64}', grant.get('source_sha256', ''))):
            raise HubError('MEDIA_RIGHTS_OR_INTEGRITY_EVIDENCE_MISSING', review=True)
        prior=job['result'].get('source_asset')
        if prior and (prior.get('sha256')!=grant['source_sha256'] or job['result'].get('rights_evidence')!=grant['evidence']):
            raise HubError('MEDIA_RIGHTS_OR_SOURCE_CHANGED',review=True)
        if job['stage']=='video_ready_for_review' and job['result'].get('processed'):
            return self.bilibili.publish_reviewed_video(job,grant)
        if job['stage']=='video_batch_ready_for_review' and job['result'].get('processed_parts'):
            raise HubError('VIDEO_BATCH_REVIEW_AND_TRANSPORT_REQUIRED',review=True)
        maximum = grant.get('max_bytes', 0)
        if type(maximum) is not int or not 0 < maximum <= self.settings.config['max_media_bytes']:
            raise HubError('MEDIA_DOWNLOAD_SIZE_BOUND_REQUIRED', review=True)
        with resource_guard(self.settings.root / 'data/locks/heavy'):
            info=job['result'].get('source_asset')
            if info:
                source=Path(info['file'])
                if (source.is_symlink() or not source.resolve().is_relative_to(folder.resolve()) or
                    info.get('sha256')!=grant['source_sha256'] or job['result'].get('rights_evidence')!=grant['evidence']):
                    raise HubError('MEDIA_RIGHTS_OR_SOURCE_CHANGED',review=True)
                if source.exists() and (not source.is_file() or source.stat().st_size>maximum or sha256_file(source)!=grant['source_sha256']):
                    raise HubError('MEDIA_SOURCE_INTEGRITY_CHANGED',review=True)
                if not source.exists(): info=None
            if not info:
                self.maintenance.check_disk(maximum * 2 + 64_000_000)
                info = download(grant['source_url'], folder / 'original.mp4',
                                allowed_hosts=self.settings.config['authorized_download_hosts'],
                                max_bytes=maximum, check_disk=self.maintenance.check_disk,
                                expected_sha256=grant['source_sha256'])
                self.store.checkpoint(job, 'authorized_media_acquired', {**job['result'],'source_asset':info,
                    'source_sha256':info['sha256'],'rights_evidence':grant['evidence']})
            prepared=self.processing.run(job,info)
        result={**job['result'],'source_asset':info,'source_sha256':info['sha256'],
                'media_manifest':prepared['manifest'],'packet_coverage_verified':prepared['packet_coverage_verified'],
                'rights_evidence':grant['evidence'],'publish_permission':'publish' in grant['actions']}
        if len(prepared['outputs'])>1:
            result['processed_parts']=prepared['outputs']
            self.store.checkpoint(job,'video_batch_ready_for_review',result)
            raise HubError('VIDEO_BATCH_REVIEW_AND_TRANSPORT_REQUIRED',review=True)
        result['processed']=prepared['outputs'][0]
        self.store.checkpoint(job, 'video_ready_for_review',result)
        return self.bilibili.publish_reviewed_video(job,grant)
