"""Lease-bound public DOM provenance, separate from paid official X evidence."""
from datetime import datetime
import json
import os
from pathlib import Path
import time
import uuid
from .db import encode
from .source_receipts import digest
from .evidence import sha256_file
from .errors import HubError


class WebSources:
    def __init__(self,settings,store):self.settings,self.store=settings,store

    def _parent(self,discovery,db):
        parent=db.execute('SELECT * FROM jobs WHERE job_id=?',(discovery['job_id'],)).fetchone()
        if (not parent or parent['job_type']!='x_discovery' or parent['queue']!='tweet' or parent['state']!='processing' or
                parent['lease_owner']!=discovery.get('lease_owner') or not parent['lease_owner'] or
                parent['lease_until']<=time.time()):raise HubError('WEB_SOURCE_CAPTURE_LEASE_REQUIRED')

    def _binding(self,db,discovery,child_id,record):
        """Verify durable files and insert evidence on the caller's transaction."""
        self._parent(discovery,db)
        child=db.execute('SELECT * FROM jobs WHERE job_id=?',(child_id,)).fetchone()
        folder=self.settings.root/'media'/child_id
        observation=folder/'web-observation.json';screenshot=folder/'browser-source.png'
        try:
            if (not child or child['job_type']!='tweet_process' or child['source_platform']!='x' or
                    child['source_id']!=record['payload']['post']['id'] or json.loads(child['payload'])!=record['payload'] or
                    folder.is_symlink() or observation.is_symlink() or screenshot.is_symlink() or
                    any(p.is_symlink() for p in folder.parents) or
                    not observation.is_file() or json.loads(observation.read_text('utf-8'))!=record or
                    sha256_file(screenshot)!=record['payload']['capture']['screenshot_sha256']):
                raise ValueError()
            moment=datetime.fromisoformat(record['payload']['capture']['captured_at'].replace('Z','+00:00'))
            if moment.tzinfo is None:raise ValueError()
            captured=moment.timestamp()
        except (OSError,ValueError,KeyError,TypeError):raise HubError('WEB_SOURCE_BINDING_CHANGED') from None
        db.execute('INSERT OR IGNORE INTO web_source_bindings VALUES(?,?,?,?,?,?,?,?)',
            (child_id,discovery['job_id'],digest(record['payload']),str(observation),sha256_file(observation),
             sha256_file(screenshot),captured,time.time()))

    @staticmethod
    def _sync_dir(folder):
        if os.name!='nt':
            descriptor=os.open(folder,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(descriptor)
            finally:os.close(descriptor)

    @staticmethod
    def _write_durable(file,data):
        temporary=file.with_name(file.name+'.partial')
        with temporary.open('xb') as stream:
            os.chmod(temporary,0o600)
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,file)

    def _persist_observation(self,discovery,child_id,record):
        """A unique unpublished directory may survive a crash; no runnable row does."""
        folder=self.settings.root/'media'/child_id
        source=Path(record['screenshot_file'])
        parent_media=self.settings.root/'media'/discovery['job_id']
        if (source.is_symlink() or any(p.is_symlink() for p in source.parents) or
                not source.resolve().is_relative_to(parent_media.resolve()) or not source.is_file() or
                source.stat().st_size>40*1024*1024):raise HubError('WEB_SOURCE_SCREENSHOT_PATH_INVALID')
        from .maintenance import Maintenance
        encoded=encode(record).encode('utf-8')
        if len(encoded)>2_000_000:raise HubError('WEB_SOURCE_OBSERVATION_TOO_LARGE')
        Maintenance(self.settings,self.store).check_disk(reserve_bytes=source.stat().st_size+len(encoded))
        folder.mkdir(mode=0o700,exist_ok=False)
        if folder.is_symlink() or any(p.is_symlink() for p in folder.parents):raise HubError('WEB_SOURCE_SCREENSHOT_PATH_INVALID')
        self._write_durable(folder/'browser-source.png',source.read_bytes())
        self._write_durable(folder/'web-observation.json',encoded)
        self._sync_dir(folder);self._sync_dir(folder.parent)

    def ingest(self,discovery,record,policy):
        """Atomically expose one v2 child and its binding after durable file writes.

        An interruption before commit leaves no executable child. A repeat after
        commit returns the first observation without changing its source/files.
        """
        payload=record['payload'];source_id=payload['post']['id']
        if payload.get('capture',{}).get('protocol')!='x-web-dom-observation-v2':
            raise HubError('WEB_SOURCE_V2_REQUIRED')
        # Check the lease before disk I/O and again on the commit transaction.
        with self.store.transaction() as db:
            self._parent(discovery,db)
            prior=db.execute("SELECT job_id FROM jobs WHERE source_platform='x' AND source_id=? AND job_type='tweet_process' AND policy_version=?",
                (source_id,policy)).fetchone()
            if prior:return {'job_id':prior['job_id'],'created':False,
                'binding_present':bool(db.execute('SELECT 1 FROM web_source_bindings WHERE job_id=?',(prior['job_id'],)).fetchone())}
        ident=str(uuid.uuid4())
        try:self._persist_observation(discovery,ident,record)
        except OSError:raise HubError('WEB_SOURCE_FILES_NOT_DURABLE',retryable=True) from None
        now=time.time()
        with self.store.transaction() as db:
            self._parent(discovery,db)
            prior=db.execute("SELECT job_id FROM jobs WHERE source_platform='x' AND source_id=? AND job_type='tweet_process' AND policy_version=?",
                (source_id,policy)).fetchone()
            if prior:return {'job_id':prior['job_id'],'created':False,
                'binding_present':bool(db.execute('SELECT 1 FROM web_source_bindings WHERE job_id=?',(prior['job_id'],)).fetchone())}
            db.execute('''INSERT INTO jobs(job_id,source_platform,source_id,job_type,queue,state,
                created_at,updated_at,policy_version,payload) VALUES(?,'x',?,'tweet_process','tweet','pending',?,?,?,?)''',
                (ident,source_id,now,now,policy,encode(payload)))
            self._binding(db,discovery,ident,record)
        return {'job_id':ident,'created':True,'binding_present':True}

    def bind(self,discovery,child_id,record):
        with self.store.transaction() as db:
            self._binding(db,discovery,child_id,record)

    def assess(self,job,*,now=None,require_fresh=True):
        now=time.time() if now is None else now
        rows=self.store.query('SELECT * FROM web_source_bindings WHERE job_id=?',(job['job_id'],))
        if not rows:raise HubError('MACHINE_WEB_SOURCE_BINDING_MISSING')
        row=rows[0];folder=self.settings.root/'media'/job['job_id']
        current=self.store.get(job['job_id'])
        if (not current or current['source_platform']!='x' or current['job_type']!='tweet_process' or
                current['payload']!=job['payload'] or current['source_id']!=job['source_id']):
            raise HubError('MACHINE_WEB_SOURCE_JOB_CHANGED')
        observation=folder/'web-observation.json';image=folder/'browser-source.png'
        try:
            if (str(observation)!=row['observation_file'] or observation.is_symlink() or image.is_symlink() or
                    any(p.is_symlink() for p in folder.parents) or folder.is_symlink() or
                    sha256_file(observation)!=row['observation_sha256'] or sha256_file(image)!=row['screenshot_sha256']):raise ValueError()
            record=json.loads(observation.read_text('utf-8'));source=job['payload'];facts=record['machine_source_facts']
            cap=source['capture'];post=source['post'];handle=source['author']['username']
            if (record['payload']!=source or digest(source)!=row['payload_sha256'] or
                    cap['protocol']!='x-web-dom-observation-v2' or cap['human_review'] is not False or
                    cap['source_screenshot_reviewed'] is not False or cap['official_api_receipt'] is not False or
                    cap['screenshot_sha256']!=row['screenshot_sha256'] or cap['visibility_passed'] is not True or
                    cap['machine_facts_sha256']!=digest(facts) or facts['human_review'] is not False or
                    facts['version']!='x-visible-plain-text-v1' or facts['observed_text']!=post['text'] or
                    facts['status_ids']!=[post['id']] or handle not in {s['x_handle'] for s in self.settings.config['sources']} or
                    source['url']!=f"https://x.com/{handle}/status/{post['id']}" or post['id']!=job['source_id'] or
                    post.get('referenced_tweets') or source.get('media') or
                    row['captured_at']>now+5 or (require_fresh and
                    now-row['captured_at']>self.settings.config.get('source_receipt_max_age_seconds',86400))):
                raise ValueError()
            # Do not trust a self-reported "supported" flag without checking the
            # recorded DOM measurements that define this version's narrow scope.
            if (facts['supported'] is not True or facts['failures'] or facts['body_count']!=1 or
                    facts['foreign_image_count']!=0 or facts['unsupported_elements']!=0 or
                    facts['extra_links'] or facts['preceding_or_reply'] is not False or
                    facts['show_more'] is not False or facts['clipped'] is not False or
                    any(i.get('loaded') is not True for i in facts['images']) or
                    any(not item.get('href') or '\u2026' in item.get('visible','') or
                        item.get('visible','').endswith('...') for item in facts['body_links'])):
                raise HubError('MACHINE_SOURCE_CONTENT_UNSUPPORTED')
        except (OSError,KeyError,ValueError,TypeError):raise HubError('MACHINE_WEB_SOURCE_INTEGRITY_INVALID') from None
        return {'version':'web-source-policy-v1','capture_integrity_passed':True,'visible_plain_text_verified':True,
                'observation_sha256':row['observation_sha256'],'payload_sha256':row['payload_sha256'],
                'facts_sha256':cap['machine_facts_sha256'],'human_review':False,'official_api':False,
                'scope':facts['scope'],'hidden_or_unrendered_content_verified':False}
