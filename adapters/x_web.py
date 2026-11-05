"""Bounded server browser observations; separate from official API receipts."""
import json
import os
from pathlib import Path
import subprocess
import signal
import uuid
from services.hub.db import encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.locks import resource_guard


class XWeb:
    def __init__(self, settings):
        self.settings = settings

    def latest(self, handle, *, job_id, seen_ids=(), not_before=0, trigger='durable_job'):
        if handle not in {'example_handle_a', 'example-channel-b'}:
            raise HubError('X_WEB_HANDLE_NOT_CONFIGURED', review=True)
        try:
            if str(uuid.UUID(job_id)) != job_id: raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise HubError('X_WEB_JOB_INVALID', review=True) from None
        attempt = str(uuid.uuid4())
        folder = self.settings.root/'media'/job_id/'x-web'/attempt
        spec = {'version': 'x-web-discovery-v1', 'job_id': job_id, 'attempt_id': attempt,
                'handle': handle, 'seen_ids': list(seen_ids)[:1000], 'not_before':not_before,'trigger':trigger,
                'machine_source':self.settings.config.get('machine_tweet_enabled',False),
                'machine_policy':self.settings.config.get('policy_version')}
        env = {key: os.environ[key] for key in ('PATH', 'PLAYWRIGHT_BROWSERS_PATH', 'TEMP', 'TMP', 'TMPDIR') if key in os.environ}
        env.update(HUB_ROOT=str(self.settings.root), HUB_BROWSER_CHANNEL='chromium',
                   XDG_CONFIG_HOME='/tmp/lighthouse-browser-config', XDG_CACHE_HOME='/tmp/lighthouse-browser-cache')
        with resource_guard(self.settings.root/'data/locks/heavy'):
            try:
                command=['xvfb-run','-a','--server-args=-screen 0 1366x1800x24 -nolisten tcp',
                         'node',str(self.settings.root/'adapters/browser_x.mjs')]
                proc = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                    env=env,start_new_session=os.name!='nt')
                try:
                    proc.communicate(encode(spec).encode(),timeout=360)
                except subprocess.TimeoutExpired:
                    # Kill this isolated process group, including its display and
                    # browser children, before releasing the shared resource lock.
                    if os.name!='nt':os.killpg(proc.pid,signal.SIGTERM)
                    else:proc.terminate()
                    try:proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        if os.name!='nt':os.killpg(proc.pid,signal.SIGKILL)
                        else:proc.kill()
                        proc.communicate(timeout=5)
                    raise
            except (OSError, subprocess.SubprocessError):
                raise HubError('X_WEB_BROWSER_UNAVAILABLE', retryable=True) from None
        file = folder/'result.json'
        if proc.returncode or file.is_symlink() or not file.is_file() or file.stat().st_size>2_000_000:
            raise HubError('X_WEB_RESULT_UNAVAILABLE', review=True)
        try:
            value = json.loads(file.read_text('utf-8'))
            if not (value['version']=='x-web-discovery-v1' and value['handle']==handle and
                    value['official_api'] is False and value['human_review'] is False and
                    value['timeline_complete'] is False and isinstance(value['posts'],list)):
                raise ValueError()
            for record in value['posts']:
                source=record['payload'];capture=source['capture']
                image=Path(record['screenshot_file'])
                if not (not image.is_symlink() and image.resolve().is_relative_to(folder.resolve()) and
                    sha256_file(image)==capture['screenshot_sha256'] and
                    source['author']['username']==handle and source['source_api']=='x-web-dom-v1' and
                    source['url']==f"https://x.com/{handle}/status/{source['post']['id']}" and
                    capture['human_review'] is False and capture['source_screenshot_reviewed'] is False and
                    capture['visibility_passed'] is True and capture['official_api_receipt'] is False and
                    capture['full_source_completeness_verified'] is False):raise ValueError()
                if capture['protocol']=='x-web-extended-observation-v1':
                    if (self.settings.config.get('policy_version')!='machine-tweet-v2' or
                        record.get('version')!='x-web-extended-observation-v1' or
                        not isinstance(record.get('machine_extended_source'),dict)):raise ValueError()
                elif source['source_media_count'] is not None or source['references_observed'] is not False:raise ValueError()
        except (KeyError, ValueError, TypeError, OSError, AssertionError):
            raise HubError('X_WEB_CAPTURE_INTEGRITY_INVALID', review=True) from None
        value['result_file']=str(file)
        return value
