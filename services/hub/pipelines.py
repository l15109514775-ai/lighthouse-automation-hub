from pathlib import Path
import json
from adapters.assets import download
from adapters.compositor import Compositor, compositor_input
from adapters.model import ModelAPI
from adapters.x import XOfficial
from .db import encode
from .errors import HubError
from .evidence import machine_evidence, sha256_file
from .locks import exclusive
from .maintenance import Maintenance
from .source_receipts import SourceReceipts


class TweetPipeline:
    def __init__(self, settings, store, http):
        self.settings, self.store = settings, store
        self.http = http
        self.x = XOfficial(http)
        self.model = ModelAPI(http, store,settings=settings)
        self.model_receipts=self.model.receipts
        self.compositor = Compositor(settings.root)
        self.maintenance = Maintenance(settings, store)
        self.source_receipts=SourceReceipts(settings,store)

    def discovery(self, job):
        if self.settings.config.get('x_visual_retired') and self.settings.config.get('x_web_enabled'):
            raise HubError('X_VISUAL_TRANSPORT_RETIRED')
        if self.settings.config.get('x_web_enabled', False):
            from .x_web_discovery import XWebDiscovery
            return XWebDiscovery(self.settings,self.store).run(job)
        if not self.settings.config.get('x_api_enabled', False):
            return {'disposition':'x_api_disabled_by_owner', 'paid_calls':0, 'ingestion':'manual'}
        if self.settings.config.get('policy_version') in ('machine-tweet-v3-api','machine-tweet-v4-api-relations'):
            from .x_api_discovery import XAPIDiscovery
            return XAPIDiscovery(self.settings,self.store,self.http).run(job)
        folder = self.settings.root / 'media' / job['job_id']
        folder.mkdir(parents=True, exist_ok=True)
        discovered = 0
        for source in self.settings.config['sources']:
            handle = source['x_handle']
            cursor_key = 'x:' + handle
            cursor = self.store.query('SELECT value FROM source_cursors WHERE key=?', (cursor_key,))
            response = self.x.latest(handle, job_id=job['job_id'], since_id=cursor[0]['value'] if cursor else None,
                                     recorder=lambda receipt:self.source_receipts.record(job,receipt))
            archive = folder / (handle + '.json')
            archive.write_text(encode(response), 'utf-8')
            for post in response['posts']:
                child=self.store.enqueue('x', post['post']['id'], 'tweet_process', 'tweet', post, self.settings.config['policy_version'])
                self.source_receipts.bind(job,child,post,response['post_receipts'][post['post']['id']])
                discovered += 1
            if not response['catchup_complete']:
                raise HubError('X_CATCHUP_BOUND_REQUIRES_REVIEW', review=True)
            if response['posts']:
                last = str(max(int(p['post']['id']) for p in response['posts']))
                with self.store.transaction() as db:
                    db.execute('INSERT OR REPLACE INTO source_cursors VALUES(?,?)', (cursor_key, last))
        return {'posts_seen':discovered}

    def process(self, job):
        from .machine_routes import machine_service
        from .machine_tweets_v2 import VERSION as V2
        from .machine_tweets_api import VERSION as API,REFERENCE_VERSION as API_REFERENCE
        selected=next((value for value in (V2,API,API_REFERENCE) if isinstance(job['policy_version'],str) and
            (job['policy_version']==value or job['policy_version'].startswith(value+':'))),None)
        if selected:
            if self.settings.config.get('policy_version')!=selected:raise HubError('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')
            try:return machine_service(self.settings,self.store,job['policy_version']).process(job,self.model)
            except HubError as exc:
                if exc.review:raise HubError(exc.code,retryable=exc.retryable,retry_after=exc.retry_after) from None
                raise
        from .machine_tweets import MachineTweets,VERSION,accepted_job_policy
        job_policy=job['policy_version']
        machine=isinstance(job_policy,str) and (job_policy==VERSION or job_policy.startswith(VERSION+':'))
        if machine:
            if not self.settings.config.get('machine_tweet_enabled',False):raise HubError('MACHINE_TWEET_POLICY_DISABLED')
            if self.settings.config.get('policy_version')!=VERSION:raise HubError('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')
            if job_policy!=accepted_job_policy(self.settings):raise HubError('MACHINE_SOURCE_GENERATION_NOT_ACTIVE')
        if machine:
            from .web_source import WebSources
            from .outcomes import JobOutcome
            existing=self.store.query("SELECT * FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=? AND account_id=?",
                (job['source_id'],'site_uid:'+self.settings.config['target_bilibili_site_uid']))
            if existing:
                if existing[0]['state']=='completed':
                    return JobOutcome('completed',{'already_published':True,'url':existing[0]['external_url']},external_id=existing[0]['external_id'])
                raise HubError('MACHINE_SOURCE_ALREADY_RESERVED')
            MachineTweets(self.settings,self.store).fresh(job)
            WebSources(self.settings,self.store).assess(job)
            if job['result'].get('machine_web'):
                return MachineTweets(self.settings,self.store).resume_prepared(job)
        source = dict(job['payload'])
        folder = self.settings.root / 'media' / job['job_id']
        folder.mkdir(parents=True, exist_ok=True)
        archive = folder / 'source.json'
        if not archive.exists():
            archive.write_text(encode(source), 'utf-8')
        def invoke(phase,translation=None):
            if machine:
                # Recheck before each billable phase, including when a clear or
                # the age boundary occurred during the translation request.
                MachineTweets(self.settings,self.store).fresh(job)
            try:return self.model.invoke(job,phase,source,translation)
            except HubError as exc:
                # Machine-mode exceptions stop/alert this job. They never ask the
                # owner to attest a translation, screenshot or source manually.
                if machine and exc.review:
                    raise HubError(exc.code,retryable=exc.retryable,retry_after=exc.retry_after) from None
                raise
        translated = invoke('translation')
        translation = translated['translation']
        qa = invoke('quality',translation)
        evidence = machine_evidence(source, translation, qa, self.settings.config.get('glossary'))
        (folder / 'translation.json').write_text(encode(translated), 'utf-8')
        (folder / 'machine-evidence-v1.json').write_text(encode(evidence), 'utf-8')
        source_evidence=self.source_receipts.assess(job)
        (folder/'source-receipt-v1.json').write_text(encode(source_evidence),'utf-8')
        model_evidence=self.model_receipts.assess(job,translation)
        (folder/'model-receipt-v1.json').write_text(encode(model_evidence),'utf-8')
        checkpoint = {**{k:job['result'][k] for k in ('translation_contract_version','translation_history','previous_result_sha256') if k in job['result']},
                      'source_sha256':sha256_file(archive), 'evidence':evidence, 'folder':str(folder),
                      'source_evidence':source_evidence,'model_evidence':model_evidence}
        self.store.checkpoint(job, 'translated', checkpoint)
        if not evidence['machine_checks_passed']:
            raise HubError('TRANSLATION_CHECK_FAILED', review=not machine)
        if machine:
            return MachineTweets(self.settings,self.store).prepare(job,translation,qa,checkpoint)
        with exclusive(self.settings.root / 'data/locks/heavy'):
            hosts = self.settings.config['allowed_media_hosts']
            configured=next((s for s in self.settings.config['sources'] if s['x_handle']==source['author']['username']),{})
            avatar = source['author'].get('profile_image_url') or configured.get('profile_image_url')
            if avatar:
                info = download(avatar, folder / 'avatar.bin', allowed_hosts=hosts, max_bytes=5_000_000,
                                check_disk=self.maintenance.check_disk)
                source['avatar_file'] = info['file']
            source['local_media'] = []
            capture=source.get('capture',{})
            if isinstance(capture,dict) and capture.get('screenshot_sha256'):
                screenshot=folder/'browser-source.png'
                if (screenshot.is_symlink() or not screenshot.is_file() or sha256_file(screenshot)!=capture['screenshot_sha256']):
                    raise HubError('SOURCE_SCREENSHOT_CAPTURE_INTEGRITY_CHANGED',review=True)
                source['source_screenshot_file']=str(screenshot)
                source['source_screenshot_captured_at']=capture.get('captured_at')
            for index, key in enumerate(source['post'].get('attachments', {}).get('media_keys', [])):
                media = source.get('media', {}).get(key, {})
                if media.get('type') != 'photo' or not media.get('url'):
                    raise HubError('X_MEDIA_TYPE_OR_SOURCE_UNVERIFIED', review=True)
                info = download(media['url'], folder / f'media-{index}.bin', allowed_hosts=hosts, max_bytes=20_000_000,
                                check_disk=self.maintenance.check_disk)
                source['local_media'].append(info)
            rendered = self.compositor.render(compositor_input(source, translation), folder)
        checkpoint['render'] = rendered
        checkpoint['missing_review'] = [x for x in rendered['report'].get('issues', []) if x['severity'] == 'error']
        self.store.checkpoint(job, 'rendered_awaiting_human', checkpoint)
        # No automated path writes any manual confirmation flag or enables a publisher.
        raise HubError('HUMAN_SOURCE_AND_TRANSLATION_REVIEW_REQUIRED', review=True)
