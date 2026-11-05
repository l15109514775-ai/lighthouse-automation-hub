from dataclasses import dataclass
from pathlib import Path
import os
import tomllib
import hashlib
import json
import re
import stat


def _image_visual_retired(root):
    marker=root/'x-visual-retired.json'
    if not marker.exists() and not marker.is_symlink():return False
    paths=('adapters/x_web.py','services/hub/x_web_discovery.py','adapters/browser_x.mjs',
        'adapters/browser_expiry_preflight.mjs','scripts/capture-extended-acceptance.mjs',
        'scripts/login-gui/browser.mjs','scripts/login-gui/session.py',
        'scripts/login-gui/lighthouse-x-login-gui.service','scripts/login-gui/Dockerfile')
    protected=('adapters/browser_publisher.mjs','adapters/bilibili.py','services/hub/publishing.py',
        'compositor/package.json','compositor/src/renderer.mjs','compositor/template.json')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError()
            value[key]=item
        return value
    try:
        if marker.is_symlink() or not stat.S_ISREG(marker.lstat().st_mode) or marker.stat().st_size>16384:raise ValueError()
        value=json.loads(marker.read_bytes(),object_pairs_hook=unique)
        if (set(value)!={'version','base_image','paths','protected','upstream_files_verified'} or
                value['version']!='x-visual-runtime-retired-v1' or value['upstream_files_verified']!=281 or
                type(value['upstream_files_verified']) is not int or
                not re.fullmatch(r'(?:[^@\s]+@)?sha256:[a-f0-9]{64}',value['base_image'])):raise ValueError()
        for field,expected in (('paths',paths),('protected',protected)):
            items=value[field]
            if not isinstance(items,list) or [i['path'] for i in items]!=list(expected):raise ValueError()
            for item in items:
                if set(item)!={'path','sha256'} or not re.fullmatch('[a-f0-9]{64}',item['sha256']):raise ValueError()
                file=root/item['path']
                if field=='paths':
                    if file.exists() or file.is_symlink():raise ValueError()
                elif (file.is_symlink() or any(p.is_symlink() for p in file.parents if p!=root.parent) or
                        not stat.S_ISREG(file.lstat().st_mode) or hashlib.sha256(file.read_bytes()).hexdigest()!=item['sha256']):raise ValueError()
        return True
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        raise ValueError('Invalid X visual retirement image marker') from None


@dataclass
class Settings:
    root: Path
    config: dict
    dry_run: bool = True

    @classmethod
    def load(cls):
        root = Path(os.environ.get('HUB_ROOT', Path(__file__).resolve().parents[2])).resolve()
        path = Path(os.environ.get('HUB_CONFIG', root / 'config/hub.toml'))
        cfg = tomllib.loads(path.read_text('utf-8-sig'))
        image_retired=_image_visual_retired(root)
        if type(cfg.get('x_visual_retired',False)) is not bool:
            raise ValueError('X visual source transport has been retired')
        if image_retired:cfg['x_visual_retired']=True
        for name, budget in cfg['budgets'].items():
            for key in budget:
                value = os.getenv(f'BUDGET_{name}_{key}'.upper())
                if value is not None:
                    budget[key] = value.lower() == 'true' if isinstance(budget[key], bool) else float(value)
        if cfg['max_attempts'] < 1 or cfg['heavy_concurrency'] != 1:
            raise ValueError('Invalid retry/concurrency configuration')
        if cfg['disk_floor_gb'] < 15:
            raise ValueError('Disk floor must be at least 15 GB')
        if (type(cfg.get('x_web_enabled',False)) is not bool or type(cfg.get('x_api_enabled',False)) is not bool
                or (cfg.get('x_web_enabled') and cfg.get('x_api_enabled'))):
            raise ValueError('Choose one X source transport')
        if type(cfg.get('x_visual_retired',False)) is not bool or (cfg.get('x_visual_retired') and cfg.get('x_web_enabled')):
            raise ValueError('X visual source transport has been retired')
        if type(cfg.get('web_reviewed_queue_enabled',False)) is not bool:
            raise ValueError('Invalid reviewed website queue switch')
        machine_policy=cfg.get('policy_version')
        machine_transport=(cfg.get('x_api_enabled') and not cfg.get('x_web_enabled')) if machine_policy in ('machine-tweet-v3-api','machine-tweet-v4-api-relations') else (
            machine_policy in ('machine-tweet-v1','machine-tweet-v2') and cfg.get('x_web_enabled'))
        if type(cfg.get('machine_tweet_enabled',False)) is not bool or (cfg.get('machine_tweet_enabled') and not machine_transport):
            raise ValueError('Invalid machine tweet policy switch')
        generation=cfg.get('machine_source_generation',1)
        if type(generation) is not int or not 1<=generation<=1000:
            raise ValueError('Invalid machine source generation')
        age=cfg.get('source_receipt_max_age_seconds',86400)
        if type(age) is not int or not 60<=age<=86400:
            raise ValueError('Invalid source receipt maximum age')
        if type(cfg.get('bilibili_token_refresh_enabled',False)) is not bool:
            raise ValueError('Invalid Bilibili OAuth refresh switch')
        if type(cfg.get('youtube_download_enabled',False)) is not bool:
            raise ValueError('Invalid YouTube download switch')
        cfg.setdefault('youtube_download_acceptance_only',False)
        if type(cfg['youtube_download_acceptance_only']) is not bool:
            raise ValueError('Invalid YouTube download acceptance-only switch')
        cfg.setdefault('youtube_natural_download_enabled',False)
        if type(cfg['youtube_natural_download_enabled']) is not bool or (cfg['youtube_natural_download_enabled'] and
                cfg.get('youtube_download_enabled') and not cfg['youtube_download_acceptance_only']):
            raise ValueError('Invalid or conflicting natural YouTube download switch')
        lead=cfg.get('bilibili_token_refresh_lead_seconds',900)
        if type(lead) is not int or not 60<=lead<=86400:
            raise ValueError('Invalid Bilibili OAuth refresh lead time')
        for key in ('youtube_feed_poll_minutes','youtube_live_poll_minutes','bilibili_reconcile_minutes'):
            if key in cfg and (isinstance(cfg[key],bool) or not isinstance(cfg[key],int) or not 1 <= cfg[key] <= 1440):
                raise ValueError('Invalid YouTube polling interval')
        for key,minimum,maximum in (('video_part_max_seconds',1,36000),('video_part_max_bytes',16384,14000000000),
            ('video_max_parts',1,32),('video_processing_timeout_seconds',30,28800)):
            if key in cfg and (type(cfg[key]) is not int or not minimum<=cfg[key]<=maximum):
                raise ValueError('Invalid bounded video processing configuration')
        for budget in cfg['budgets'].values():
            if any(float(value) < 0 for key, value in budget.items() if key != 'paid_authorized'):
                raise ValueError('Negative budget')
        return cls(root, cfg, os.getenv('DRY_RUN', 'true').lower() != 'false')

    def prepare(self):
        for folder in ('data', 'media', 'logs', 'backups', 'data/locks'):
            (self.root / folder).mkdir(parents=True, exist_ok=True)

    @property
    def db_path(self):
        return self.root / 'data/hub.sqlite3'
