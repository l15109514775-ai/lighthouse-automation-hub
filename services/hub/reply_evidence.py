"""Prototype paired-source contract, deliberately not wired to live publishers.

This records a bounded machine relationship rule, never human or official X
verification. A preceding article alone (reply-context-observation-v1) is not
sufficient. Browser collection of the new evidence is a separate integration.
"""
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
from .db import encode
from .errors import HubError
from .evidence import machine_evidence,sha256_file
from .model_contract import strict_json,validate_value
from .model_receipts import ModelReceipts
from .source_receipts import digest

SOURCE_VERSION='reply-machine-source-v1'
PACKET_VERSION='reply-thread-machine-observation-v1'
RULE='visible-reply-to-and-adjacent-parent-reread-v1'
CHECK_VERSION='reply-pair-translation-check-v1'


def _time(value):
    if not isinstance(value,str):raise ValueError()
    moment=datetime.fromisoformat(value.replace('Z','+00:00'))
    if moment.tzinfo is None:raise ValueError()
    return moment.timestamp()


def _uuid(value):
    if not isinstance(value,str) or str(uuid.UUID(value))!=value:raise ValueError()
    return value


class ReplyEvidence:
    def __init__(self,settings,store):self.settings,self.store=settings,store

    def initialize(self):
        # This prototype has no startup hook, scheduler handler or public API.
        file=Path(__file__).resolve().parents[2]/'scheduler/prototypes/reply_machine_v1.sql'
        db=self.store.connect()
        try:db.executescript(file.read_text('utf-8'))
        finally:db.close()

    def _file(self,discovery_id,item):
        try:
            root=self.settings.root/'media'/_uuid(discovery_id)
            file=Path(item['file'])
            if not file.is_absolute():file=root/file
            if (file.is_symlink() or any(p.is_symlink() for p in file.parents) or
                not file.resolve().is_relative_to(root.resolve()) or not file.is_file() or
                file.stat().st_size>40*1024*1024 or not re.fullmatch('[a-f0-9]{64}',item['sha256']) or
                sha256_file(file)!=item['sha256']):raise ValueError()
            return file.resolve()
        except (ValueError,KeyError,TypeError,AttributeError,OSError):
            raise HubError('REPLY_EVIDENCE_ARTIFACT_INVALID') from None

    def _visibility(self,discovery_id,item):
        try:
            value=json.loads(self._file(discovery_id,item).read_text('utf-8'))
            if (value['version']!='tweet-visibility-v2' or value['human_review'] is not False or
                value['used_ocr'] is not False or value['occluded'] is not False or
                value['clipped_text'] is not False or type(value['text_line_count']) is not int or
                value['text_line_count']<1 or not value['points'] or
                any(p.get('visible') is not True for p in value['points']) or
                any(p.get('pointerless_painted_cover') is True for p in value['fixed_intersections'])):raise ValueError()
            return value
        except (ValueError,KeyError,TypeError,OSError):raise HubError('REPLY_SOURCE_NOT_FULLY_VISIBLE') from None

    def _observation(self,discovery_id,item,*,now):
        try:
            value=json.loads(self._file(discovery_id,item).read_text('utf-8'))
            source=value['payload'];post=source['post'];author=source['author'];facts=value['facts'];cap=source['capture']
            ident=post['id'];handle=author['username']
            if (value['version']!='reply-visible-post-v1' or value['human_review'] is not False or
                value['official_api'] is not False or not re.fullmatch('[0-9]{1,30}',ident) or
                not re.fullmatch('[A-Za-z0-9_]{1,15}',handle) or not author['name'].strip() or
                source['url']!=f'https://x.com/{handle}/status/{ident}' or source['source_api']!='x-web-dom-v1' or
                not isinstance(post['text'],str) or not post['text'].strip() or len(post['text'])>20000 or
                facts['post_id']!=ident or facts['handle']!=handle or facts['display_name']!=author['name'] or
                facts['observed_text']!=post['text'] or facts['observed_published_at']!=post['created_at'] or
                any(type(facts[k]) is not int for k in ('body_count','media_count','quote_count','card_count')) or
                facts['body_count']!=1 or facts['show_more'] is not False or facts['clipped'] is not False or
                facts['media_count']!=0 or facts['quote_count']!=0 or facts['card_count']!=0 or
                cap['human_review'] is not False or cap['source_screenshot_reviewed'] is not False or
                cap['official_api_receipt'] is not False or cap['visibility_passed'] is not True or
                cap['screenshot_sha256']!=value['screenshot']['sha256']):raise ValueError()
            self._file(discovery_id,value['screenshot']);self._visibility(discovery_id,value['visibility'])
            captured=_time(cap['captured_at']);published=_time(post['created_at'])
            if not published<=captured<=now+5:raise ValueError()
            stamp=re.fullmatch(r'(\d{1,2}):(\d{2})\s*[·•]\s*(\d{4})年(\d{1,2})月(\d{1,2})日',facts['visible_timestamp'])
            if not stamp or facts['display_timezone']!='Asia/Shanghai':raise ValueError()
            hour,minute,year,month,day=map(int,stamp.groups())
            displayed=datetime(year,month,day,hour,minute,tzinfo=timezone(timedelta(hours=8))).timestamp()
            precision=post.get('timestamp_precision')
            if precision=='minute':
                if published!=displayed:raise ValueError()
            elif precision=='dom_datetime':
                if facts.get('datetime_attribute')!=post['created_at'] or not 0<=published-displayed<60:raise ValueError()
            else:raise ValueError()
            if any(not link.get('href') or '\u2026' in link.get('visible','') or
                link.get('visible','').endswith('...') for link in facts['body_links']):raise ValueError()
            if value['page_url']!=source['url'] and value.get('role')!='parent_in_thread':raise ValueError()
            _uuid(value['navigation_id'])
            return value
        except (ValueError,KeyError,TypeError,AttributeError,OSError):raise HubError('REPLY_POST_OBSERVATION_INVALID') from None

    @staticmethod
    def _identity(value):
        source=value['payload'];post=source['post'];author=source['author']
        return {'id':post['id'],'handle':author['username'],'name':author['name'],'text':post['text'],
                'published_at':_time(post['created_at']),'timestamp_precision':post.get('timestamp_precision'),
                'url':source['url']}

    def validate_packet(self,discovery_id,packet,*,now=None):
        now=time.time() if now is None else now
        try:
            _uuid(discovery_id);_uuid(packet['capture_id'])
            if (packet['version']!=PACKET_VERSION or packet['rule']!=RULE or
                packet['observer_kind']!='server_rendered_dom' or packet['human_review'] is not False or
                packet['official_api'] is not False):raise ValueError()
            parent=self._observation(discovery_id,packet['parent_in_thread'],now=now)
            reply=self._observation(discovery_id,packet['reply_in_thread'],now=now)
            reread=self._observation(discovery_id,packet['parent_reread'],now=now)
            p=self._identity(parent);r=self._identity(reply)
            if (p['id']==r['id'] or p!=self._identity(reread) or parent['role']!='parent_in_thread' or
                reply['role']!='reply_in_thread' or reread['role']!='parent_independent_reread' or
                reply['page_url']!=r['url'] or parent['page_url']!=r['url'] or reread['page_url']!=p['url'] or
                parent['navigation_id']!=reply['navigation_id'] or reread['navigation_id']==parent['navigation_id'] or
                _time(reread['payload']['capture']['captured_at'])<max(_time(parent['payload']['capture']['captured_at']),_time(reply['payload']['capture']['captured_at'])) or
                r['published_at']<p['published_at']):raise ValueError()
            thread=packet['thread'];label=thread['reply_to_label']
            if (thread['page_url']!=r['url'] or thread['navigation_id']!=reply['navigation_id'] or
                thread['context_before_reply_ids']!=[p['id']] or thread['article_ids_through_reply']!=[p['id'],r['id']] or
                type(thread['parent_index']) is not int or type(thread['reply_index']) is not int or
                thread['parent_index']!=0 or thread['reply_index']!=1 or
                thread['collapsed_context_detected'] is not False or thread['show_more_context'] is not False or
                thread['parent_is_quote'] is not False or label['article_id']!=r['id'] or label['visible'] is not True or
                label['target_handles']!=[p['handle']] or label['target_profile_urls']!=['https://x.com/'+p['handle']] or
                not re.match(r'^(?:回复|回覆|Replying to|返信先)\s*',label['text'],re.I) or
                re.findall(r'@([A-Za-z0-9_]+)',label['text'])!=[p['handle']]):raise ValueError()
            self._file(discovery_id,thread['screenshot'])
            viewport=thread['viewport'];width=viewport['width'];height=viewport['height']
            if type(width) is not int or type(height) is not int or not 1<=width<=4000 or not 1<=height<=10000:raise ValueError()
            for name in ('parent_rect','reply_rect','reply_to_label_rect'):
                rect=thread[name]
                if any(type(rect[k]) not in (float,int) or not math.isfinite(rect[k]) for k in ('x','y','width','height')):raise ValueError()
                if not (rect['x']>=0 and rect['y']>=0 and rect['width']>0 and rect['height']>0 and
                    rect['x']+rect['width']<=width and rect['y']+rect['height']<=height):raise ValueError()
            pr=thread['parent_rect'];rr=thread['reply_rect'];lr=thread['reply_to_label_rect']
            if (pr['y']+pr['height']>rr['y']+1 or lr['x']<rr['x'] or lr['y']<rr['y'] or
                lr['x']+lr['width']>rr['x']+rr['width'] or lr['y']+lr['height']>rr['y']+rr['height']):raise ValueError()
            for source in (parent['payload'],reread['payload']):
                if source['post'].get('referenced_tweets') or source.get('media'):raise ValueError()
            refs=reply['payload']['post'].get('referenced_tweets',[])
            if refs not in ([],[{'type':'replied_to','id':p['id']}]):raise ValueError()
            if reply['payload']['author']['username'] not in {s['x_handle'] for s in self.settings.config['sources']}:raise ValueError()
            return {'version':SOURCE_VERSION,'rule':RULE,'parent_source':reread['payload'],'reply_source':reply['payload'],
                'parent_post_id':p['id'],'reply_post_id':r['id'],'machine_relationship_supported':True,
                'human_review':False,'official_api':False,'automatic_publication_eligible':False,
                'reply_published_at':reply['payload']['post']['created_at'],
                'limitations':['A bounded rendered-DOM relationship rule, not official direct-parent verification.',
                    'No rights, translation quality, publication permission or hidden-content claim is established.']}
        except (ValueError,KeyError,TypeError,AttributeError,OSError):raise HubError('REPLY_MACHINE_RELATIONSHIP_EVIDENCE_REQUIRED') from None

    @staticmethod
    def _write(file,data):
        if file.exists():
            if file.is_symlink() or file.read_bytes()!=data:raise HubError('REPLY_IMMUTABLE_RECEIPT_CONFLICT')
            return
        temporary=file.with_suffix('.partial')
        with temporary.open('xb') as stream:
            os.chmod(temporary,0o600);stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,file)
        if os.name!='nt':
            descriptor=os.open(file.parent,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)

    def record(self,discovery,packet):
        checked=self.validate_packet(discovery['job_id'],packet)
        receipt_id=digest([SOURCE_VERSION,discovery['job_id'],packet]);folder=self.settings.root/'media'/discovery['job_id']/'reply-machine'/packet['capture_id']
        if folder.is_symlink() or any(p.is_symlink() for p in folder.parents):raise HubError('REPLY_EVIDENCE_ARTIFACT_INVALID')
        folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        file=folder/'source-receipt.json';value={'version':SOURCE_VERSION,'receipt_id':receipt_id,
            'discovery_job_id':discovery['job_id'],'packet':packet,'validated_source':checked}
        self._write(file,encode(value).encode())
        try:
            with self.store.transaction() as db:
                parent=db.execute('SELECT * FROM jobs WHERE job_id=?',(discovery['job_id'],)).fetchone()
                if (not parent or parent['job_type']!='x_discovery' or parent['queue']!='tweet' or parent['state']!='processing' or
                    not parent['lease_owner'] or parent['lease_owner']!=discovery.get('lease_owner') or parent['lease_until']<=time.time()):
                    raise HubError('REPLY_DISCOVERY_LEASE_REQUIRED')
                db.execute('INSERT OR IGNORE INTO reply_machine_receipts VALUES(?,?,?,?,?,?,?,?,?)',
                    (receipt_id,discovery['job_id'],packet['capture_id'],checked['parent_post_id'],checked['reply_post_id'],
                     digest(packet),str(file),sha256_file(file),time.time()))
                actual=db.execute('SELECT receipt_id FROM reply_machine_receipts WHERE capture_id=?',(packet['capture_id'],)).fetchone()
                if actual['receipt_id']!=receipt_id:raise HubError('REPLY_IMMUTABLE_RECEIPT_CONFLICT')
                db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (time.time(),'reply-machine-prototype','record_reply_evidence',receipt_id,'bounded_dom_rule_only'))
        except sqlite3.OperationalError:raise HubError('REPLY_EVIDENCE_PROTOTYPE_NOT_INITIALIZED') from None
        return {'receipt_id':receipt_id,'file':str(file),'sha256':sha256_file(file),'automatic_publication_eligible':False}

    def verify(self,receipt_id):
        try:
            rows=self.store.query('SELECT * FROM reply_machine_receipts WHERE receipt_id=?',(receipt_id,))
            if not rows:raise HubError('REPLY_SOURCE_RECEIPT_MISSING')
            row=rows[0];file=self._file(row['discovery_job_id'],{'file':row['receipt_file'],'sha256':row['receipt_sha256']})
            value=json.loads(file.read_text('utf-8'));packet=value['packet']
            checked=self.validate_packet(row['discovery_job_id'],packet)
            if (value['version']!=SOURCE_VERSION or value['receipt_id']!=receipt_id or
                value['discovery_job_id']!=row['discovery_job_id'] or packet['capture_id']!=row['capture_id'] or
                digest(packet)!=row['packet_sha256'] or digest([SOURCE_VERSION,row['discovery_job_id'],packet])!=receipt_id or
                checked!=value['validated_source'] or checked['parent_post_id']!=row['parent_post_id'] or
                checked['reply_post_id']!=row['reply_post_id']):raise ValueError()
            return value
        except sqlite3.OperationalError:raise HubError('REPLY_EVIDENCE_PROTOTYPE_NOT_INITIALIZED') from None
        except (OSError,ValueError,KeyError,TypeError):raise HubError('REPLY_SOURCE_RECEIPT_CHANGED') from None

    def model_inputs(self,receipt_id):
        value=self.verify(receipt_id)['validated_source'];parent=deepcopy(value['parent_source']);reply=deepcopy(value['reply_source'])
        reply['post']['referenced_tweets']=[{'type':'replied_to','id':value['parent_post_id']}]
        reply['reply_context']={'version':SOURCE_VERSION,'parent_source':deepcopy(parent),
            'machine_source_receipt_id':receipt_id,'machine_relationship_rule':RULE,'human_review':False,
            'relationship_verified_by_human':False,'official_api':False}
        return {'parent':parent,'reply':reply}

    def _translation_value(self,receipt_id,parent_job_id,reply_job_id,parent_translation,reply_translation):
        if any(not isinstance(s,str) or not s.strip() or len(s)>20000 for s in (parent_translation,reply_translation)):
            raise HubError('REPLY_BOTH_TRANSLATIONS_REQUIRED')
        inputs=self.model_inputs(receipt_id);jobs=[self.store.get(i) for i in (parent_job_id,reply_job_id)]
        if (parent_job_id==reply_job_id or any(not j for j in jobs) or
            any(j['source_platform']!='x' or j['source_id']!=inputs[name]['post']['id'] or j['payload']!=inputs[name]
                for name,j in zip(('parent','reply'),jobs))):raise HubError('REPLY_SEPARATE_TRANSLATION_JOBS_REQUIRED')
        results={};all_calls=[]
        for name,job,text in zip(('parent','reply'),jobs,(parent_translation,reply_translation)):
            proof=ModelReceipts(self.store).assess(job,text)
            deterministic=None
            if proof['proof_integrity_passed'] and proof['separate_requests_verified']:
                quality=self.store.query('SELECT response FROM model_requests WHERE request_key=?',(proof['request_keys'][1],))
                qa=validate_value(strict_json(quality[0]['response']),'quality')
                glossary={**self.settings.config.get('glossary',{}),**job['payload'].get('translation_glossary',{})}
                deterministic=machine_evidence(job['payload'],text,qa,glossary)
                all_calls.extend(proof['call_ids'])
            results[name]={'job_id':job['job_id'],'post_id':job['source_id'],'translation':text,
                'translation_sha256':hashlib.sha256(text.encode()).hexdigest(),
                'model_receipts':proof,'machine_checks':deterministic}
        integrity=len(all_calls)==4 and len(set(all_calls))==4 and all(v['model_receipts']['proof_integrity_passed'] for v in results.values())
        passed=integrity and all(v['machine_checks'] and v['machine_checks']['machine_checks_passed'] for v in results.values())
        live=integrity and all(v['model_receipts']['live_transport_verified'] for v in results.values())
        value={'version':CHECK_VERSION,'receipt_id':receipt_id,'pair':results,'four_separate_calls_verified':integrity,
            'translation_checks_passed':bool(passed),'live_transport_verified':bool(live),'human_review':False,
            'automatic_publication_eligible':False,'publisher_connected':False,
            'failures':([] if passed else ['REPLY_PAIR_TRANSLATION_CHECK_NOT_PASSED'])+([] if live else ['REPLY_MODEL_LIVE_TRANSPORT_REQUIRED'])}
        return value

    def assess_translations(self,receipt_id,parent_job_id,reply_job_id,parent_translation,reply_translation):
        value=self._translation_value(receipt_id,parent_job_id,reply_job_id,parent_translation,reply_translation)
        ident=digest(value);self.verify(receipt_id);base=Path(self.store.query('SELECT receipt_file FROM reply_machine_receipts WHERE receipt_id=?',(receipt_id,))[0]['receipt_file']).parent
        file=base/('translation-check-'+ident+'.json');self._write(file,encode(value).encode())
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO reply_machine_translation_checks VALUES(?,?,?,?,?,?,?)',
                (ident,receipt_id,parent_job_id,reply_job_id,str(file),sha256_file(file),time.time()))
        return {**value,'check_id':ident,'file':str(file),'sha256':sha256_file(file)}

    def verify_translation_check(self,check_id):
        try:
            rows=self.store.query('''SELECT c.*,r.discovery_job_id FROM reply_machine_translation_checks c
                JOIN reply_machine_receipts r ON r.receipt_id=c.receipt_id WHERE c.check_id=?''',(check_id,))
            if not rows:raise HubError('REPLY_TRANSLATION_CHECK_MISSING')
            row=rows[0];file=self._file(row['discovery_job_id'],{'file':row['result_file'],'sha256':row['result_sha256']})
            value=json.loads(file.read_text('utf-8'))
            if digest(value)!=check_id or value['version']!=CHECK_VERSION or value['receipt_id']!=row['receipt_id']:raise ValueError()
            current=self._translation_value(row['receipt_id'],row['parent_job_id'],row['reply_job_id'],
                value['pair']['parent']['translation'],value['pair']['reply']['translation'])
            if current!=value:raise ValueError()
            return value
        except sqlite3.OperationalError:raise HubError('REPLY_EVIDENCE_PROTOTYPE_NOT_INITIALIZED') from None
        except (OSError,ValueError,KeyError,TypeError):raise HubError('REPLY_TRANSLATION_CHECK_CHANGED') from None
