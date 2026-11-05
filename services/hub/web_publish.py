"""Server-side coordinator for one explicitly owner-reviewed tweet image.

The browser prepares a draft, this process durably reserves dispatch, and only
then grants one send. Neither retries nor a restarted process blindly resend.
"""
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from datetime import datetime

from .db import encode
from .errors import HubError, PublishUnknown
from .evidence import sha256_file
from .locks import resource_guard
from .supervised_web import SupervisedWeb, KIND


def run_reviewed_tweet(settings, store, job_id):
    service = SupervisedWeb(settings, store)
    return _run_website(settings,store,job_id,service,KIND,'maintenance')


def run_machine_tweet(settings,store,job_id):
    from .machine_routes import machine_service
    job=store.get(job_id)
    if not job:raise HubError('MACHINE_QUEUE_REQUEST_INVALID')
    service=machine_service(settings,store,job['policy_version'])
    return _run_website(settings,store,job_id,service,service.kind,'machine-policy')


def _run_website(settings,store,job_id,service,kind,principal):
    with resource_guard(settings.root/'data/locks/heavy'):
        job, manifest = service._snapshot(job_id)
        known = store.query('SELECT * FROM publications WHERE job_id=? AND kind=?', (job_id, kind))
        if known and known[0]['state'] == 'completed':
            return {'state': 'completed', 'url': known[0]['external_url'], 'external_id': known[0]['external_id'], 'sent': False}
        attempt_id = str(uuid.uuid4())
        folder = settings.root/'media'/job_id/'website-attempts'/attempt_id
        folder.mkdir(parents=True, mode=0o700)
        unresolved=bool(known and known[0]['state']!='rejected')
        spec = {'version': 'bilibili-ui-transport-v1', 'job_id': job_id, 'attempt_id': attempt_id,
                'mode': 'lookup' if unresolved else 'publish', 'source_url': manifest['source_url'],
                'target_site_uid': manifest['target_site_uid'], 'target_name': manifest['target_name'],
                'file': manifest['render']['png'], 'artifact_sha256': manifest['render']['sha256'], 'caption': manifest['caption']}
        if principal=='machine-policy':
            spec.update(machine_policy=service.version,dispatch_expires_at=
                datetime.fromisoformat(job['payload']['post']['created_at'].replace('Z','+00:00')).timestamp()+
                service.policy()['max_post_age_seconds'])
        # The browser does not inherit relay/API or maintenance credentials.
        env = {k: os.environ[k] for k in ('PATH', 'PLAYWRIGHT_BROWSERS_PATH', 'TEMP', 'TMP', 'TMPDIR') if k in os.environ}
        env.update(HUB_ROOT=str(settings.root), BILIBILI_EXPECTED_SITE_UID=manifest['target_site_uid'],
                   HUB_BROWSER_CHANNEL=settings.config.get('web_browser_channel','chromium'),
                   XDG_CONFIG_HOME='/tmp/lighthouse-browser-config',XDG_CACHE_HOME='/tmp/lighthouse-browser-cache')
        proc = subprocess.Popen(['node', str(settings.root/'adapters/browser_publisher.mjs')],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        proc.stdin.write(encode(spec).encode('utf-8')); proc.stdin.close()
        reserved = unresolved
        try:
            deadline = time.monotonic()+18*60
            preparation_deadline = time.monotonic()+120
            while time.monotonic()<deadline:
                result_file = folder/'result.json'
                if result_file.exists():
                    outcome = json.loads(result_file.read_text('utf-8'))
                    if outcome['state'] in ('published_visible', 'existing_visible'):
                        evidence = outcome['evidence']
                        evidence_file = folder/'visible-result.json'
                        evidence_file.write_text(encode(evidence), 'utf-8')
                        if not reserved:
                            permit = service.reserve(principal, job_id, site_uid=spec['target_site_uid'],
                                artifact_sha256=spec['artifact_sha256'], caption_sha256=manifest['caption_sha256'])
                            if not permit['can_click_publish']:raise PublishUnknown()
                            reserved = True
                        return service.record_visible(principal, job_id, evidence_file)
                    if reserved:
                        if outcome.get('error_code')=='AUTH_EXPIRED':
                            store.event('auth.expired',{'job_id':job_id,'component':'bilibili_website'},dedup_key='web-auth:'+job_id)
                        raise PublishUnknown()
                    raise HubError(outcome.get('error_code', 'WEB_PUBLICATION_BLOCKED'), review=principal=='maintenance')
                ready = folder/'ready.json'
                if ready.exists() and not reserved:
                    actual = json.loads(ready.read_text('utf-8'))
                    if (actual.get('attempt_id') != attempt_id or actual.get('site_uid') != spec['target_site_uid'] or
                            actual.get('artifact_sha256') != spec['artifact_sha256'] or
                            actual.get('caption_sha256') != manifest['caption_sha256']):
                        raise HubError('SUPERVISED_WEBSITE_DRAFT_MISMATCH', review=True)
                    permit = service.reserve(principal, job_id, site_uid=spec['target_site_uid'],
                        artifact_sha256=spec['artifact_sha256'], caption_sha256=manifest['caption_sha256'])
                    if not permit['can_click_publish']:raise PublishUnknown()
                    reserved = True
                    tmp = folder/'commit.partial'
                    tmp.write_text(encode({'action': 'publish', 'attempt_id': attempt_id,
                                          'artifact_sha256': spec['artifact_sha256']}), 'utf-8')
                    tmp.replace(folder/'commit.json')
                if proc.poll() is not None:
                    if reserved:raise PublishUnknown()
                    raise HubError('WEB_BROWSER_EXITED_BEFORE_DISPATCH', review=True)
                if not ready.exists() and time.monotonic()>preparation_deadline:
                    if reserved:raise PublishUnknown()
                    raise HubError('WEB_DRAFT_PREPARATION_TIMEOUT', retryable=True)
                time.sleep(0.25)
            raise PublishUnknown() if reserved else HubError('WEB_DRAFT_PREPARATION_TIMEOUT', retryable=True)
        finally:
            # The result is written before the Node transport's finally closes
            # Chromium. Give that cleanup its bounded natural exit window first.
            if proc.poll() is None:
                try:proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.terminate()
                    try:proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=5)
