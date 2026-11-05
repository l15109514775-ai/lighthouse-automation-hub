"""Later ordinary-part evidence, with no local media or sending authority.

The raw manager row, public owner/description/player and historical one-use
ticket are independently checked. One P's BV never proves another P or the
whole replay. Historical v1 removes one exact source LF. Explicit v2 evidence
also permits the observed omission of expected U+2060, never actual-side trim.
"""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import math
import hashlib
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlsplit

from .errors import HubError
from .youtube_download import digest


def require(value):
    if not value:raise HubError('REPLAY_PART_PUBLIC_EVIDENCE_REQUIRED',review=True)


def finite(value):return type(value) in (int,float) and math.isfinite(value)


def title_display_matches(raw,expected):
    return isinstance(raw,str) and isinstance(expected,str) and raw in (expected,re.sub(' +',' ',expected))


def description_display_evidence(raw,expected,source,metadata_sha256,*,allow_ascii_space_collapse=False):
    """Independently derive only the four versioned exact display candidates."""
    if (not isinstance(raw,str) or not isinstance(expected,str) or len(raw)>8000 or len(expected)>2000 or
            not isinstance(source,str) or not re.fullmatch(r'https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{11}',source) or
            not isinstance(metadata_sha256,str) or not re.fullmatch('[a-f0-9]{64}',metadata_sha256)):
        return None
    prefix=source+'\n';display=expected.replace('\u2060','')
    variants=[('exact',expected,False,False),('source_prefix_once',prefix+expected,True,False)]
    if display!=expected:variants.extend([('word_joiners_omitted',display,False,True),
        ('source_prefix_once_word_joiners_omitted',prefix+display,True,True)])
    if allow_ascii_space_collapse:
        for name,text,prefixed,omitted in list(variants):
            collapsed=re.sub(' +',' ',text)
            if collapsed!=text:variants.append((name+'_ascii_spaces_collapsed',collapsed,prefixed,omitted,True))
    match=next((item for item in variants if item[1]==raw),None)
    if match is None:return None
    sha=lambda value:hashlib.sha256(value.encode('utf-8')).hexdigest()
    collapsed=len(match)==5
    return {'version':'bilibili-public-description-display-v3' if collapsed else 'bilibili-public-description-display-v2','candidate':match[0],'metadata_sha256':metadata_sha256,
        'expected_description_sha256':sha(expected),'observed_description_sha256':sha(raw),
        'expected_codepoints':len(expected),'observed_codepoints':len(raw),'source_prefix_removed':match[2],
        'omitted_word_joiner_positions':[i for i,c in enumerate(expected) if c=='\u2060'] if match[3] else [],
        **({'ascii_spaces_collapsed':True} if collapsed else {})}


def video_url(value,bv):
    u=urlsplit(value)
    return u.scheme=='https' and u.netloc=='www.bilibili.com' and u.path in ('/video/'+bv,'/video/'+bv+'/') and not u.query and not u.fragment


def manager_video_url(link,bv):
    # V5 manager attributes are really //www.bilibili.com/video/BV.../ .
    # Independently derive the canonical URL from the original exact href;
    # never accept a supplied canonical field as an authority by itself.
    raw=link.get('href')
    if not isinstance(raw,str):return False
    match=re.fullmatch(r'(?:https:)?//www\.bilibili\.com/video/(BV[A-Za-z0-9]{10})/?',raw)
    if match is None or match[1]!=bv:return False
    canonical='https://www.bilibili.com/video/'+bv+'/'
    if raw.startswith('//') and link.get('canonical_url')!=canonical:return False
    return link.get('canonical_url',canonical)==canonical


