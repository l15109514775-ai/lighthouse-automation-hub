"""Queue-owned handler construction; importing this module starts no services.

Legacy callers can request the union explicitly through build_handlers(None).
Normal workers must name their queue before constructing any provider service.
"""


def _http(settings, store):
    from adapters.http import ProviderHTTP
    from .budget import Budget
    return ProviderHTTP(Budget(store, settings.config['budgets']))


def _tweet(settings, store):
    from .pipelines import TweetPipeline
    from .web_queue import ReviewedWebQueue
    from .machine_routes import MachineRoutes
    from .x_official_capture_queue_v1 import OfficialCaptureQueue, JOB_TYPE as CAPTURE
    from .x_official_reparse_v1 import OfficialSourceReparse, JOB_TYPE as REPARSE
    from .tweet_repost_check_v1 import TweetRepostCheck, JOB_TYPE as REPOST_CHECK
    tweet = TweetPipeline(settings, store, _http(settings, store))
    web = ReviewedWebQueue(settings, store)
    machine = MachineRoutes(settings, store)
    return {
        REPOST_CHECK: TweetRepostCheck(settings, store).process,
        REPARSE: OfficialSourceReparse(settings, store).process,
        CAPTURE: OfficialCaptureQueue(settings, store).process,
        'x_discovery': tweet.discovery, 'tweet_process': tweet.process,
        'tweet_web_publish': web.dispatch, 'tweet_web_reconcile': web.reconcile,
        'tweet_machine_publish': machine.dispatch,
        'tweet_machine_reconcile': machine.reconcile,
    }


def _video(settings, store):
    from .video_pipeline import VideoPipeline
    from .youtube_download import YouTubeDownload
    video = VideoPipeline(settings, store, _http(settings, store))
    return {
        'youtube_discovery': video.discovery, 'video_process': video.process,
        'youtube_download': YouTubeDownload(settings, store).process,
    }


def _system(settings, store):
    from .maintenance import Maintenance
    from .youtube_monitor import YouTubeMonitor
    from .bilibili_service import BilibiliService
    from .bilibili_tokens import BilibiliTokens
    http = _http(settings, store)
    maintenance = Maintenance(settings, store)
    monitor = YouTubeMonitor(settings, store, http)
    return {
        'maintenance': maintenance.monitor, 'backup': maintenance.backup_job,
        'cleanup': maintenance.cleanup, 'youtube_feed': monitor.feeds,
        'youtube_live_monitor': monitor.live,
        'bilibili_reconcile': BilibiliService(settings, store, http).reconcile,
        'bilibili_token_check': BilibiliTokens(settings, store, http).tick,
    }


def build_handlers(settings, store, queue):
    builders = {'tweet': _tweet, 'video': _video, 'system': _system}
    if queue is None:
        handlers = {}
        for build in builders.values():
            owned = build(settings, store)
            if handlers.keys() & owned.keys():
                raise ValueError('WORKER_HANDLER_OWNERSHIP_CONFLICT')
            handlers.update(owned)
        return handlers
    if not isinstance(queue, str) or queue not in builders:
        raise ValueError('WORKER_QUEUE_INVALID')
    return builders[queue](settings, store)
