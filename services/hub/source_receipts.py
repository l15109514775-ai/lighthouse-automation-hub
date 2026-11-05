"""Independent API-origin evidence. Never attests human review, rights or publication."""
from datetime import datetime,timezone
from copy import deepcopy
import hashlib
import json
import os
import re
from pathlib import Path
import time
import uuid
import subprocess
from adapters.x import XOfficial,build_official_record,OFFICIAL_VERSION,OFFICIAL_RELATIONS_VERSION,PRICING_SOURCE,_author
from adapters.assets import download
from adapters.official_photo_url import official_photo_retrieval,validate_photo_retrieval
from .db import encode
from .errors import HubError
from .maintenance import Maintenance
from .locks import resource_guard
from .evidence import sha256_file

VERSION='source-receipt-v1'


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def read_body(data):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError()
            result[key]=value
        return result
    def invalid(_):raise ValueError()
    try:
        value=json.loads(data,object_pairs_hook=pairs,parse_constant=invalid)
        if not isinstance(value,dict):raise ValueError()
        return value
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise HubError('SOURCE_RESPONSE_INVALID',review=True) from None


class SourceReceipts:
    def __init__(self,settings,store):
        self.settings,self.store=settings,store

    def _official_parent_row(self,db,job):
        current=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
        if (not current or current['job_type']!='x_discovery' or current['queue']!='tweet' or
                current['state']!='processing' or not current['lease_owner'] or
                current['lease_owner']!=job.get('lease_owner') or (current['lease_until'] or 0)<=time.time()):
            raise HubError('SOURCE_CAPTURE_LEASE_REQUIRED',review=True)

    def _parent(self,job):
        db=self.store.connect()
        try:self._official_parent_row(db,job)
        finally:db.close()

    def _official_capture_owner(self,job):
        self._parent(job)
        return job['job_id']

    def _file(self,row):
        try:
            job_id=str(uuid.UUID(row['discovery_job_id']))
            call_id=str(uuid.UUID(row['call_id']))
        except (ValueError,TypeError,AttributeError):
            raise HubError('SOURCE_RECEIPT_ID_INVALID',review=True) from None
        media=self.settings.root/'media'
        file=media/job_id/'source-receipts'/(call_id+'.json')
        if (file.is_symlink() or any(p.is_symlink() for p in file.parents if p!=self.settings.root.parent) or
                not file.resolve().is_relative_to(media.resolve())):
            raise HubError('SOURCE_RECEIPT_PATH_INVALID',review=True)
        return file

    def _receipt(self,call_id):
        rows=self.store.query('''SELECT r.*,a.provider,a.job_id api_job_id,a.state api_state,a.finished_at api_finished_at
            FROM source_http_receipts r JOIN api_calls a ON a.call_id=r.call_id WHERE r.call_id=?''',(call_id,))
        if (not rows or rows[0]['provider']!='x' or rows[0]['api_state']!='done' or
                rows[0]['api_job_id']!=rows[0]['discovery_job_id'] or rows[0]['captured_at']!=rows[0]['api_finished_at']):
            raise HubError('SOURCE_API_CALL_NOT_CONFIRMED',review=True)
        return rows[0]

    def record(self,job,snapshot):
        self._parent(job)
        row=self._receipt(snapshot['call_id'])
        raw=snapshot.get('raw_body')
        if (row['discovery_job_id']!=job['job_id'] or not isinstance(raw,bytes) or len(raw)>5*1024*1024 or
                hashlib.sha256(raw).hexdigest()!=row['raw_sha256'] or digest(read_body(raw))!=row['canonical_sha256']):
            raise HubError('SOURCE_RESPONSE_INTEGRITY_CHANGED',review=True)
        # Response archives contain public source bodies only, never request headers or secret values.
        for key in ('X_BEARER_TOKEN','MODEL_API_KEY','OPENAI_API_KEY','YOUTUBE_API_KEY',
                    'HUB_READ_TOKEN','HUB_MAINTAIN_TOKEN','EVENT_ENCRYPTION_KEY'):
            secret=os.getenv(key,'')
            if len(secret)>16 and secret.encode() in raw:
                raise HubError('SOURCE_RESPONSE_CONTAINS_SECRET',review=True)
        file=self._file(row)
        Maintenance(self.settings,self.store).check_disk(reserve_bytes=len(raw))
        file.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if file.exists():
            if hashlib.sha256(file.read_bytes()).hexdigest()!=row['raw_sha256']:
                raise HubError('SOURCE_RESPONSE_INTEGRITY_CHANGED',review=True)
        else:
            temporary=file.with_suffix('.partial')
            with temporary.open('xb') as stream:
                os.chmod(temporary,0o600)
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,file)
            if os.name!='nt':
                descriptor=os.open(file.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
        if re.fullmatch(r'/2/users/by/username/[A-Za-z0-9_]{1,15}',row['path']):
            body=read_body(raw)
            try:
                handle=row['path'].rsplit('/',1)[1];author=_author(body.get('data'),handle)
                if not body.get('errors'):
                    with self.store.transaction() as db:
                        db.execute('INSERT OR REPLACE INTO source_cursors VALUES(?,?)',('x-official-author:'+handle.lower(),encode({
                            'version':'x-official-author-cache-v1','user_call_id':row['call_id'],'author_id':author['id']})))
            except HubError:pass
        from .x_cost_ledger import confirm_saved_receipt
        confirm_saved_receipt(self.settings.root,self.store,row['call_id'])
        return row['call_id']

    def cached_author(self,handle,*,require_live=True):
        rows=self.store.query('SELECT value FROM source_cursors WHERE key=?',('x-official-author:'+handle.lower(),))
        if not rows:return None
        try:
            item=json.loads(rows[0]['value'])
            if item['version']!='x-official-author-cache-v1':return None
            row=self._receipt(item['user_call_id']);body=self._body(row)
            if (row['path'].lower()!='/2/users/by/username/'+handle.lower() or body.get('errors') or
                    not 0<=time.time()-row['captured_at']<=min(86400,self.settings.config.get('source_receipt_max_age_seconds',86400)) or
                    require_live and row['transport_mode']!='default_tls'):return None
            author=_author(body.get('data'),handle)
            if author['id']!=item['author_id']:return None
            return {'version':'x-official-author-cache-v1','user_call_id':row['call_id'],'author':author,
                'captured_at':row['captured_at'],'transport_mode':row['transport_mode']}
        except (HubError,ValueError,KeyError,TypeError):return None

    def _official_record(self,payload,*,discovery_id=None,require_fresh=True):
        try:
            official=payload['official_source'];refs=official['receipt_refs'];handle=payload['author']['username'];pid=payload['post']['id']
            version=official['version']
            if (version not in (OFFICIAL_VERSION,OFFICIAL_RELATIONS_VERSION) or handle.lower() not in {s['x_handle'].lower() for s in self.settings.config['sources']} or
                    set(refs)!={'user_call_id','timeline_call_id','detail_call_ids'} or not isinstance(refs['detail_call_ids'],list) or
                    len(refs['detail_call_ids'])>(2 if version==OFFICIAL_RELATIONS_VERSION else 1)):raise ValueError()
            ids=[refs['user_call_id'],refs['timeline_call_id'],*refs['detail_call_ids']]
            if len(ids)!=len(set(ids)) or any(str(uuid.UUID(v))!=v for v in ids):raise ValueError()
            rows={ident:self._receipt(ident) for ident in ids};bodies={ident:self._body(row) for ident,row in rows.items()}
            user,page=rows[refs['user_call_id']],rows[refs['timeline_call_id']]
            actual_discovery=page['discovery_job_id']
            if discovery_id is not None and actual_discovery!=discovery_id:raise ValueError()
            if any(row['method']!='GET' or row['host']!='api.x.com' for row in rows.values()):raise ValueError()
            if user['path'].lower()!='/2/users/by/username/'+handle.lower():raise ValueError()
            author=_author(bodies[user['call_id']].get('data'),handle)
            if page['path']!='/2/users/'+author['id']+'/tweets':raise ValueError()
            required={'author_id','created_at','attachments','referenced_tweets','note_tweet','edit_history_tweet_ids'}
            for row in [page,*[rows[ident] for ident in refs['detail_call_ids']]]:
                params=json.loads(row['params'])
                if not required<=set(params.get('tweet.fields','').split(',')):raise ValueError()
                if row!=page and (row['discovery_job_id']!=actual_discovery or not re.fullmatch(r'/2/tweets/[0-9]{1,30}',row['path'])):raise ValueError()
            now=time.time();maximum=self.settings.config.get('source_receipt_max_age_seconds',86400)
            if any(row['captured_at']>now+5 or require_fresh and now-row['captured_at']>maximum for row in rows.values()):
                raise HubError('SOURCE_RECEIPT_EXPIRED_OR_FUTURE')
            for ident in refs['detail_call_ids']:
                if rows[ident]['path']!='/2/tweets/'+str(bodies[ident].get('data',{}).get('id')):raise ValueError()
            metadata={ident:{'captured_at':row['captured_at'],'transport_mode':row['transport_mode']} for ident,row in rows.items()}
            materials={'post':official.get('local_media',[])}
            if version==OFFICIAL_RELATIONS_VERSION:
                relations=payload['reference_context']['relations']
                if (not isinstance(relations,list) or len(relations)>2 or
                        any(r.get('role') not in ('parent','quote') for r in relations) or
                        len({r['role'] for r in relations})!=len(relations)):raise ValueError()
                for relation in relations:materials[relation['role']]=relation['source']['official_source'].get('local_media',[])
            elif payload.get('reply_context'):materials['parent']=payload['reply_context']['parent_source']['official_source'].get('local_media',[])
            rebuilt=build_official_record(handle,pid,bodies[user['call_id']],bodies[page['call_id']],
                {ident:bodies[ident] for ident in refs['detail_call_ids']},refs,metadata,materials,source_version=version)
            if rebuilt['payload']!=payload:raise HubError('SOURCE_NORMALIZED_INPUT_MISMATCH')
            return rebuilt,rows,actual_discovery
        except (KeyError,TypeError,ValueError,AttributeError):raise HubError('X_OFFICIAL_SOURCE_DOCUMENT_INVALID') from None

    def _official_media_file(self,discovery_id,value):
        try:
            root=self.settings.root/'media'/discovery_id;file=Path(value)
            if (not file.is_absolute() or file.is_symlink() or any(p.is_symlink() for p in file.parents) or
                    not file.resolve().is_relative_to(root.resolve()) or not file.is_file() or not 0<file.stat().st_size<=20000000):raise ValueError()
            return file.resolve()
        except (TypeError,ValueError,OSError):raise HubError('X_OFFICIAL_MEDIA_FILE_INVALID') from None

    def _validate_photo(self,discovery_id,file,photo):
        file=self._official_media_file(discovery_id,file)
        script=Path(__file__).resolve().parents[2]/'adapters/verify_official_media.mjs'
        env={k:os.environ[k] for k in ('PATH','TEMP','TMP','TMPDIR','SYSTEMROOT','WINDIR') if k in os.environ}
        try:
            proc=subprocess.run(['node',str(script),str(file),str(self.settings.root/'media'/discovery_id),
                str(photo['width']),str(photo['height']),photo['format']],env=env,capture_output=True,timeout=45)
            result=json.loads(proc.stdout)
            if proc.returncode:
                code=result.get('error_code');raise HubError(code if isinstance(code,str) and re.fullmatch('[A-Z_]+',code) else 'X_OFFICIAL_MEDIA_DECODE_FAILED')
            if (result.get('version')!='x-official-photo-validation-v1' or result.get('decoded_complete') is not True or
                    result.get('animated') is not False or result.get('width')!=photo['width'] or result.get('height')!=photo['height'] or
                    result.get('format')!=photo['format'] or result.get('sha256')!=sha256_file(file)):raise ValueError()
            return result
        except (OSError,subprocess.SubprocessError,ValueError,TypeError,KeyError):raise HubError('X_OFFICIAL_MEDIA_VALIDATOR_FAILED') from None

    def _official_publication_window(self,discovery,payload,not_before=None):
        """Normal intake keeps the owner age limit; exceptional ops must bind their own grant."""
        try:published=datetime.fromisoformat(payload['post']['created_at'].replace('Z','+00:00')).timestamp()
        except (ValueError,KeyError,TypeError):raise HubError('X_PUBLICATION_TIME_INVALID') from None
        if published<max(time.time()-7200,not_before or 0):raise HubError('OWNER_TWO_HOUR_WINDOW_EXPIRED')

    def _official_initial_job_state(self,discovery,payload):
        """Trusted scoped intake may hold V2 work atomically; normal discovery queues it."""
        return 'pending'

    def _official_gate(self,discovery,payload,policy,db,not_before=None):
        self._official_parent_row(db,discovery)
        self._official_publication_window(discovery,payload,not_before)
        pid=payload['post']['id']
        if db.execute("SELECT 1 FROM source_tombstones WHERE source_platform='x' AND source_id=?",(pid,)).fetchone():raise HubError('OWNER_BACKLOG_CLEARED')
        old=db.execute("SELECT job_id,state,external_id,external_url FROM publications WHERE platform='bilibili' AND source_platform='x' AND source_id=? AND account_id=?",
            (pid,'site_uid:'+self.settings.config['target_bilibili_site_uid'])).fetchone()
        if old:return {'job_id':old['job_id'],'created':False,'binding_present':False,'publication_state':old['state'],
            'already_published':old['state']=='completed','url':old['external_url']}
        old=db.execute("SELECT job_id FROM jobs WHERE source_platform='x' AND source_id=? AND job_type='tweet_process' AND policy_version=?",(pid,policy)).fetchone()
        if not old and policy.split(':',1)[0]=='machine-tweet-v4-api-relations':
            # Changing source scope is not permission to replay an existing
            # official-source job under a new policy/version.
            old=db.execute("""SELECT j.job_id FROM jobs j WHERE j.source_platform='x' AND j.source_id=?
                AND j.job_type='tweet_process' AND EXISTS(SELECT 1 FROM tweet_source_bindings b WHERE b.job_id=j.job_id)
                ORDER BY j.created_at,j.job_id LIMIT 1""",(pid,)).fetchone()
        if old:return {'job_id':old['job_id'],'created':False,'binding_present':bool(db.execute('SELECT 1 FROM tweet_source_bindings WHERE job_id=?',(old['job_id'],)).fetchone())}
        return None

    def ingest_official(self,discovery,record,policy,*,not_before=None,downloader=None):
        version=record.get('version')
        if version not in (OFFICIAL_VERSION,OFFICIAL_RELATIONS_VERSION):raise HubError('X_OFFICIAL_SOURCE_VERSION_INVALID')
        expected='machine-tweet-v4-api-relations' if version==OFFICIAL_RELATIONS_VERSION else 'machine-tweet-v3-api'
        if not isinstance(policy,str) or not re.fullmatch(re.escape(expected)+r'(?::source-[1-9][0-9]{0,3})?',policy):raise HubError('X_OFFICIAL_POLICY_INVALID')
        payload=record['payload'];rebuilt,rows,discovery_id=self._official_record(payload,discovery_id=self._official_capture_owner(discovery))
        if rebuilt['version']!=version:raise HubError('X_OFFICIAL_SOURCE_VERSION_INVALID')
        if any(node['source']['official_source'].get('local_media') for node in rebuilt['nodes']):raise HubError('X_OFFICIAL_PREBOUND_MEDIA_NOT_ALLOWED')
        with self.store.transaction() as db:
            existing=self._official_gate(discovery,payload,policy,db,not_before)
            if existing:return existing
        materials={};fetch=downloader or download;mode='default_tls' if downloader is None else 'custom_transport'
        from .web_source import WebSources
        with resource_guard(self.settings.root/'data/locks/heavy'):
            for node in rebuilt['nodes']:
                items=[]
                for photo in node['media']:
                    def check_disk(reserve=0):
                        self._parent(discovery)
                        self._official_publication_window(discovery,payload,not_before)
                        Maintenance(self.settings,self.store).check_disk(reserve_bytes=reserve)
                    check_disk(20000000)
                    folder=self.settings.root/'media'/discovery_id/'official-media'/str(uuid.uuid4());folder.mkdir(mode=0o700,parents=True)
                    file=folder/f"photo-{node['role']}-{photo['index']}.{photo['format']}"
                    retrieval=official_photo_retrieval(photo['source_url'])
                    result=fetch(retrieval['download_url'],file,allowed_hosts=['pbs.twimg.com'],max_bytes=20000000,check_disk=check_disk)
                    decoded=self._validate_photo(discovery_id,result['file'],photo);check_disk()
                    if result['sha256']!=decoded['sha256']:raise HubError('X_OFFICIAL_MEDIA_HASH_CHANGED')
                    with file.open('r+b' if os.name=='nt' else 'rb') as stream:os.fsync(stream.fileno())
                    WebSources._sync_dir(folder);WebSources._sync_dir(folder.parent)
                    items.append({'index':photo['index'],'file':str(file.resolve()),'sha256':decoded['sha256'],'format':decoded['format'],
                        'width':decoded['width'],'height':decoded['height'],'completeness':'complete','media_key':photo['media_key'],
                        'source_url':photo['source_url'],'transport_mode':mode,**retrieval})
                materials[node['role']]=items
        refs=rebuilt['receipt_refs'];page=rows[refs['timeline_call_id']];user=rows[refs['user_call_id']]
        bodies={ident:self._body(row) for ident,row in rows.items()};metadata={ident:{'captured_at':row['captured_at'],'transport_mode':row['transport_mode']} for ident,row in rows.items()}
        rebuilt=build_official_record(payload['author']['username'],payload['post']['id'],bodies[user['call_id']],bodies[page['call_id']],
            {ident:bodies[ident] for ident in refs['detail_call_ids']},refs,metadata,materials,source_version=version);payload=rebuilt['payload']
        ident=str(uuid.uuid4());now=time.time();binding=digest([ident,user['call_id'],page['call_id'],digest(payload)])
        with self.store.transaction() as db:
            existing=self._official_gate(discovery,payload,policy,db,not_before)
            if existing:return existing
            self._official_record(payload,discovery_id=discovery_id)
            initial=self._official_initial_job_state(discovery,payload)
            if initial not in ('pending','upload_pending') or initial!='pending' and version!=OFFICIAL_RELATIONS_VERSION:
                raise HubError('X_OFFICIAL_INITIAL_STATE_INVALID')
            db.execute("INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload) VALUES(?,'x',?,'tweet_process','tweet',?,?,?,?,?)",
                (ident,payload['post']['id'],initial,now,now,policy,encode(payload)))
            db.execute('INSERT INTO tweet_source_bindings VALUES(?,?,?,?,?,?,?)',(binding,ident,discovery_id,user['call_id'],page['call_id'],digest(payload),now))
        return {'job_id':ident,'created':True,'binding_present':True}

    def assess_official(self,job,require_fresh=True,*,source_version=OFFICIAL_VERSION):
        bindings=self.store.query('SELECT * FROM tweet_source_bindings WHERE job_id=? ORDER BY bound_at DESC LIMIT 1',(job['job_id'],))
        if not bindings:raise HubError('SOURCE_RECEIPT_MISSING')
        binding=bindings[0];current=self.store.get(job['job_id'])
        if (not current or current['payload']!=job['payload'] or current['source_platform']!='x' or current['job_type']!='tweet_process' or
                current['source_id']!=job['source_id'] or current['source_id']!=job['payload']['post']['id'] or
                digest(job['payload'])!=binding['payload_sha256']):raise HubError('SOURCE_JOB_INPUT_CHANGED')
        rebuilt,rows,discovery_id=self._official_record(job['payload'],discovery_id=binding['discovery_job_id'],require_fresh=require_fresh)
        if rebuilt['version']!=source_version:raise HubError('X_OFFICIAL_SOURCE_VERSION_INVALID')
        refs=rebuilt['receipt_refs']
        if binding['user_call_id']!=refs['user_call_id'] or binding['timeline_call_id']!=refs['timeline_call_id']:raise HubError('SOURCE_RECEIPT_JOB_MISMATCH')
        from .web_source import WebSources
        result_nodes=[];all_live=all(r['transport_mode']=='default_tls' for r in rows.values())
        for node in rebuilt['nodes']:
            provenance=[];api_calls=[];photos={m['media_key']:m for m in node['media']};local=node['source']['official_source']['local_media']
            if not isinstance(local,list) or len(local)!=len(photos):raise HubError('X_OFFICIAL_MEDIA_BINDING_MISSING')
            for item in local:
                photo=photos.get(item.get('media_key'))
                if (not photo or any(item.get(key)!=photo[key] for key in ('index','source_url','format','width','height')) or
                        item.get('completeness')!='complete' or item.get('transport_mode') not in ('default_tls','custom_transport')):
                    raise HubError('X_OFFICIAL_MEDIA_BINDING_CHANGED')
                validate_photo_retrieval(item,photo['source_url'])
                checked=self._validate_photo(discovery_id,item['file'],photo)
                if checked['sha256']!=item['sha256']:raise HubError('X_OFFICIAL_MEDIA_HASH_CHANGED')
                provenance.append({'file':item['file'],'sha256':item['sha256']});all_live=all_live and item['transport_mode']=='default_tls'
            for call_id in node['receipt_ids']:
                row=rows[call_id];raw=self._file(row)
                response={'file':str(raw.resolve()),'sha256':row['raw_sha256']};provenance.append(response)
                api_calls.append({'call_id':call_id,'method':row['method'],'host':row['host'],'path':row['path'],'params':json.loads(row['params']),
                    'http_status_class':'2xx','transport_mode':row['transport_mode'],'captured_at':row['captured_at'],'response':response,
                    'canonical_response_sha256':row['canonical_sha256']})
            authority={'version':'x-official-render-authority-v1','source_sha256':digest(node['source']),'post_id':node['source']['post']['id'],
                'author_id':node['source']['author']['id'],'api_calls':api_calls,'human_review':False,'publication_authorized':False,
                'pricing_source':deepcopy(PRICING_SOURCE)}
            folder=self.settings.root/'media'/job['job_id'];folder.mkdir(mode=0o700,exist_ok=True)
            file=folder/f"official-api-authority-{node['role']}.json";encoded=encode(authority).encode()
            if file.exists():
                if file.is_symlink() or file.read_bytes()!=encoded:raise HubError('X_OFFICIAL_AUTHORITY_CHANGED')
            else:WebSources._write_durable(file,encoded);WebSources._sync_dir(folder)
            descriptor={'file':str(file),'sha256':hashlib.sha256(encoded).hexdigest()};provenance.append(descriptor)
            result_nodes.append({'role':node['role'],'source':node['source'],'media':local,'provenance':provenance,'api_authority':descriptor})
        proof_version='x-official-source-proof-v2' if rebuilt['version']==OFFICIAL_RELATIONS_VERSION else 'x-official-source-proof-v1'
        return {'proof':{'version':proof_version,'capture_integrity_passed':True,'source_live_verified':all_live,
            'payload_sha256':digest(job['payload']),'binding_id':binding['binding_id'],'discovery_job_id':discovery_id,
            'receipt_ids':list(rows),'human_review':False,'automatic_publication_eligible':False,'pricing_source':deepcopy(PRICING_SOURCE)},'nodes':result_nodes}

    def _body(self,row):
        file=self._file(row)
        if not file.is_file() or file.stat().st_size>5*1024*1024:
            raise HubError('SOURCE_RESPONSE_FILE_MISSING',review=True)
        data=file.read_bytes()
        if hashlib.sha256(data).hexdigest()!=row['raw_sha256']:
            raise HubError('SOURCE_RESPONSE_INTEGRITY_CHANGED',review=True)
        value=read_body(data)
        if digest(value)!=row['canonical_sha256']:
            raise HubError('SOURCE_RESPONSE_INTEGRITY_CHANGED',review=True)
        return value

    def bind(self,discovery,child_id,payload,references):
        self._parent(discovery)
        child=self.store.get(child_id)
        if (not child or child['job_type']!='tweet_process' or child['source_platform']!='x' or
                child['source_id']!=payload['post']['id'] or child['payload']!=payload):
            self.store.event('review.required',{'job_id':child_id,'code':'SOURCE_EXISTING_JOB_INPUT_DIFFERS'},
                             dedup_key='source-binding:'+child_id)
            return None
        user=self._receipt(references['user_call_id']);timeline=self._receipt(references['timeline_call_id'])
        if any(row['discovery_job_id']!=discovery['job_id'] for row in (user,timeline)):
            raise HubError('SOURCE_RECEIPT_JOB_MISMATCH',review=True)
        self._body(user);self._body(timeline)
        ident=digest([child_id,user['call_id'],timeline['call_id'],digest(payload)])
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO tweet_source_bindings VALUES(?,?,?,?,?,?,?)',
                (ident,child_id,discovery['job_id'],user['call_id'],timeline['call_id'],digest(payload),time.time()))
        return ident

    def assess(self,job):
        result={'version':VERSION,'capture_integrity_passed':False,'source_live_verified':False,
            'human_review':False,'auto_publish_eligible':False,'failures':[],
            'does_not_verify':['copyright_permission','semantic_translation_quality','original_screenshot',
                               'material_image_completeness','referenced_post_completeness','human_review','publication_authorization']}
        try:
            bindings=self.store.query('''SELECT * FROM tweet_source_bindings WHERE job_id=?
                ORDER BY bound_at DESC,binding_id DESC LIMIT 1''',(job['job_id'],))
            if not bindings:raise HubError('SOURCE_RECEIPT_MISSING',review=True)
            binding=bindings[0]
            current=self.store.get(job['job_id'])
            if (not current or current['payload']!=job['payload'] or current['source_id']!=job['source_id'] or
                    digest(current['payload'])!=binding['payload_sha256']):
                raise HubError('SOURCE_JOB_INPUT_CHANGED',review=True)
            user=self._receipt(binding['user_call_id']);page=self._receipt(binding['timeline_call_id'])
            if any(row['discovery_job_id']!=binding['discovery_job_id'] for row in (user,page)):
                raise HubError('SOURCE_RECEIPT_JOB_MISMATCH',review=True)
            now=time.time();maximum=self.settings.config.get('source_receipt_max_age_seconds',86400)
            if any(not 0<=now-row['captured_at']<=maximum for row in (user,page)):
                raise HubError('SOURCE_RECEIPT_EXPIRED_OR_FUTURE',review=True)
            user_body=self._body(user);timeline=self._body(page)
            author=user_body.get('data')
            if (not isinstance(author,dict) or not isinstance(author.get('username'),str) or
                    not isinstance(author.get('name'),str) or not author['name'].strip()):
                raise HubError('SOURCE_AUTHOR_NOT_VERIFIED',review=True)
            handle=author['username']
            if handle.lower() not in {s['x_handle'].lower() for s in self.settings.config['sources']}:
                raise HubError('SOURCE_AUTHOR_NOT_AUTHORIZED',review=True)
            if user['path'].lower()!='/2/users/by/username/'+handle.lower() or page['path']!='/2/users/'+str(author.get('id'))+'/tweets':
                raise HubError('SOURCE_ENDPOINT_IDENTITY_MISMATCH',review=True)
            requested=set(json.loads(page['params']).get('tweet.fields','').split(','))
            user_fields=set(json.loads(user['params']).get('user.fields','').split(','))
            if (not {'author_id','created_at','attachments','referenced_tweets','note_tweet','edit_history_tweet_ids'}<=requested or
                    not {'name','username','profile_image_url'}<=user_fields):
                raise HubError('SOURCE_REQUESTED_FIELDS_INCOMPLETE',review=True)
            if user_body.get('errors') or timeline.get('errors'):
                raise HubError('SOURCE_UPSTREAM_PARTIAL_ERRORS',review=True)
            posts=[post for post in timeline.get('data',[]) if isinstance(post,dict) and post.get('id')==current['source_id']]
            if len(posts)!=1:raise HubError('SOURCE_POST_NOT_UNIQUE',review=True)
            expected=XOfficial.normalize(posts[0],author,timeline,handle)
            if expected!=current['payload']:
                raise HubError('SOURCE_NORMALIZED_INPUT_MISMATCH',review=True)
            post=expected['post']
            try:
                published=datetime.fromisoformat(post['created_at'].replace('Z','+00:00'))
                if published.tzinfo is None or not 2000<=published.year<=2100 or published.timestamp()>min(user['captured_at'],page['captured_at'])+300:
                    raise ValueError()
            except (KeyError,ValueError,TypeError,AttributeError):
                raise HubError('SOURCE_PUBLICATION_TIME_INVALID',review=True) from None
            refs=post.get('referenced_tweets',[]);attachments=post.get('attachments',{})
            if (not isinstance(refs,list) or not isinstance(attachments,dict) or
                    any(not isinstance(ref,dict) or ref.get('type') not in ('quoted','replied_to','retweeted') or
                        not isinstance(ref.get('id'),str) or not re.fullmatch(r'[0-9]{1,30}',ref['id']) for ref in refs) or
                    not isinstance(attachments.get('media_keys',[]),list) or
                    any(not isinstance(key,str) or not key for key in attachments.get('media_keys',[]))):
                raise HubError('SOURCE_RELATIONSHIP_OR_MEDIA_INVALID',review=True)
            edits=post.get('edit_history_tweet_ids')
            if (not isinstance(edits,list) or not edits or post['id'] not in edits or
                    any(not isinstance(key,str) or not re.fullmatch(r'[0-9]{1,30}',key) for key in edits)):
                raise HubError('SOURCE_EDIT_HISTORY_NOT_VERIFIED',review=True)
            result.update(capture_integrity_passed=True,binding_id=binding['binding_id'],
                payload_sha256=binding['payload_sha256'],receipt_ids=[user['call_id'],page['call_id']],
                transport_modes=[user['transport_mode'],page['transport_mode']],
                captured_at=min(user['captured_at'],page['captured_at']),
                source_live_verified=all(row['transport_mode']=='default_tls' for row in (user,page)),
                source_facts={'author_id':author['id'],'post_id':post['id'],'url':expected['url'],
                    'published_at':post['created_at'],'full_text_sha256':hashlib.sha256(post['text'].encode()).hexdigest(),
                    'media_keys':attachments.get('media_keys',[]),'reference_ids':[ref['id'] for ref in refs],
                    'edited':len(edits)>1})
            if not result['source_live_verified']:result['failures'].append('SOURCE_CUSTOM_TRANSPORT_NOT_LIVE_VERIFIED')
        except HubError as exc:
            result['failures'].append(exc.code)
        except (OSError,ValueError,TypeError,KeyError,AttributeError):
            result['failures'].append('SOURCE_RECEIPT_DOCUMENT_INVALID')
        return result

    def status(self):
        return {'version':VERSION,'http_receipts':self.store.query('SELECT COUNT(*) n FROM source_http_receipts')[0]['n'],
            'bound_inputs':self.store.query('SELECT COUNT(*) n FROM tweet_source_bindings')[0]['n'],
            'live_http_receipts':self.store.query("SELECT COUNT(*) n FROM source_http_receipts WHERE transport_mode='default_tls'")[0]['n'],
            'publishing_enabled_by_receipts':False}
