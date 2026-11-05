import argparse
import json
import logging
import os
from .config import Settings
from .db import Store


def runtime(settings, store):
    from adapters.http import ProviderHTTP
    from .budget import Budget
    from .maintenance import Maintenance
    from .pipelines import TweetPipeline
    from .video_pipeline import VideoPipeline
    from .youtube_monitor import YouTubeMonitor
    from .youtube_download import YouTubeDownload
    from .bilibili_service import BilibiliService
    from .bilibili_tokens import BilibiliTokens
    http = ProviderHTTP(Budget(store, settings.config['budgets']))
    tweet, video, maintenance = TweetPipeline(settings, store, http), VideoPipeline(settings, store, http), Maintenance(settings, store)
    monitor = YouTubeMonitor(settings, store, http)
    bilibili = BilibiliService(settings,store,http)
    from .web_queue import ReviewedWebQueue
    web=ReviewedWebQueue(settings,store)
    from .machine_routes import MachineRoutes
    machine=MachineRoutes(settings,store)
    from .x_official_capture_queue_v1 import OfficialCaptureQueue,JOB_TYPE as X_MEDIA_INGEST_JOB
    from .x_official_reparse_v1 import OfficialSourceReparse,JOB_TYPE as X_CACHED_REPARSE_JOB
    from .tweet_repost_check_v1 import TweetRepostCheck,JOB_TYPE as TWEET_REPOST_CHECK_JOB
    return {TWEET_REPOST_CHECK_JOB:TweetRepostCheck(settings,store).process,
            X_CACHED_REPARSE_JOB:OfficialSourceReparse(settings,store).process,
            X_MEDIA_INGEST_JOB:OfficialCaptureQueue(settings,store).process,
            'x_discovery':tweet.discovery, 'tweet_process':tweet.process, 'youtube_discovery':video.discovery,
            'video_process':video.process, 'maintenance':maintenance.monitor, 'backup':maintenance.backup_job, 'cleanup':maintenance.cleanup,
            'youtube_feed':monitor.feeds,'youtube_live_monitor':monitor.live,'bilibili_reconcile':bilibili.reconcile,
            'youtube_download':YouTubeDownload(settings,store).process,
            'bilibili_token_check':BilibiliTokens(settings,store,http).tick,
            'tweet_web_publish':web.dispatch,'tweet_web_reconcile':web.reconcile,
            'tweet_machine_publish':machine.dispatch,'tweet_machine_reconcile':machine.reconcile}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['init','api','scheduler','worker','status','automation-readiness','bilibili-preflight',
        'mcp','import-tweet','export-review','review-readiness','prepare-video-review','submit-video-review','queue-reviewed-video',
        'bilibili-token-status','bilibili-token-import','tweet-review-readiness','prepare-tweet-review',
        'submit-tweet-review','verify-tweet-review','retranslate-tweet','queue-reviewed-web'])
    parser.add_argument('--queue', choices=['tweet','video','system'], default='tweet')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--input')
    parser.add_argument('--job-id')
    parser.add_argument('--request-id')
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    settings = Settings.load()
    settings.prepare()
    store = Store(settings.db_path)
    store.init()
    # Do not emit provider URLs, payloads, authorization values or exception strings.
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s')
    logging.getLogger('httpx').setLevel(logging.CRITICAL)
    logging.getLogger('httpcore').setLevel(logging.CRITICAL)
    if args.command == 'init':
        print(json.dumps({'database_initialized':True,'dry_run':settings.dry_run}))
    elif args.command == 'api':
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(settings), host='0.0.0.0', port=8080, access_log=False, log_level='warning')
    elif args.command == 'scheduler':
        from scheduler.service import DurableScheduler
        instance = DurableScheduler(settings, store)
        instance.tick() if args.once else instance.run()
    elif args.command == 'worker':
        from .worker import Worker
        worker = Worker(settings, store, runtime(settings, store))
        worker.once(args.queue) if args.once else worker.run(args.queue)
    elif args.command == 'status':
        from .control import Control
        print(json.dumps(Control(settings, store).status(), ensure_ascii=False))
    elif args.command == 'automation-readiness':
        from .readiness import automation_readiness
        print(json.dumps(automation_readiness(settings,store),ensure_ascii=False))
    elif args.command == 'bilibili-preflight':
        from adapters.http import ProviderHTTP
        from .budget import Budget
        from .bilibili_service import BilibiliService
        print(json.dumps(BilibiliService(settings,store,ProviderHTTP(Budget(store,settings.config['budgets']))).preflight(),ensure_ascii=False))
    elif args.command in ('bilibili-token-status','bilibili-token-import'):
        from .bilibili_tokens import BilibiliTokens
        from .control import Control
        from .errors import HubError
        import getpass
        import sys
        service=BilibiliTokens(settings,store)
        try:
            if args.command=='bilibili-token-status':
                result=service.status()
            else:
                if not args.input: parser.error('--input is required')
                if not sys.stdin.isatty(): raise HubError('INTERACTIVE_MAINTENANCE_AUTH_REQUIRED',review=True)
                principal=Control(settings,store).authorize(getpass.getpass('Maintenance token (hidden): '),write=True)
                result=service.import_file(args.input,principal,replace=args.replace)
            print(json.dumps(result,ensure_ascii=False))
        except HubError as exc:
            print(json.dumps({'error_code':exc.code,'published':False}))
            raise SystemExit(2) from None
    elif args.command=='queue-reviewed-web':
        from .web_queue import ReviewedWebQueue
        from .control import Control
        from .errors import HubError
        import getpass,sys
        try:
            if not args.job_id or not args.input:parser.error('--job-id and --input are required')
            if not sys.stdin.isatty():raise HubError('INTERACTIVE_MAINTENANCE_AUTH_REQUIRED',review=True)
            principal=Control(settings,store).authorize(getpass.getpass('Maintenance token (hidden): '),write=True)
            request=json.loads(open(args.input,encoding='utf-8').read())
            result=ReviewedWebQueue(settings,store).queue(principal,args.job_id,
                manifest_sha256=request['manifest_sha256'],target_site_uid=request['target_site_uid'],
                authorization_reference=request['authorization_reference'])
            print(json.dumps(result,ensure_ascii=False))
        except (HubError,KeyError,ValueError) as exc:
            print(json.dumps({'error_code':exc.code if isinstance(exc,HubError) else 'WEB_QUEUE_REQUEST_INVALID','published':False}))
            raise SystemExit(2) from None
    elif args.command=='retranslate-tweet':
        from .translation_revision import retranslate_failed
        from .control import Control
        from .errors import HubError
        import getpass,sys
        try:
            if not args.job_id:parser.error('--job-id is required')
            if not sys.stdin.isatty():raise HubError('INTERACTIVE_MAINTENANCE_AUTH_REQUIRED',review=True)
            principal=Control(settings,store).authorize(getpass.getpass('Maintenance token (hidden): '),write=True)
            print(json.dumps(retranslate_failed(settings,store,args.job_id,principal),ensure_ascii=False))
        except HubError as exc:
            print(json.dumps({'error_code':exc.code,'published':False}));raise SystemExit(2) from None
    elif args.command in ('tweet-review-readiness','prepare-tweet-review','submit-tweet-review','verify-tweet-review'):
        from .tweet_reviews import TweetReviews
        from .errors import HubError
        service=TweetReviews(settings,store)
        try:
            if args.command=='tweet-review-readiness':result=service.readiness()
            elif args.command=='prepare-tweet-review':
                if not args.job_id:parser.error('--job-id is required')
                result=service.prepare(args.job_id,args.input)
            elif args.command=='submit-tweet-review':
                if not args.input:parser.error('--input is required')
                result=service.submit_file(args.input)
            else:
                if not args.job_id:parser.error('--job-id is required')
                result={'request_id':service.verify_approval(store.get(args.job_id)),'published':False}
            print(json.dumps(result,ensure_ascii=False))
        except HubError as exc:
            print(json.dumps({'error_code':exc.code,'published':False}))
            raise SystemExit(2) from None
    elif args.command in ('review-readiness','prepare-video-review','submit-video-review','queue-reviewed-video'):
        from .video_reviews import VideoReviews
        from .errors import HubError
        import getpass
        import sys
        service=VideoReviews(settings,store)
        try:
            if args.command=='review-readiness':
                result=service.readiness()
            elif args.command=='prepare-video-review':
                if not args.job_id: parser.error('--job-id is required')
                result=service.prepare(args.job_id,os.getenv('BILIBILI_ACCOUNT_ID',''))
            elif args.command=='submit-video-review':
                if not args.input: parser.error('--input is required')
                result=service.submit_file(args.input)
            else:
                if not args.request_id: parser.error('--request-id is required')
                if not sys.stdin.isatty(): raise HubError('INTERACTIVE_MAINTENANCE_AUTH_REQUIRED',review=True)
                result=service.queue(args.request_id,getpass.getpass('Maintenance token (hidden): '))
            print(json.dumps(result,ensure_ascii=False))
        except HubError as exc:
            print(json.dumps({'error_code':exc.code,'published':False}))
            raise SystemExit(2) from None
    elif args.command == 'mcp':
        from .mcp_server import main as serve
        serve()
    elif args.command == 'import-tweet':
        from .manual import import_tweet
        if not args.input:
            parser.error('--input is required')
        print(json.dumps({'job_id':import_tweet(settings,store,args.input),'source':'manual-input','published':False}))
    elif args.command == 'export-review':
        from .manual import export_review_bundle
        if not args.job_id:
            parser.error('--job-id is required')
        print(json.dumps(export_review_bundle(settings,store,args.job_id),ensure_ascii=False))


if __name__ == '__main__':
    main()
