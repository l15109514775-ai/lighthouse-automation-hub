from datetime import datetime
from pathlib import Path
import json
import os
import subprocess
from zoneinfo import ZoneInfo
from services.hub.errors import HubError
from services.hub.evidence import sha256_file


def compositor_input(source, translation):
    post, author = source['post'], source['author']
    refs = post.get('referenced_tweets', [])
    relation = 'repost' if any(x['type'] == 'retweeted' for x in refs) else 'reply' if any(x['type'] == 'replied_to' for x in refs) else 'original'
    published = post.get('created_at')
    if published:
        published = datetime.fromisoformat(published.replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Tokyo')).isoformat()
    captured=source.get('source_screenshot_captured_at')
    if isinstance(captured,str):
        try:
            moment=datetime.fromisoformat(captured.replace('Z','+00:00'))
            if moment.tzinfo is not None:captured=moment.isoformat(timespec='seconds')
        except ValueError:pass  # Keep invalid values visible to the original renderer's checks.
    return {'schemaVersion':1, 'displayTimezone':'Asia/Shanghai', 'post':{
        'id':post['id'], 'url':source['url'], 'author':{'name':author['name'], 'handle':author['username'],
            'avatar':source.get('avatar_file'), 'verified':False},
        'originalText':post['text'], 'translation':translation, 'publishedAt':published or '', 'sourceTimezone':'Asia/Tokyo',
        'sourceScreenshot':{'file':source.get('source_screenshot_file'),
            'capturedAt':captured, 'reviewed':False},
        'sourceMediaCount':(source.get('source_media_count') if source.get('source_api') in ('manual-input','x-web-dom-v1')
                            else len(post.get('attachments', {}).get('media_keys', []))),
        'sourceQuote':('unknown' if source.get('source_api') in ('manual-input','x-web-dom-v1') and not source.get('references_observed')
                       else 'present' if any(x['type'] in ('quoted','retweeted') for x in refs) else 'none'),
        'quote':None, 'media':[{'file':item['file'], 'completeness':'unknown', 'reviewed':False} for item in source.get('local_media', [])],
        'relationship':{'type':relation}, 'review':{'translationConfirmed':False, 'sourceConfirmed':False},
        'notes':['自动化草稿；来源截图、素材与译文尚待人工核对。']}}


class Compositor:
    def __init__(self, root, browser_path=None):
        self.root = Path(root)
        self.browser_path = Path(browser_path) if browser_path is not None else self.root / 'compositor/runtime/browsers'

    def render(self, input_data, folder, *, mode='preview', allowed_root=None):
        if mode not in ('preview','publish'):
            raise HubError('COMPOSITOR_MODE_INVALID',review=True)
        folder = Path(folder).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        allowed = Path(allowed_root).resolve() if allowed_root is not None else folder
        if not folder.is_relative_to(allowed):
            raise HubError('COMPOSITOR_ASSET_ROOT_INVALID',review=True)
        input_file = folder / 'compositor-input.json'
        input_file.write_text(json.dumps(input_data, ensure_ascii=False, indent=2), 'utf-8')
        out = folder / 'rendered'
        out.mkdir(exist_ok=True)
        result_file = out / 'hub-render-result.json'
        result_file.unlink(missing_ok=True)
        env = {**os.environ, 'PLAYWRIGHT_BROWSERS_PATH':str(self.browser_path),
               'TEMP':str(allowed), 'TMP':str(allowed), 'TMPDIR':str(allowed)}
        try:
            proc = subprocess.run(['node', str(self.root / 'adapters/render_bridge.mjs'), str(input_file), str(out), str(allowed),mode],
                                  cwd=self.root / 'compositor', env=env, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            raise HubError('COMPOSITOR_RUNTIME_UNAVAILABLE', review=True) from None
        # Never reuse a stale result from an earlier attempt.
        if not result_file.is_file() or result_file.stat().st_mtime < input_file.stat().st_mtime:
            raise HubError('COMPOSITOR_RUNTIME_FAILED', review=True)
        result = json.loads(result_file.read_text('utf-8'))
        if proc.returncode or not result['report'].get('png'):
            codes = [x['code'] for x in result['report'].get('issues', [])]
            if mode=='publish' and not {'BROWSER_NOT_INSTALLED','RENDER_FAILED'}&set(codes):
                raise HubError('COMPOSITOR_PUBLISH_BLOCKED',review=True)
            raise HubError('BROWSER_NOT_INSTALLED' if 'BROWSER_NOT_INSTALLED' in codes else 'COMPOSITOR_RENDER_FAILED', review=True)
        png = Path(result['dir']) / result['report']['png']['file']
        if not png.resolve().is_relative_to(folder) or not png.is_file():
            raise HubError('COMPOSITOR_OUTPUT_INVALID', review=True)
        return {'report':result['report'], 'png':str(png), 'sha256':sha256_file(png),
                'input_sha256':sha256_file(input_file),'report_file':str(result_file)}
