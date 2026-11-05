"""Bounded XDS avatar-line reply evidence; v1 label rule remains unchanged.

Thread parent time stays day precision. Canonical full parent time is attributed
only to the independently reopened parent permalink, never written into the day
observation. No official direct-parent or human-review claim is made.
"""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json
import math
import re
import sqlite3
import time
from pathlib import Path
from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .reply_evidence import ReplyEvidence,_time,_uuid
from .source_receipts import digest

SOURCE_VERSION='reply-machine-source-v2'
PACKET_VERSION='reply-thread-machine-observation-v2'
RULE='adjacent-list-avatar-connector-parent-day-reread-v2'


def _rect(value):
    if any(type(value[k]) not in (float,int) or not math.isfinite(value[k]) for k in ('x','y','width','height')):
        raise ValueError()
    if value['width']<=0 or value['height']<=0:raise ValueError()
    return value


def _inside(inner,outer,tolerance=0):
    _rect(inner);_rect(outer)
    return (inner['x']>=outer['x']-tolerance and inner['y']>=outer['y']-tolerance and
        inner['x']+inner['width']<=outer['x']+outer['width']+tolerance and
        inner['y']+inner['height']<=outer['y']+outer['height']+tolerance)


class ReplyEvidenceV2(ReplyEvidence):
    def _day_parent(self,discovery_id,item,*,now):
        try:
            value=json.loads(self._file(discovery_id,item).read_text('utf-8'))
            source=value['payload'];post=source['post'];author=source['author'];facts=value['facts'];cap=source['capture']
            day=post['published_day'];handle=author['username'];ident=post['id']
            if (value['version']!='reply-visible-post-day-v2' or value['role']!='parent_in_thread_day' or
                value['human_review'] is not False or value['official_api'] is not False or
                source['source_api']!='x-web-dom-day-v2' or 'created_at' in post or post['timestamp_precision']!='day' or
                not re.fullmatch('[0-9]{1,30}',ident) or not re.fullmatch('[A-Za-z0-9_]{1,15}',handle) or
                source['url']!=f'https://x.com/{handle}/status/{ident}' or not author['name'].strip() or
                not isinstance(post['text'],str) or not post['text'].strip() or len(post['text'])>20000 or
                day['precision']!='day' or facts['observed_published_day']!=day or facts['visible_timestamp']!=day['visible_text'] or
                facts['display_timezone']!='Asia/Shanghai' or facts['post_id']!=ident or facts['handle']!=handle or
                facts['display_name']!=author['name'] or facts['observed_text']!=post['text'] or
                any(type(facts[k]) is not int for k in ('body_count','media_count','quote_count','card_count')) or
                facts['body_count']!=1 or any(facts[k]!=0 for k in ('media_count','quote_count','card_count')) or
                facts['show_more'] is not False or facts['clipped'] is not False or
                cap['protocol']!='reply-thread-day-dom-v2' or cap['human_review'] is not False or
                cap['source_screenshot_reviewed'] is not False or cap['official_api_receipt'] is not False or
                cap['visibility_passed'] is not True or cap['screenshot_sha256']!=value['screenshot']['sha256'] or
                post.get('referenced_tweets') or source.get('media')):raise ValueError()
            match=re.fullmatch(r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日',day['visible_text'])
            if not match:raise ValueError()
            year=int(match[1]) if match[1] else None
            if (day['year']!=year or type(day['month']) is not int or type(day['day']) is not int or
                day['month']!=int(match[2]) or day['day']!=int(match[3])):raise ValueError()
            datetime(year or 2000,day['month'],day['day'])
            if any(not x.get('href') or '\u2026' in x.get('visible','') or x.get('visible','').endswith('...') for x in facts['body_links']):raise ValueError()
            self._file(discovery_id,value['screenshot']);self._visibility(discovery_id,value['visibility'])
            if _time(cap['captured_at'])>now+5:raise ValueError()
            _uuid(value['navigation_id'])
            return value
        except (ValueError,KeyError,TypeError,AttributeError,OSError):raise HubError('REPLY_V2_DAY_PARENT_OBSERVATION_INVALID') from None

    @staticmethod
    def _core(source):
        return {k:source['post'][k] for k in ('id','text')}|{'author':source['author']['name'],
            'handle':source['author']['username'],'url':source['url']}

    def validate_packet(self,discovery_id,packet,*,now=None):
        now=time.time() if now is None else now
        try:
            _uuid(discovery_id);_uuid(packet['capture_id'])
            if (packet['version']!=PACKET_VERSION or packet['rule']!=RULE or packet['observer_kind']!='server_rendered_dom' or
                packet['human_review'] is not False or packet['official_api'] is not False or
                packet.get('direct_parent_independently_verified') is True):raise ValueError()
            parent=self._day_parent(discovery_id,packet['parent_in_thread_day'],now=now)
            reply=self._observation(discovery_id,packet['reply_in_thread'],now=now)
            full=self._observation(discovery_id,packet['parent_reread'],now=now)
            p=parent['payload'];r=reply['payload'];f=full['payload'];pid=p['post']['id'];rid=r['post']['id'];thread=packet['thread']
            if (pid==rid or self._core(p)!=self._core(f) or reply['role']!='reply_in_thread' or full['role']!='parent_independent_reread' or
                parent['page_url']!=r['url'] or reply['page_url']!=r['url'] or full['page_url']!=p['url'] or
                parent['navigation_id']!=reply['navigation_id'] or full['navigation_id']==reply['navigation_id'] or
                _time(f['post']['created_at'])>_time(r['post']['created_at']) or
                _time(f['post']['created_at'])>_time(p['capture']['captured_at']) or
                _time(f['capture']['captured_at'])<max(_time(p['capture']['captured_at']),_time(r['capture']['captured_at'])) or
                f['post'].get('referenced_tweets') or f.get('media') or
                r['post'].get('referenced_tweets',[]) not in ([],[{'type':'replied_to','id':pid}])):raise ValueError()
            day=p['post']['published_day'];full_day=datetime.fromtimestamp(_time(f['post']['created_at']),timezone(timedelta(hours=8)))
            if (day['month']!=full_day.month or day['day']!=full_day.day or (day['year'] is not None and day['year']!=full_day.year)):
                raise HubError('REPLY_V2_PARENT_VISIBLE_DAY_MISMATCH')
            if (r['author']['username'] not in {s['x_handle'] for s in self.settings.config['sources']} or
                thread['page_url']!=r['url'] or thread['navigation_id']!=reply['navigation_id'] or
                thread['context_before_reply_ids']!=[pid] or thread['article_ids_through_reply']!=[pid,rid] or
                thread['collapsed_context_detected'] is not False or thread['show_more_context'] is not False or
                thread['parent_is_quote'] is not False):raise ValueError()
            label=thread['reply_to_label']
            if label is not None and (label['target_handles']!=[p['author']['username']] or label['visible'] is not True):raise ValueError()
            topology=thread['topology'];listing=topology['list'];pi=topology['parent_item'];ri=topology['reply_item'];line=topology['connector']
            if (listing['tag']!='UL' or listing['article_ids']!=[pid,rid] or listing['child_count']!=2 or
                pi['tag']!='LI' or ri['tag']!='LI' or pi['article_ids']!=[pid] or ri['article_ids']!=[rid] or
                topology['items_same_list'] is not True or topology['items_directly_adjacent'] is not True or
                topology['connector_candidate_count']!=1):raise ValueError()
            viewport=thread['viewport'];vp={'x':0,'y':0,'width':viewport['width'],'height':viewport['height']}
            if any(type(viewport[k]) is not int or not 1<=viewport[k]<=10000 for k in ('width','height')):raise ValueError()
            for box in (pi['rect'],ri['rect'],listing['rect'],line['rect']):
                if not _inside(box,vp):raise ValueError()
            if not _inside(pi['rect'],listing['rect'],1) or not _inside(ri['rect'],listing['rect'],1):raise ValueError()
            if abs(pi['rect']['y']+pi['rect']['height']-ri['rect']['y'])>1:raise ValueError()
            pa=topology['parent_avatar'];ra=topology['reply_avatar'];lr=_rect(line['rect'])
            for avatar,source,item in ((pa,p,pi),(ra,r,ri)):
                ar=_rect(avatar['rect'])
                if (avatar['loaded'] is not True or avatar['alt']!='@'+source['author']['username'] or
                    not re.match(r'^https://pbs\.twimg\.com/profile_images/',avatar['src']) or
                    not 10<=ar['width']<=80 or not 10<=ar['height']<=80 or not _inside(ar,item['rect'])):raise ValueError()
            color=re.fullmatch(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*(0?\.\d+|[01](?:\.0+)?))?\s*\)',line['background_color'])
            if not color:raise ValueError()
            red,green,blue=map(int,color.groups()[:3]);alpha=float(color[4] or 1)*line['opacity']
            grey=(red+green+blue)/3*alpha+255*(1-alpha)
            if (line['tag']!='SPAN' or line['inside_body'] is not False or line['owner_article_id']!=pid or
                line['owner_item_article_id']!=pid or line['avatar_column_contains_parent_avatar'] is not True or
                line['avatar_column_display']!='flex' or line['avatar_column_flex_direction']!='column' or
                not {'w-0.5','flex-1','rounded-t-full'}<=set(line['class_name'].split()) or
                type(line['opacity']) not in (int,float) or not math.isfinite(line['opacity']) or
                max(red,green,blue)>255 or max(red,green,blue)-min(red,green,blue)>2 or
                not .05<=alpha<=1 or not 80<=grey<=248 or not 1<=lr['width']<=4 or lr['height']<10 or
                not _inside(lr,pi['rect'],1)):raise ValueError()
            pc=pa['rect']['x']+pa['rect']['width']/2;rc=ra['rect']['x']+ra['rect']['width']/2;lc=lr['x']+lr['width']/2
            if (abs(pc-rc)>1 or abs(lc-pc)>1 or not 0<=lr['y']-(pa['rect']['y']+pa['rect']['height'])<=8 or
                abs(lr['y']+lr['height']-(pi['rect']['y']+pi['rect']['height']))>1 or
                not 0<=ra['rect']['y']-(lr['y']+lr['height'])<=24 or len(line['hit_points'])<3):raise ValueError()
            for point in line['hit_points']:
                if point['visible'] is not True or not lr['x']<=point['x']<=lr['x']+lr['width'] or not lr['y']<=point['y']<=lr['y']+lr['height']:raise ValueError()
            self._file(discovery_id,thread['screenshot'])
            return {'version':SOURCE_VERSION,'rule':RULE,'parent_source':f,'reply_source':r,'parent_post_id':pid,'reply_post_id':rid,
                'thread_parent_source':p,'thread_parent_observation_sha256':packet['parent_in_thread_day']['sha256'],
                'independent_parent_observation_sha256':packet['parent_reread']['sha256'],
                'canonical_parent_time':{'value':f['post']['created_at'],'source':'independent_parent_permalink',
                    'thread_time_precision':'day','thread_visible_day':day,'same_day_verified':True},
                'machine_relationship_supported':True,'human_review':False,'official_api':False,'automatic_publication_eligible':False,
                'limitations':['Bounded adjacent-list/avatar-connector evidence, not authoritative API direct-parent verification.',
                    'Thread day contains no inferred hour, minute or unobserved year.','No publication or rights approval is established.']}
        except (ValueError,KeyError,TypeError,AttributeError,OSError,OverflowError):raise HubError('REPLY_V2_TOPOLOGY_EVIDENCE_REQUIRED') from None

    def record(self,discovery,packet):
        checked=self.validate_packet(discovery['job_id'],packet)
        receipt_id=digest([SOURCE_VERSION,discovery['job_id'],packet]);folder=self.settings.root/'media'/discovery['job_id']/'reply-machine-v2'/packet['capture_id']
        if folder.is_symlink() or any(p.is_symlink() for p in folder.parents):raise HubError('REPLY_EVIDENCE_ARTIFACT_INVALID')
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        file=folder/'source-receipt-v2.json';self._write(file,encode({'version':SOURCE_VERSION,'receipt_id':receipt_id,
            'discovery_job_id':discovery['job_id'],'packet':packet,'validated_source':checked}).encode())
        try:
            with self.store.transaction() as db:
                parent=db.execute('SELECT * FROM jobs WHERE job_id=?',(discovery['job_id'],)).fetchone()
                if (not parent or parent['job_type']!='x_discovery' or parent['queue']!='tweet' or parent['state']!='processing' or
                    not parent['lease_owner'] or parent['lease_owner']!=discovery.get('lease_owner') or parent['lease_until']<=time.time()):
                    raise HubError('REPLY_DISCOVERY_LEASE_REQUIRED')
                db.execute('INSERT OR IGNORE INTO reply_machine_receipts VALUES(?,?,?,?,?,?,?,?,?)',
                    (receipt_id,discovery['job_id'],packet['capture_id'],checked['parent_post_id'],checked['reply_post_id'],digest(packet),str(file),sha256_file(file),time.time()))
                actual=db.execute('SELECT receipt_id FROM reply_machine_receipts WHERE capture_id=?',(packet['capture_id'],)).fetchone()
                if actual['receipt_id']!=receipt_id:raise HubError('REPLY_IMMUTABLE_RECEIPT_CONFLICT')
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (time.time(),'reply-machine-prototype','record_reply_evidence_v2',receipt_id,'bounded_avatar_connector_and_day_reread'))
        except sqlite3.OperationalError:raise HubError('REPLY_EVIDENCE_PROTOTYPE_NOT_INITIALIZED') from None
        return {'receipt_id':receipt_id,'file':str(file),'sha256':sha256_file(file),'automatic_publication_eligible':False}

    def verify(self,receipt_id):
        try:
            rows=self.store.query('SELECT * FROM reply_machine_receipts WHERE receipt_id=?',(receipt_id,))
            if not rows:raise HubError('REPLY_SOURCE_RECEIPT_MISSING')
            row=rows[0];file=self._file(row['discovery_job_id'],{'file':row['receipt_file'],'sha256':row['receipt_sha256']})
            value=json.loads(file.read_text('utf-8'));packet=value['packet'];checked=self.validate_packet(row['discovery_job_id'],packet)
            if (value['version']!=SOURCE_VERSION or value['receipt_id']!=receipt_id or value['discovery_job_id']!=row['discovery_job_id'] or
                packet['capture_id']!=row['capture_id'] or digest(packet)!=row['packet_sha256'] or
                digest([SOURCE_VERSION,row['discovery_job_id'],packet])!=receipt_id or checked!=value['validated_source'] or
                checked['parent_post_id']!=row['parent_post_id'] or checked['reply_post_id']!=row['reply_post_id']):raise ValueError()
            return value
        except sqlite3.OperationalError:raise HubError('REPLY_EVIDENCE_PROTOTYPE_NOT_INITIALIZED') from None
        except (ValueError,KeyError,TypeError,OSError):raise HubError('REPLY_SOURCE_RECEIPT_CHANGED') from None

    def model_inputs(self,receipt_id):
        checked=self.verify(receipt_id)['validated_source'];parent=deepcopy(checked['parent_source']);reply=deepcopy(checked['reply_source'])
        reply['post']['referenced_tweets']=[{'type':'replied_to','id':checked['parent_post_id']}]
        reply['reply_context']={'version':SOURCE_VERSION,'parent_source':deepcopy(parent),'machine_source_receipt_id':receipt_id,
            'machine_relationship_rule':RULE,'parent_time_basis':deepcopy(checked['canonical_parent_time']),
            'human_review':False,'relationship_verified_by_human':False,'official_api':False}
        return {'parent':parent,'reply':reply}
