"""Independent validation of native manager and every public P observation.

No browser, network, source-file access or sending authority lives here.
Historical result lookup does not depend on a current sending grant.
"""
from datetime import datetime, timezone, timedelta
import math
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urljoin, urlparse, parse_qs

from .errors import HubError
from .replay_machine_checks import read_checked
import json


def fail():
    raise HubError('REPLAY_MULTIPART_PUBLIC_EVIDENCE_REQUIRED', review=True)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        fail()
    return parsed.timestamp()


def fresh(value):
    return 0 <= time.time() - timestamp(value) <= 300


def video_url(value, bv, index=None):
    p = urlparse(value)
    if p.scheme != 'https' or p.netloc != 'www.bilibili.com' or p.username or p.password or p.fragment or p.path not in ('/video/' + bv, '/video/' + bv + '/'):
        return False
    query = parse_qs(p.query, keep_blank_values=True)
    return (not query if index is None else set(query) <= {'p'} and query.get('p', ['1']) == [str(index)])


def normalize_description(raw, binding):
    prefix = binding['source_url'] + '\n'
    value = raw[len(prefix):] if raw.startswith(prefix) else raw
    if value != binding['metadata']['description']:
        fail()
    return {'version': 'bilibili-description-source-prefix-v1', 'observed_description': raw,
        'normalized_description': value, 'prefix_occurrences_removed': int(raw.startswith(prefix))}


def identity(dom, binding, bv, index):
    if (not video_url(dom['url'], bv, index) or dom['titles'] != [binding['metadata']['title']] or
            len(dom['owners']) != 1 or len(dom['descriptions']) != 1):
        fail()
    owner = dom['owners'][0]
    u = urlparse(urljoin('https://www.bilibili.com', owner['href']))
    if (u.scheme != 'https' or u.netloc != 'space.bilibili.com' or u.username or u.password or u.query or u.fragment or
            u.path.rstrip('/') != '/' + binding['site_uid'] or owner['text'] != binding['account_name'] or
            'up-name' not in owner['classes'].split() or binding['account_name'] not in owner['card_text']):
        fail()
    return normalize_description(dom['descriptions'][0], binding)


def native_published_at(row):
    if len(row['dates']) != 1:
        fail()
    date = row['dates'][0]
    match = re.fullmatch(r'(\d{4})年(\d{2})月(\d{2})日\s+(\d{2}):(\d{2}):(\d{2})', date['text'])
    if match is None or 'pubdate' not in date['parent_classes'].split():
        fail()
    return datetime(*map(int, match.groups()), tzinfo=timezone(timedelta(hours=8))).timestamp()


