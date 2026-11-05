"""Later native reconciliation on a nonempty manager's observed published row.

It neither submits nor creates a reservation. Single-file V2/V3 only: multipart
requires its own per-P public proof, not this single-player contract.
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from .replay_later_native_result_v1 import require, when, _record_later_native_result
from .youtube_download import digest

VERSION='replay-later-native-result-reconciliation-v2'
ORIGIN='later_native_lookup_reconciled_v2'
MANAGER='https://member.bilibili.com/platform/upload-manager/article'
ROUTES={'replay-browser-highest-v2','replay-browser-natural-v3'}
UUID=re.compile(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}')


def _common(value,binding,*,now=None):
    now=time.time() if now is None else now
    require(value.get('version')=='replay-native-manager-row-v2' and
        all(value.get(k)==v for k,v in {'job_id':binding['job_id'],'site_uid':binding['site_uid'],
            'source_id':binding['source_id'],'source_url':binding['source_url'],
            'metadata_sha256':binding['metadata_sha256']}.items()) and
        type(value.get('attempt_id')) is str and UUID.fullmatch(value['attempt_id']) and
        type(value.get('binding_sha256')) is str and re.fullmatch('[a-f0-9]{64}',value['binding_sha256']) and
        0<=now-when(value['observed_at'])<=300 and
        all(type(value.get(k)) is int and value[k]==0 for k in ('file_selections','save_clicks','submit_clicks','player_seeks')) and
        value.get('source_or_media_modified') is False)
    dom=value['visible_dom'];require(dom.get('url')==MANAGER and dom.get('truncated') is False and
        isinstance(dom.get('rows'),list) and len(dom['rows'])<=60)
    text=dom.get('visible_text');require(type(text) is str and
        all(label in text for label in ('视频管理','图文管理','互动视频管理')) and
        len(re.findall(r'(?:^|\n)全部稿件\s+\d+(?:\n|$)',text))==1)
    return dom


def _bv(raw):
    u=urlsplit(raw or '')
    require(u.scheme=='https' and u.netloc=='www.bilibili.com' and not u.query and not u.fragment)
    found=re.fullmatch(r'/video/(BV[A-Za-z0-9]{10})/?',u.path);require(found is not None)
    return found[1]


def validate_manager_row(value,binding,external,*,duration_seconds,now=None):
    """One observed title anchor and one cover anchor, not all-account count 1."""
    dom=_common(value,binding,now=now)
    matches=[r for r in dom['rows'] if any(a.get('text')==binding['metadata']['title'] for a in r.get('title_links',[]))]
    require(len(matches)==1);row=matches[0]
    require(value.get('selected_row')==row and {'article-card','clearfix','v2'}<=set(row.get('class','').split()) and
        len(row.get('title_links',[]))==len(row.get('covers',[]))==len(row.get('dates',[]))==len(row.get('durations',[]))==1)
    title=row['title_links'][0];cover=row['covers'][0]
    require(title.get('tag')==cover.get('tag')=='A' and title.get('visible') is cover.get('visible') is True and
        {'name','ellipsis'}<=set(title.get('class','').split()) and 'cover-wrp' in cover.get('class','').split() and
        title['text']==binding['metadata']['title'] and _bv(title['href'])==_bv(cover['href'])==external)
    date=row['dates'][0];require('date' in date.get('class','').split() and 'is-success' in date.get('parent_class','').split())
    found=re.fullmatch(r'(\d{4})年(\d{2})月(\d{2})日 (\d{2}):(\d{2}):(\d{2})',date.get('text',''));require(found is not None)
    duration=re.fullmatch(r'(\d{1,2}):(\d{2}):(\d{2})',row['durations'][0]);require(duration is not None)
    hours,minutes,seconds=map(int,duration.groups())
    require(minutes<60 and seconds<60 and abs(hours*3600+minutes*60+seconds-duration_seconds)<=2)
    try:return datetime(*map(int,found.groups()),tzinfo=timezone(timedelta(hours=8))).timestamp()
    except ValueError:require(False)


def validate_manager_before(value,binding,state,intent,*,expected_binding_sha256):
    # The record was fresh when captured, not necessarily when long QA ends.
    dom=_common(value,binding,now=when(value['observed_at']))
    require(value['attempt_id']==state['attempt_id'] and
        value['binding_sha256']==expected_binding_sha256 and
        0<when(intent['created_at'])-when(value['observed_at'])<=900 and
        not any(any(a.get('text')==binding['metadata']['title'] for a in row.get('title_links',[])) for row in dom['rows']) and
        value.get('selected_row') is None and value.get('known_external_id') is None)


def record_later_native_result_v2(service,principal,job_id,*,public_reference,screenshot_reference,
        manager_reference,before_reference,actual_operator_reference,apply=False):
    service._operator(principal)
    require(service.NATIVE_DIRECTORY in ROUTES)
    job,binding,state=service.snapshot(job_id,write=False)
    base=service.settings.root/'media'/job_id/service.NATIVE_DIRECTORY/state['attempt_id']/'later-lookups-v2'
    for reference in (public_reference,screenshot_reference,manager_reference,before_reference):
        file=Path(reference['file']);require(file.parent.parent==base and UUID.fullmatch(file.parent.name))
    require(Path(manager_reference['file']).name=='manager-row.json' and Path(before_reference['file']).name=='manager-row.json' and
        Path(public_reference['file']).name=='public-native-inventory.json' and Path(screenshot_reference['file']).name=='public-page.png' and
        Path(manager_reference['file']).parent==Path(public_reference['file']).parent==Path(screenshot_reference['file']).parent and
        Path(before_reference['file']).parent!=Path(manager_reference['file']).parent)
    def after(value,augmented,external):
        require(value['attempt_id']==state['attempt_id'] and value['binding_sha256']==digest(binding))
        source=service.store.get(binding['download_job_id'])
        duration=source['result'][service.DOWNLOAD_STATE]['binding']['duration']
        return validate_manager_row(value,augmented,external,duration_seconds=duration)
    def before(value,augmented,current,intent):
        return validate_manager_before(value,augmented,current,intent,expected_binding_sha256=digest(binding))
    return _record_later_native_result(service,principal,job_id,public_reference=public_reference,
        screenshot_reference=screenshot_reference,manager_reference=manager_reference,
        before_reference=before_reference,actual_operator_reference=actual_operator_reference,apply=apply,
        version=VERSION,origin=ORIGIN,before_validator=before,manager_validator=after)
