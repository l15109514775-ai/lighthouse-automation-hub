"""Lease-bound extended DOM packets; no model, publishing or human approval.

Original captures remain in the discovery archive. A single SQLite transaction
exposes the processing child and its binding only after all inputs are durable.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from .db import encode
from .errors import HubError
from .evidence import sha256_file
from .source_receipts import digest
from .web_source import WebSources
from .reply_evidence_v2 import ReplyEvidenceV2

VERSION='x-web-extended-observation-v1'


class ExtendedWebSources:
    def __init__(self,settings,store):self.settings,self.store=settings,store

    def _file(self,discovery_id,value,expected=None):
        try:
            if str(uuid.UUID(discovery_id))!=discovery_id:raise ValueError()
            root=self.settings.root/'media'/discovery_id;file=Path(value)
            if not file.is_absolute():file=root/file
            if (file.is_symlink() or any(p.is_symlink() for p in file.parents) or not file.resolve().is_relative_to(root.resolve()) or
                not file.is_file() or file.stat().st_size>40*1024*1024 or expected and sha256_file(file)!=expected):raise ValueError()
            return file.resolve()
        except (ValueError,TypeError,OSError,AttributeError):raise HubError('EXTENDED_SOURCE_FILE_INVALID') from None

    def _references(self,discovery_id,record):
        result={}
        def walk(value):
            if isinstance(value,dict):
                if isinstance(value.get('file'),str) and isinstance(value.get('sha256'),str):
                    file=self._file(discovery_id,value['file'],value['sha256']);result[str(file)]=value['sha256']
                if isinstance(value.get('screenshot_file'),str):
                    expected=value.get('screenshot_sha256') or value.get('payload',{}).get('capture',{}).get('screenshot_sha256')
                    if expected:
                        file=self._file(discovery_id,value['screenshot_file'],expected);result[str(file)]=expected
                if isinstance(value.get('evidence_dir'),str):
                    root=self.settings.root/'media'/discovery_id;folder=Path(value['evidence_dir'])
                    if folder.is_symlink() or any(p.is_symlink() for p in folder.parents) or not folder.resolve().is_relative_to(root.resolve()):
                        raise HubError('EXTENDED_SOURCE_FILE_INVALID')
                for item in value.values():walk(item)
            elif isinstance(value,list):
                for item in value:walk(item)
        walk(record)
        if not result:raise HubError('EXTENDED_SOURCE_PROVENANCE_MISSING')
        return result

    def _verify(self,discovery_id,record,packet_file):
        try:
            post=record['payload'];cap=post['capture'];extended=record['machine_extended_source'];nodes=extended['nodes']
            discovery=self.store.get(discovery_id)
            if extended.get('collector_trigger')=='durable_scheduler' and (not discovery or discovery['source_platform']!='scheduler'):
                raise HubError('EXTENDED_SOURCE_SCHEDULER_ORIGIN_INVALID')
            if (record['version']!=VERSION or extended['version']!='extended-dom-source-v1' or cap['protocol']!=VERSION or
                cap['human_review'] is not False or cap['official_api_receipt'] is not False or
                cap['source_screenshot_reviewed'] is not False or cap['full_source_completeness_verified'] is not False or
                post['source_api']!='x-web-dom-v1' or post['author']['username'] not in {s['x_handle'] for s in self.settings.config['sources']} or
                not isinstance(nodes,list) or [n['role'] for n in nodes] not in (['post'],['post','parent']) or
                nodes[0]['source']!=post or nodes[0]['screenshot']['file']!=record['screenshot_file'] or
                nodes[0]['screenshot']['sha256']!=cap['screenshot_sha256']):raise ValueError()
            refs=self._references(discovery_id,record)
            env={k:os.environ[k] for k in ('PATH','TEMP','TMP','TMPDIR','SYSTEMROOT','WINDIR') if k in os.environ}
            script=Path(__file__).resolve().parents[2]/'adapters/browser_extended_source.mjs'
            proc=subprocess.run(['node',str(script),'--verify',str(packet_file),str(self.settings.root/'media'/discovery_id)],
                cwd=script.parent.parent,env=env,capture_output=True,timeout=90)
            if proc.returncode or len(proc.stdout)>2_000_000:raise HubError('EXTENDED_SOURCE_NODE_VALIDATOR_REJECTED')
            checked=json.loads(proc.stdout)
            if checked['version']!='extended-node-integrity-v1' or checked['integrity_verified'] is not True:
                raise HubError('EXTENDED_SOURCE_NODE_VALIDATOR_REJECTED')
            reply=extended.get('reply_packet')
            relation=None
            if len(nodes)==2:
                if not reply:raise ValueError()
                relation=ReplyEvidenceV2(self.settings,self.store).validate_packet(discovery_id,reply)
                raw_reply=relation['reply_source'];raw_parent=relation['parent_source']
                if nodes[0]['raw_source']!=raw_reply or nodes[1]['raw_source']!=raw_parent:raise ValueError()
                if post['reply_context']['parent_source']!=nodes[1]['source'] or post['post']['referenced_tweets']!=[
                    {'type':'replied_to','id':raw_parent['post']['id']}]:raise ValueError()
            elif reply or post['post'].get('referenced_tweets') or post.get('reply_context'):raise ValueError()
            result_nodes=[]
            for node in nodes:
                if not node['provenance']:raise ValueError()
                media=[]
                if node.get('media_bundle'):
                    for item in node['media_bundle']['files']:
                        media.append({k:item[k] for k in ('file','sha256','index','format','width','height')}|{'completeness':'complete'})
                result_nodes.append({'role':node['role'],'source':node['source'],'screenshot':node['screenshot'],
                    'media':media,'provenance':node['provenance']})
            return {'node_proof':checked,'nodes':result_nodes,'references':refs,'reply_proof':relation}
        except (ValueError,TypeError,KeyError,OSError,AttributeError,subprocess.SubprocessError):
            raise HubError('EXTENDED_SOURCE_PACKET_INVALID') from None

    def ingest(self,discovery,record,policy):
        source_id=record['payload']['post']['id']
        verifier=WebSources(self.settings,self.store)
        with self.store.transaction() as db:
            verifier._parent(discovery,db)
            if db.execute("SELECT 1 FROM source_tombstones WHERE source_platform='x' AND source_id=?",(source_id,)).fetchone():
                raise HubError('OWNER_BACKLOG_CLEARED')
            prior=db.execute("SELECT job_id FROM jobs WHERE source_platform='x' AND source_id=? AND job_type='tweet_process' AND policy_version=?",(source_id,policy)).fetchone()
            if prior:return {'job_id':prior['job_id'],'created':False,'binding_present':bool(db.execute('SELECT 1 FROM extended_web_source_bindings WHERE job_id=?',(prior['job_id'],)).fetchone())}
        ident=str(uuid.uuid4());folder=self.settings.root/'media'/ident;folder.mkdir(mode=0o700,exist_ok=False)
        from .maintenance import Maintenance
        encoded=encode(record).encode()
        Maintenance(self.settings,self.store).check_disk(reserve_bytes=len(encoded)+2_000_000)
        file=folder/'extended-source-packet.json'
        try:
            WebSources._write_durable(file,encoded);WebSources._sync_dir(folder);WebSources._sync_dir(folder.parent)
        except OSError:raise HubError('EXTENDED_SOURCE_FILES_NOT_DURABLE',retryable=True) from None
        proof=self._verify(discovery['job_id'],record,file)
        try:
            for name,expected in proof['references'].items():
                path=self._file(discovery['job_id'],name,expected)
                # Windows FlushFileBuffers needs a writable handle even when
                # flushing previously written bytes; no content is modified.
                with path.open('r+b' if os.name=='nt' else 'rb') as stream:os.fsync(stream.fileno())
            for parent in {Path(name).parent for name in proof['references']}:WebSources._sync_dir(parent)
        except OSError:raise HubError('EXTENDED_SOURCE_FILES_NOT_DURABLE',retryable=True) from None
        try:
            captured=datetime.fromisoformat(record['payload']['capture']['captured_at'].replace('Z','+00:00'))
            if captured.tzinfo is None or captured.timestamp()>time.time()+5:raise ValueError()
        except (ValueError,TypeError,KeyError):raise HubError('EXTENDED_SOURCE_TIMESTAMP_INVALID') from None
        now=time.time()
        with self.store.transaction() as db:
            verifier._parent(discovery,db)
            if db.execute("SELECT 1 FROM source_tombstones WHERE source_platform='x' AND source_id=?",(source_id,)).fetchone():raise HubError('OWNER_BACKLOG_CLEARED')
            prior=db.execute("SELECT job_id FROM jobs WHERE source_platform='x' AND source_id=? AND job_type='tweet_process' AND policy_version=?",(source_id,policy)).fetchone()
            if prior:return {'job_id':prior['job_id'],'created':False,'binding_present':bool(db.execute('SELECT 1 FROM extended_web_source_bindings WHERE job_id=?',(prior['job_id'],)).fetchone())}
            self._references(discovery['job_id'],record)
            db.execute('''INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,created_at,updated_at,policy_version,payload)
                VALUES(?,'x',?,'tweet_process','tweet','pending',?,?,?,?)''',(ident,source_id,now,now,policy,encode(record['payload'])))
            db.execute('INSERT INTO extended_web_source_bindings VALUES(?,?,?,?,?,?,?)',
                (ident,discovery['job_id'],digest(record['payload']),str(file),sha256_file(file),captured.timestamp(),now))
        return {'job_id':ident,'created':True,'binding_present':True}

    def assess(self,job,require_fresh=True):
        rows=self.store.query('SELECT * FROM extended_web_source_bindings WHERE job_id=?',(job['job_id'],))
        if not rows:raise HubError('EXTENDED_SOURCE_BINDING_MISSING')
        row=rows[0];current=self.store.get(job['job_id'])
        if not current or current['payload']!=job['payload'] or current['source_id']!=job['source_id'] or digest(job['payload'])!=row['payload_sha256']:
            raise HubError('EXTENDED_SOURCE_JOB_CHANGED')
        file=self._file(job['job_id'],row['packet_file'],row['packet_sha256'])
        record=json.loads(file.read_text('utf-8'))
        if record['payload']!=job['payload']:raise HubError('EXTENDED_SOURCE_JOB_CHANGED')
        if row['captured_at']>time.time()+5 or require_fresh and time.time()-row['captured_at']>self.settings.config.get('source_receipt_max_age_seconds',86400):
            raise HubError('EXTENDED_SOURCE_CAPTURE_EXPIRED')
        checked=self._verify(row['discovery_job_id'],record,file)
        return {'proof':{'version':'extended-web-source-proof-v1','capture_integrity_passed':True,
            'payload_sha256':row['payload_sha256'],'packet_sha256':row['packet_sha256'],'discovery_job_id':row['discovery_job_id'],
            'node_proof':checked['node_proof'],'reply_proof':checked['reply_proof'],'human_review':False,'official_api':False,
            'automatic_publication_eligible':False},'nodes':checked['nodes']}