def validate_public_result(service, job, binding, state, observation, result_reference):
    try:
        root = service.settings.root
        output = root / 'media' / job['job_id'] / service.NATIVE_DIRECTORY / state['attempt_id']
        base = service._native_base(job, binding, state)
        def read(reference, *, maximum=20_000_000, image=False):
            if not isinstance(reference, dict) or set(reference) != {'file', 'bytes', 'sha256'}:
                fail()
            file = Path(reference['file'])
            if not file.is_relative_to(output):
                fail()
            actual, raw = read_checked(root, file, maximum=maximum)
            if actual != reference:
                fail()
            if image:
                if len(raw) < 33 or raw[:16] != b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR':
                    fail()
                return raw
            return json.loads(raw)
        def bound(body, version):
            if (body.get('version') != version or any(body.get(k) != v or type(body.get(k)) is not type(v) for k, v in base.items()) or
                    not fresh(body['observed_at'])):
                fail()
            return body
        if read(result_reference) != observation:
            fail()
        bound(observation, service.RESULT_OBSERVATION_VERSION)
        if (observation.get('origin') != 'later_native_lookup_reconciled_v4' or observation.get('same_submit_attempt_observed') is not False or
                observation.get('human_review') is not False or observation.get('server_file_hash_verified') is not False):
            fail()
        bv = observation['external_id']
        if not re.fullmatch('BV[A-Za-z0-9]{10}', bv) or observation['url'] != 'https://www.bilibili.com/video/' + bv:
            fail()
        previous = state.get('result_observation')
        if previous is not None and previous['external_id'] != bv:
            fail()
        manager = bound(read(observation['manager_observation']), 'replay-multipart-manager-observation-v4')
        if manager.get('read_only') is not True or manager['visible_dom']['url'] != 'https://member.bilibili.com/platform/upload-manager/article' or manager['visible_dom']['truncated'] is not False:
            fail()
        intent_file = root / 'media' / job['job_id'] / 'replay-native-intents/public-submit.json'
        actual_intent, raw_intent = read_checked(root, intent_file, maximum=1_000_000)
        intent = json.loads(raw_intent)
        ticket = intent['reservation']
        clicked_at = timestamp(intent['created_at'])
        if (manager['submit_intent'] != actual_intent or intent['version'] != 'replay-multipart-one-use-intent-v4' or
                intent['kind'] != 'public-submit' or any(intent.get(k) != v for k, v in base.items()) or
                ticket['idempotency_key'] != state['key'] or ticket['version'] != service.NATIVE_RESERVATION_VERSION or
                ticket.get('can_click_submit') is not True or str(uuid.UUID(ticket['ticket_id'])) != ticket['ticket_id'] or
                any(ticket.get(k) != v for k,v in base.items()) or
                ticket['upload_reference_sha256'] != state['upload_observation']['upload_reference_sha256'] or
                ticket['machine_evidence_sha256'] != state['machine_acceptance']['evidence_sha256'] or
                not all(type(ticket[k]) in (int,float) and math.isfinite(ticket[k]) for k in ('issued_at','expires_at')) or
                not 0 < ticket['expires_at']-ticket['issued_at'] <= 300 or
                not ticket['issued_at'] <= clicked_at <= ticket['expires_at']):
            fail()
        matches = [row for row in manager['visible_dom']['rows'] if any(a['text'] == binding['metadata']['title'] for a in row['title_links'])]
        if len(matches) > 1 or previous is None and len(matches) != 1:
            fail()
        if matches:
            row = matches[0]
            if (manager['selected_row'] != row or set(row['classes'].split()) != {'article-card', 'clearfix', 'v2'} or
                    len(row['title_links']) != 1 or len(row['covers']) != 1 or
                    row['title_links'][0]['text'] != binding['metadata']['title'] or
                    not video_url(row['title_links'][0]['href'], bv) or not video_url(row['covers'][0]['href'], bv) or
                    native_published_at(row) < clicked_at - 30 or
                    native_published_at(row) > timestamp(manager['observed_at']) + 60 or
                    observation['state']=='completed' and 'is-success' not in row['dates'][0]['parent_classes'].split()):
                fail()
        if manager['known_external_id'] not in (None, bv) or previous and manager['known_external_id'] != bv:
            fail()
        if observation['state'] == 'awaiting_moderation':
            pending = bound(read(observation['public_observation']), 'replay-multipart-public-coverage-pending-v4')
            if (observation.get('public_accessible') is not False or pending['external_id'] != bv or
                    pending.get('identity_verified') is not True or not pending['public_observations']):
                fail()
            first = bound(read(pending['public_observations'][0]), 'replay-multipart-public-part-observation-v4')
            identity(first['visible_dom'], binding, bv, first['index'])
            return {'result': observation, 'result_reference': result_reference, 'manager': observation['manager_observation'],
                'coverage': observation['public_observation'], 'public_accessible': False}
        if observation['state'] != 'completed' or observation.get('public_accessible') is not True:
            fail()
        coverage = bound(read(observation['cleanup_coverage']), 'replay-multipart-public-coverage-v4')
        n = len(binding['parts'])
        if (coverage['external_id'] != bv or coverage['url'] != observation['url'] or coverage['source_duration_seconds'] != binding['source_duration_seconds'] or
                coverage['expected_part_count'] != n or type(coverage['expected_part_count']) is not int or
                coverage['observed_part_count'] != n or type(coverage['observed_part_count']) is not int or coverage['public_parts'] != observation['public_parts'] or
                coverage['manager_observation'] != observation['manager_observation'] or len(coverage['public_observations']) != n or
                len(coverage['screenshots']) != n or len(coverage['playlist']) != n or len(coverage['public_parts']) != n or
                coverage['human_review'] is not False or coverage['server_file_hash_verified'] is not False):
            fail()
        _, prep_raw = read_checked(root, output / 'native-preparation.json', maximum=1_000_000)
        preparation = json.loads(prep_raw)
        if (preparation != state['native_preparation'] or preparation['version'] != service.NATIVE_PREPARATION_VERSION or
                any(preparation.get(k)!=v for k,v in base.items()) or
                preparation['upload_reference_sha256'] != state['upload_observation']['upload_reference_sha256']):
            fail()
        inner_reference=preparation['native_observation_file']
        inner=read(inner_reference,maximum=1_000_000)
        expected_inner={k:v for k,v in preparation.items() if k not in ('private_draft_saved','pre_submit_screenshot','native_observation_file')}
        expected_inner['version']=service.NATIVE_FORM_VERSION
        if inner!=expected_inner:fail()
        read(preparation['pre_submit_screenshot'],image=True)
        total, cids, descriptions = 0, [], []
        for i, (summary, reference, local, playlist) in enumerate(zip(coverage['public_parts'], coverage['public_observations'], binding['parts'], coverage['playlist']), 1):
            raw = bound(read(reference), 'replay-multipart-public-part-observation-v4')
            dom = raw['visible_dom']
            descriptions.append(identity(dom, binding, bv, i))
            locator = {'kind': 'visible_playlist_index', 'index': i, 'url': observation['url'] + '/?p=' + str(i)}
            if (raw['index'] != i or raw['external_id'] != bv or raw['part_locator'] != locator or raw.get('read_only') is not True or
                    raw['near_end_seek_performed'] is not True or raw['near_end_playability_observed'] is not True or
                    summary['index'] != i or summary['part_index'] != local['part_index'] or summary['part_idempotency_key'] != local['part_idempotency_key'] or
                    summary['part_locator'] != locator or len(dom['videos']) != 1 or dom.get('links_truncated') is not False):
                fail()
            player = dom['videos'][0]
            if (any(summary[k] != player[k] for k in ('duration_seconds', 'current_time', 'ready_state', 'seekable_ranges', 'error_code')) or
                    not all(type(player[k]) in (int, float) and math.isfinite(player[k]) for k in ('duration_seconds','current_time')) or
                    abs(player['duration_seconds'] - local['duration_seconds']) > 2 or type(player['ready_state']) is not int or
                    not 2<=player['ready_state']<=4 or player['error_code'] is not None or
                    any(type(player[k]) is not int or player[k]<=0 for k in ('video_width','video_height')) or
                    not 0<=player['current_time']<=player['duration_seconds']+1 or player['seeking'] is not False or
                    not (player['current_time']>max(0,player['duration_seconds']-5)+.1 or
                        player['ended'] is True and player['current_time']>=player['duration_seconds']-.2) or
                    not all(set(r)=={'start','end'} and all(type(v) in (int,float) and math.isfinite(v) for v in r.values()) and 0<=r['start']<r['end'] for r in player['seekable_ranges']) or
                    not any(r['end'] >= player['duration_seconds']-2 for r in player['seekable_ranges']) or
                    summary['near_end_playability_observed'] is not True or summary['near_end_seek_performed'] is not True or not fresh(summary['observed_at'])):
                fail()
            if playlist['index'] != i or not video_url(playlist['url'],bv,i):
                fail()
            visible_playlist=[]
            for link in dom['links']:
                absolute=urljoin(dom['url'],link['href']);parsed=urlparse(absolute);query=parse_qs(parsed.query)
                values=query.get('p',[])
                if (parsed.scheme=='https' and parsed.netloc=='www.bilibili.com' and parsed.path.rstrip('/')=='/video/'+bv and
                        len(values)==1 and values[0].isdigit() and 1<=int(values[0])<=32):
                    visible_playlist.append({'index':int(values[0]),'text':link['text'],
                        'url':parsed.scheme+'://'+parsed.netloc+parsed.path+'?p='+values[0]})
            if n==1 and not visible_playlist:
                if playlist.get('single_visible_video') is not True:fail()
            elif (len(visible_playlist)!=n or any(item['index']!=ordinal for ordinal,item in enumerate(visible_playlist,1)) or
                    any(any(item[k]!=declared[k] for k in ('index','url','text')) for item,declared in zip(visible_playlist,coverage['playlist']))):
                fail()
            allowed_names = [local['filename'], Path(local['filename']).stem, preparation['native_rows'][i-1]['native_filename']]
            if not any(name in [line.strip() for line in playlist['text'].split('\n')] for name in allowed_names):
                fail()
            cid_values = set()
            for value in [*dom['iframes'], player['current_src']]:
                parsed = urlparse(value)
                query = parse_qs(parsed.query)
                if parsed.scheme == 'https' and parsed.netloc in ('player.bilibili.com','www.bilibili.com') and query.get('bvid') == [bv] and len(query.get('cid',[]))==1 and query['cid'][0].isdigit():
                    cid_values.add(query['cid'][0])
            if len(cid_values) > 1 or summary['cid'] != (next(iter(cid_values)) if cid_values else None):
                fail()
            if summary['cid'] is not None:
                cids.append(summary['cid'])
            read(coverage['screenshots'][i-1], image=True)
            total += player['duration_seconds']
        if (len(cids) != len(set(cids)) or abs(total-binding['source_duration_seconds']) > 2 or
                coverage['total_duration_seconds'] != total or observation['total_duration_seconds'] != total):
            fail()
        identity(coverage['public_metadata_observation'],binding,bv,1)
        return {'result': observation, 'result_reference': result_reference, 'manager': observation['manager_observation'],
            'coverage': observation['cleanup_coverage'], 'description_normalization': descriptions,
            'public_accessible': True, 'all_parts_playable_verified': True}
    except HubError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError):
        fail()