def validate_result(service,job,binding,state,observation,*,validation_time=None):
    # Imports are deferred to keep the service and pure validator acyclic.
    from .replay_parts_v5 import reference_body,native_surface
    from .replay_video_web import _timestamp
    root=service.settings.root;now=time.time() if validation_time is None else validation_time;base=service._native_base(job,binding,state)
    if validation_time is not None:require(finite(now) and 0<=now<=time.time())
    folder=root/'media'/job['job_id']/service.NATIVE_DIRECTORY/state['attempt_id']
    try:
        required=set(base)|{'version','external_id','url','state','public_accessible','public_lookup_observed_at',
            'native_result_evidence','origin','same_submit_attempt_observed','human_review','server_file_hash_verified'}
        require(set(observation)==required and observation['version']==service.RESULT_OBSERVATION_VERSION and
            all(observation[k]==v and type(observation[k]) is type(v) for k,v in base.items()) and
            observation['state']=='completed' and observation['public_accessible'] is True and
            observation['origin']=='later_native_lookup_reconciled_v5' and observation['same_submit_attempt_observed'] is False and
            observation['human_review'] is observation['server_file_hash_verified'] is False)
        bv=observation['external_id'];require(isinstance(bv,str) and re.fullmatch('BV[A-Za-z0-9]{10}',bv) and observation['url']=='https://www.bilibili.com/video/'+bv)
        previous=state.get('result_observation');require(not previous or previous['external_id']==bv)
        intent,intentref=service_read(service,folder.parent.parent/'replay-native-intents/public-submit.json')
        require(intent['version']=='replay-part-one-use-intent-v5' and intent['kind']=='public-submit' and all(intent.get(k)==v for k,v in base.items()))
        ticket=intent['reservation'];clicked=_timestamp(intent['created_at']);proof=state['machine_acceptance']
        require(ticket['version']==service.NATIVE_RESERVATION_VERSION and ticket['can_click_submit'] is True and ticket['state']=='dispatching' and
            all(ticket.get(k)==v for k,v in base.items()) and ticket['idempotency_key']==state['key'] and
            str(uuid.UUID(ticket['ticket_id']))==ticket['ticket_id'] and finite(ticket['issued_at']) and finite(ticket['expires_at']) and
            0<ticket['expires_at']-ticket['issued_at']<=300 and ticket['issued_at']<=clicked<ticket['expires_at'] and
            ticket['machine_evidence_sha256']==proof['evidence_sha256'])
        proofref={'file':proof['evidence_file'],'bytes':proof['evidence_bytes'],'sha256':proof['evidence_sha256']}
        original=reference_body(root,proofref,maximum=1_000_000)
        require(original=={k:v for k,v in proof.items() if k not in ('evidence_file','evidence_bytes','evidence_sha256')} and
            original['version']==service.MACHINE_PROOF_VERSION and original['job_id']==job['job_id'] and
            original['binding_sha256']==digest(binding) and original['policy_sha256']==binding['policy_sha256'] and
            all(original[k] is False for k in ('human_review','per_artifact_owner_acceptance','publication_authorized')) and _timestamp(original['created_at'])<=clicked)
        prep=state['native_preparation'];require(prep['version']==service.NATIVE_PREPARATION_VERSION and all(prep.get(k)==v for k,v in base.items()) and
            prep['private_draft_saved'] is prep['metadata_verified'] is prep['cover_applied'] is True and
            0<=clicked-_timestamp(prep['observed_at'])<=300 and
            prep['upload_reference_sha256']==ticket['upload_reference_sha256']==state['upload_observation']['upload_reference_sha256'])
        native_surface(binding,prep['native_surface'],metadata=True)
        inner=reference_body(root,prep['native_observation_file'],expected=folder/'native-preparation-observation.json')
        expected={k:deepcopy(v) for k,v in prep.items() if k not in ('native_observation_file','pre_submit_screenshot','private_draft_saved')};expected['version']=service.NATIVE_FORM_VERSION
        require(inner==expected)
        for field,key in (('title','title'),('description','description'),('category','category_label')):require(prep[field]==binding['metadata'][key])
        require(prep['source']==binding['source_url'] and prep['creation_statement']=='内容为转载' and set(prep['tags'])==set(binding['metadata']['tags']))
        require(prep['cover_sha256']==proof['checks']['cover']['output']['sha256'] and prep['cover_provenance_sha256']==proof['checks']['cover']['provenance']['sha256'])
        png(root,prep['pre_submit_screenshot'],folder/'pre-submit.png')
        evidence_ref=observation['native_result_evidence'];evpath=Path(evidence_ref['file'])
        require(evpath.name=='reconciliation.json' and evpath.parent.parent==folder/'later-lookups-v2' and str(uuid.UUID(evpath.parent.name))==evpath.parent.name)
        evidence=reference_body(root,evidence_ref);require(evidence['version']=='replay-later-native-result-observation-v2' and evidence['external_id']==bv)
        common(base,evidence,now,_timestamp,allow_seek=True)
        manager=reference_body(root,evidence['manager_observation'],expected=evpath.parent/'manager-row.json')
        common(base,manager,now,_timestamp)
        require(manager['version']=='replay-native-manager-row-v2')
        dom=manager['visible_dom'];require(dom['url']=='https://member.bilibili.com/platform/upload-manager/article' and dom['truncated'] is False and len(dom['rows'])<=60)
        text=dom['visible_text'];require(all(x in text for x in ('视频管理','图文管理','互动视频管理')) and len(re.findall(r'(?:^|\n)全部稿件\s+\d+(?:\n|$)',text))==1)
        matches=[r for r in dom['rows'] if any(title_display_matches(a.get('text'),binding['metadata']['title']) for a in r['title_links'])]
        require(len(matches)==1 and matches[0]==manager['selected_row']);row=matches[0]
        require({'article-card','clearfix','v2'}<=set(row['class'].split()) and len(row['title_links'])==len(row['covers'])==len(row['dates'])==len(row['durations'])==1)
        a,c=row['title_links'][0],row['covers'][0]
        require(a['tag']==c['tag']=='A' and a['visible'] is c['visible'] is True and {'name','ellipsis'}<=set(a['class'].split()) and
            'cover-wrp' in c['class'].split() and title_display_matches(a['text'],binding['metadata']['title']) and manager_video_url(a,bv) and manager_video_url(c,bv))
        date=row['dates'][0];match=re.fullmatch(r'(\d{4})年(\d{2})月(\d{2})日 (\d{2}):(\d{2}):(\d{2})',date['text'])
        require(match is not None and 'date' in date['class'].split() and 'is-success' in date['parent_class'].split())
        posted=datetime(*map(int,match.groups()),tzinfo=timezone(timedelta(hours=8))).timestamp()
        hms=row['durations'][0].split(':');require(len(hms) in (2,3) and all(re.fullmatch(r'\d{1,2}',v) for v in hms))
        nums=list(map(int,hms));require(all(v<60 for v in nums[1:]) and abs(sum(v*60**i for i,v in enumerate(reversed(nums)))-binding['duration_seconds'])<=2)
        public=reference_body(root,evidence['public_observation'],expected=evpath.parent/'public-native-inventory.json')
        common(base,public,now,_timestamp,allow_seek=True)
        require(public['version']=='replay-part-public-player-observation-v5' and public['external_id']==bv and public['expected_title']==binding['metadata']['title'] and
            observation['public_lookup_observed_at']==public['observed_at'] and
            math.floor(clicked)<=posted<=min(_timestamp(manager['observed_at']),_timestamp(public['observed_at']))+60)
        d=public['visible_dom'];require(video_url(d['url'],bv) and len(d['titles'])==1 and title_display_matches(d['titles'][0]['text'],binding['metadata']['title']))
        owners=[v for v in d['owner_candidates'] if v['visible'] is True and 'up-name' in v['class'].split()]
        require(len(owners)==1);owner=owners[0];u=urlsplit(owner['url'])
        require(u.scheme=='https' and u.netloc=='space.bilibili.com' and u.path.rstrip('/')=='/'+binding['site_uid'] and not u.query and not u.fragment and
            owner['text']==binding['account_name'] and owner['text'] in owner['card_text'])
        descriptions=[v for v in d['descriptions'] if v['visible'] is True and 'basic-desc-info' in v['class'].split()]
        require(len(descriptions)==1);raw=descriptions[0]['text'];prefix=binding['source_url']+'\n';expected=binding['metadata']['description']
        display=None
        if 'description_display_evidence' in public:
            display=description_display_evidence(raw,expected,binding['source_url'],binding['metadata_sha256'],
                allow_ascii_space_collapse=public['description_display_evidence'].get('version')=='bilibili-public-description-display-v3')
            require(display is not None and public['description_display_evidence']==display and
                all(type(public['description_display_evidence'][k]) is type(v) for k,v in display.items()))
        else:
            # Do not reinterpret historical v1 evidence using a later rule.
            require(raw==expected or raw==prefix+expected)
        videos=[v for v in d['videos'] if v['visible'] is True];require(len(videos)==1);v=videos[0]
        require(finite(v['duration_seconds']) and abs(v['duration_seconds']-binding['duration_seconds'])<=2 and
            finite(v['current_time']) and 0<v['current_time']<=v['duration_seconds']+1 and type(v['ready_state']) is int and 2<=v['ready_state']<=4 and
            v['error_code'] is None and type(v['video_width']) is int and v['video_width']>0 and type(v['video_height']) is int and v['video_height']>0 and
            isinstance(v['seekable_ranges'],list) and 1<=len(v['seekable_ranges'])<=100 and
            all(set(r)=={'start','end'} and finite(r['start']) and finite(r['end']) and 0<=r['start']<r['end']<=v['duration_seconds']+1 for r in v['seekable_ranges']))
        # The V5-only transport records an actual bounded seek and playback
        # observation. Setting currentTime alone is not proof of playability.
        play=public['near_end_playback'];require(public['player_seeks']==1 and play['seek_performed'] is True and
            play['seeking'] is False and v['seeking'] is False and type(play['ended']) is bool and v['ended'] is play['ended'] and finite(play['before']) and finite(play['after']) and
            0<=play['before']<=play['after']<=v['duration_seconds']+1 and play['after']>=max(0,v['duration_seconds']-5) and
            (play['after']-play['before']>.1 or play['ended'] is True) and abs(play['after']-v['current_time'])<=1)
        png(root,evidence['public_screenshot'],evpath.parent/'public-page.png')
        return {'version':'replay-part-public-coverage-v5',**base,'external_id':bv,'url':observation['url'],'duration_seconds':binding['duration_seconds'],
            'observed_at':public['observed_at'],'player':deepcopy(v),'near_end_playback':deepcopy(play),'native_result_evidence':evidence_ref,
            'manager_observation':evidence['manager_observation'],'public_observation':evidence['public_observation'],'public_screenshot':evidence['public_screenshot'],
            'original_public_submit':intentref,'source_packet_coverage':deepcopy(proof['checks']['source']['selected_packet_coverage']),
            'observed_description':raw,'normalized_description':expected,
            'description_normalization_version':display['version'] if display is not None else 'bilibili-exact-once-source-prefix-v1',
            'source_prefix_removed':display['source_prefix_removed'] if display is not None else raw!=expected,
            **({'description_display_evidence':deepcopy(display)} if display is not None else {}),
            'complete_part_playability_verified':True,'server_file_hash_verified':False,'human_review':False}
    except HubError:raise
    except (OSError,ValueError,KeyError,TypeError,AttributeError,OverflowError):require(False)


def common(base,value,now,timestamp,*,allow_seek=False):
    require(all(value.get(k)==v and type(value.get(k)) is type(v) for k,v in base.items()) and
        0<=now-timestamp(value['observed_at'])<=300 and value['source_or_media_modified'] is False and
        all(type(value.get(k)) is int and value[k]==0 for k in ('file_selections','save_clicks','submit_clicks')) and
        type(value['player_seeks']) is int and value['player_seeks'] in ((0,1) if allow_seek else (0,)))


def png(root,ref,path):
    from .replay_parts_v5 import reference_body
    raw=reference_body(root,ref,maximum=20_000_000,expected=path,json_body=False)
    require(len(raw)>=33 and raw[:16]==b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR' and
        0<int.from_bytes(raw[16:20],'big')<=10000 and 0<int.from_bytes(raw[20:24],'big')<=10000)


def service_read(service,path):
    from .replay_parts_v5 import document
    return document(service.settings.root,path)
