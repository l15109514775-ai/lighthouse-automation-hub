"""Operational capability gates, separate from process /ready and from credentials."""
import os
import time
import json


def automation_readiness(settings, store):
    cfg = settings.config
    now = time.time()
    beats = {row['name']:row['at'] for row in store.query('SELECT * FROM heartbeats')}
    core_alive = all(now - beats.get(name,0) < 120 for name in
        ('scheduler','worker:tweet','worker:video','worker:system'))
    model_calls = store.query("SELECT MAX(finished_at) last_success FROM api_calls WHERE provider='model' AND state='done'")
    youtube_polls = {row['source_key']:row for row in store.query('SELECT * FROM source_polls')}
    channels = [source['youtube_channel_id'] for source in cfg['sources']]
    fresh_window = cfg.get('youtube_live_poll_minutes',5) * 120 + 60
    youtube_verified = all((poll := youtube_polls.get('youtube-live:'+channel)) and
        poll['last_success_at'] and not poll['error_code'] and now-poll['last_success_at'] < fresh_window for channel in channels)
    rights_claimed = cfg.get('owner_content_permission_statement') == 'owner_claimed_scope_unverified'
    rights_confirmed = cfg.get('owner_content_permission_statement') == 'owner_confirmed_scope'
    x_keys=['x-web:'+s['x_handle'] for s in cfg['sources']]
    x_polls=[youtube_polls[key] for key in x_keys if key in youtube_polls]
    x_web_fresh=bool(cfg.get('x_web_enabled') and len(x_polls)==len(x_keys) and all(
        p['last_success_at'] and not p['error_code'] and now-p['last_success_at']<cfg['tweet_poll_minutes']*120+60 for p in x_polls))
    api_keys=['x-api:'+s['x_handle'] for s in cfg['sources']]
    api_polls=[youtube_polls[key] for key in api_keys if key in youtube_polls]
    def api_metadata(row):
        try:
            value=json.loads(row['metadata'])
            return value if isinstance(value,dict) else {}
        except (ValueError,TypeError):return {}
    api_sources=[{'source_key':p['source_key'],'checked_at':p['checked_at'],'last_success_at':p['last_success_at'],
        'error_code':p['error_code'],'catchup_complete':api_metadata(p).get('catchup_complete') is True}
        for p in api_polls]
    x_api_fresh=bool(cfg.get('x_api_enabled') and not cfg.get('x_web_enabled') and len(api_polls)==len(api_keys) and all(
        p['last_success_at'] and not p['error_code'] and 0<=now-p['last_success_at']<cfg['tweet_poll_minutes']*120+60 and
        api_metadata(p).get('source_authority')=='x-v2-official' and api_metadata(p).get('state')=='observed' and
        api_metadata(p).get('catchup_complete') is True for p in api_polls))
    supervised_count=store.query("SELECT COUNT(*) n FROM publications WHERE platform='bilibili' AND kind='dynamic_website_supervised' AND state='completed'")[0]['n']
    machine_count=store.query("SELECT COUNT(*) n FROM publications WHERE platform='bilibili' AND kind IN ('dynamic_website_machine_v1','dynamic_website_machine_v2','dynamic_website_machine_v3_api','dynamic_website_machine_v4_api_relations') AND state='completed'")[0]['n']
    machine_last_completed=store.query("SELECT MAX(updated_at) at FROM publications WHERE platform='bilibili' AND kind IN ('dynamic_website_machine_v1','dynamic_website_machine_v2','dynamic_website_machine_v3_api','dynamic_website_machine_v4_api_relations') AND state='completed'")[0]['at']
    machine_auth_failures=store.query("SELECT COUNT(*) n FROM jobs WHERE source_platform='machine_web' AND state='auth_expired'")[0]['n']
    machine_enabled=bool(cfg.get('machine_tweet_enabled',False))
    machine_policy=None
    machine_error='MACHINE_TWEET_POLICY_DISABLED'
    from .machine_routes import machine_service,is_machine_policy
    # Preserve the legacy independent v1-policy inspection for callers whose
    # configured pipeline is still human-review-v1. Active machine versions,
    # including the API policy, always use their own service and publication kind.
    selected=cfg.get('policy_version') if is_machine_policy(cfg.get('policy_version')) else 'machine-tweet-v1'
    service=machine_service(settings,store,selected)
    if machine_enabled:
        from .errors import HubError
        try:
            machine_policy=service.policy()
            machine_error=None
        except HubError as exc:
            machine_error=exc.code
    machine_authorized=machine_policy is not None
    active_kind=service.kind
    active_machine_count=store.query("SELECT COUNT(*) n FROM publications WHERE platform='bilibili' AND kind=? AND state='completed'",(active_kind,))[0]['n']
    machine_accepted=machine_authorized and active_machine_count>0
    website_count=supervised_count+machine_count
    monitor_blockers=[]
    if cfg.get('x_api_enabled'):
        if not x_api_fresh:
            monitor_blockers.append({'code':'X_API_OBSERVATION_REQUIRED','component':'tweet_monitor',
                'detail':'Fresh complete official API polls for both configured sources are required. Browser observations and API credentials alone do not satisfy this gate.'})
    else:
        monitor_blockers.append({'code':'X_WEB_CATCHUP_COVERAGE_LIMITED' if x_web_fresh else 'X_WEB_OBSERVATION_REQUIRED' if cfg.get('x_web_enabled') else 'X_SOURCE_NOT_ENABLED',
            'component':'tweet_monitor','detail':'Ordinary rendered-page snapshots are bounded; failed reads are not empty timelines and complete history catch-up is not verified.'})
    blockers = monitor_blockers+[
        {'code':('MACHINE_CONTENT_SCOPE_LIMITED' if machine_accepted else 'MACHINE_PUBLISH_ACCEPTANCE_REQUIRED' if machine_authorized else machine_error),
         'component':'dynamic_publish','detail':('Machine mode is limited to its versioned observed content scope. Unsupported or unverifiable content stops with a specific error; original human confirmations remain false.' if machine_accepted else
             'Machine policy authorization, its configured switch and a real completed machine publication are distinct checks. Existing supervised publications do not prove unattended machine publishing.')},
        {'code':'BILIBILI_VIDEO_TRANSPORT_NOT_CONNECTED','component':'video_publish',
         'detail':'Official transport is implemented; no authorized official or permitted website transport has passed real acceptance.'},
        {'code':('CONTENT_SOURCE_AND_PERMISSION_BINDING_REQUIRED' if rights_confirmed else
                 'CONTENT_RIGHTS_SCOPE_UNVERIFIED' if rights_claimed else 'CONTENT_RIGHTS_NOT_GRANTED'),
         'component':'download_and_publish',
         'detail':('Owner confirmed target-account permission for tweets, full replays and clips; lawful material acquisition and per-job binding remain required.' if rights_confirmed else
                   'Owner supplied a permit identifier; its account and full-replay/tweet scope remain unverified.' if rights_claimed
                   else 'No applicable verified content grant is configured; public availability is not a grant.')},
    ]
    if not machine_authorized:
        blockers.append({'code':machine_error,'component':'translation_review',
            'detail':'Owner-authorized machine mode requires its separate recorded policy. No workflow may turn the original human confirmation fields true automatically.'})
    if not os.getenv('YOUTUBE_API_KEY'):
        blockers.append({'code':'YOUTUBE_CREDENTIAL_REQUIRED','component':'live_state',
                         'detail':'Public feed discovery does not verify whether a broadcast is live or ended.'})
    elif not youtube_verified:
        blockers.append({'code':'YOUTUBE_LIVE_READ_NOT_VERIFIED','component':'live_state',
                         'detail':'Fresh successful official API observations for both configured channels are required.'})
    if not cfg.get('first_publish_accepted') and not machine_accepted:
        blockers.append({'code':'FIRST_PUBLISH_ACCEPTANCE_REQUIRED','component':'publish',
                         'detail':'A real authorized sample and first publication acceptance are still required.'})
    return {'goal_status':'in_progress','fully_automatic_ready':False,'core_independent_of_chat':core_alive,
        'translation_provider':'configured_model_api','last_successful_model_call':model_calls[0]['last_success'],
        'youtube_live_verified':bool(youtube_verified),'dry_run':settings.dry_run,
        'publishing_enabled':machine_authorized,'target_account_name':cfg.get('target_bilibili_account_name'),
        'owner_permission_statement':cfg.get('owner_content_permission_statement','unverified'),
        'bilibili_video_implementation':True,'bilibili_dynamic_implementation':True,
        'website_publications_completed':website_count,'supervised_website_publications_completed':supervised_count,
        'machine_website_publications_completed':machine_count,'x_web_implementation':not bool(cfg.get('x_visual_retired',False)),
        'x_visual_retired':bool(cfg.get('x_visual_retired',False)),
        'active_machine_policy_publications_completed':active_machine_count,
        'active_machine_publication_kind':active_kind,
        'machine_transport_historical_publication_accepted':machine_count>0,
        'active_machine_policy':cfg.get('policy_version'),
        'machine_tweet_implemented':True,'machine_tweet_enabled':machine_enabled,
        'machine_tweet_policy_valid':machine_authorized,'machine_tweet_policy_error':machine_error,
        'machine_tweet_publish_accepted':machine_accepted,
        'machine_tweet_ready':bool(machine_accepted and core_alive and (x_api_fresh if selected in ('machine-tweet-v3-api','machine-tweet-v4-api-relations') else x_web_fresh) and not machine_auth_failures),
        'machine_tweet_last_completed_at':machine_last_completed,
        'machine_tweet_auth_expired_jobs':machine_auth_failures,
        'machine_tweet_content_scope':machine_policy['supported_content'] if machine_policy else [],
        'machine_tweet_max_post_age_seconds':machine_policy['max_post_age_seconds'] if machine_policy else None,
        'machine_tweet_requires_per_post_human_review':False,
        'machine_tweet_activation_at':machine_policy['activation_at'] if machine_policy else None,
        'reviewed_website_queue_implemented':True,'reviewed_artifact_dispatch_enabled':bool(cfg.get('web_reviewed_queue_enabled')),
        'x_web_enabled':bool(cfg.get('x_web_enabled')),'x_web_observations_fresh':x_web_fresh,
        'x_web_source_errors':{p['source_key']:p['error_code'] for p in x_polls if p['error_code']},
        'x_api_implementation':True,'x_api_enabled':bool(cfg.get('x_api_enabled')),
        'x_api_observations_fresh':x_api_fresh,'x_api_sources':api_sources,
        'x_api_source_errors':{p['source_key']:p['error_code'] for p in api_polls if p['error_code']},
        'x_source_mode':'x-v2-official' if cfg.get('x_api_enabled') else 'x-web-dom-v1' if cfg.get('x_web_enabled') else 'disabled',
        'bilibili_oauth_refresh_implemented':True,
        'youtube_download_implemented':True,
        'youtube_download_enabled':bool(cfg.get('youtube_download_enabled',False)),
        'youtube_download_verified_jobs':store.query("SELECT COUNT(*) n FROM jobs WHERE job_type='youtube_download' AND state='completed' AND json_extract(result,'$.youtube_download_v1.state')='verified'")[0]['n'],
        'video_segment_preparation_implemented':True,'multi_part_publish_implemented':False,'blockers':blockers}
