"""Explicit test disposal and full-duration published-video original retention.

Local MP4 only. No Bilibili deletion, credential, arbitrary shell or source/job
deduplication removal. Root attestation enables this code; a separately recorded
real owner instruction controls disposal. Neither one approves publication.
"""
from datetime import datetime,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import uuid
import xml.etree.ElementTree as ET

from adapters.media_tools import inspect_media,packet_evidence
from .archive_source_retention_v2 import _inspect,_path,_root_protected,_uuid,digest
from .db import encode
from .errors import HubError
from .locks import resource_guard

VERSION='video-source-retention-v2'
PRINCIPAL='operator:local-video-retention-v2'
DISPOSAL='explicit_abandoned_test_original'
PUBLISHED='confirmed_full_duration_public_video'
STATES={'short_video_web_v1':('short_video_publish','video_website_short_v1'),
    'replay_video_web_v1':('replay_video_publish','video_website_replay_v1'),
    'replay_highest_video_web_v2':('replay_highest_video_publish_v2','video_website_replay_highest_v2'),
    'replay_natural_video_web_v3':('replay_natural_video_publish_v3','video_website_replay_natural_v3')}
MAX_BYTES=14_000_000_000
TEST_COUNT=19


def fail(code='VIDEO_RETENTION_EVIDENCE_REQUIRED'):raise HubError(code,review=True)


class VideoSourceRetentionV2:
    RELEASE_FILENAME='video-source-retention-v2-release.json'
    RELEASE_EXTRA_SOURCES=()
    TEST_COUNT=TEST_COUNT
    TEST_GROUPS={'tests.test_video_source_retention_v2':TEST_COUNT}
    TEST_VERSION='video-source-retention-v2-ubuntu-tests-v1'
    def __init__(self,settings,store,*,highest_service_factory=None):
        self.settings,self.store=settings,store;self.root=Path(settings.root)
        self.folder=self.root/'data/video-source-retention-v2';self.path=self.folder/'intent.sqlite3'
        self.highest_service_factory=highest_service_factory

    def initialize(self):
        _path(self.root,self.folder);self.folder.mkdir(mode=0o700,exist_ok=True)
        _path(self.root,self.path)
        with sqlite3.connect(self.path) as db:
            db.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,plan_sha256 TEXT NOT NULL,state TEXT NOT NULL,record TEXT NOT NULL);''')
        if os.name=='posix':self.path.chmod(0o600)
        with self.store.transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS video_retention_authorizations_v2(
                id TEXT PRIMARY KEY,record TEXT NOT NULL)''')

    def authorize(self,principal,*,actual_owner_response,owner_response_reference,scope,job_id=None):
        if (principal!=PRINCIPAL or scope not in (DISPOSAL,PUBLISHED) or
                any(not isinstance(x,str) or not x.strip() or len(x)>2048 for x in (actual_owner_response,owner_response_reference)) or
                scope==DISPOSAL and not _uuid(job_id) or scope==PUBLISHED and job_id is not None):
            fail('VIDEO_RETENTION_OWNER_INSTRUCTION_REQUIRED')
        self.initialize()
        if scope==DISPOSAL:
            job,binding,state,source,publication=self._context(job_id)
            if state.get('owner_authorized_remote_deletion_v1',{}).get('remote_state')!='owner_deleted':
                fail('VIDEO_RETENTION_EXPLICIT_ABANDONED_TEST_REQUIRED')
            artifact={'job_id':job_id,'download_job_id':binding['download_job_id'],'source_id':binding['source_id'],
                'artifact_sha256':binding['artifact_sha256'],'file_bytes':binding['file_bytes']}
        else:artifact=None
        record={'version':VERSION,'authorization_id':str(uuid.uuid4()),'scope':scope,'artifact':artifact,
            'actual_owner_response':actual_owner_response,'owner_response_reference':owner_response_reference,
            'target_site_uid':str(self.settings.config['target_bilibili_site_uid']),
            'recorded_at':datetime.now(timezone.utc).isoformat(),'supersedes_legacy_local_test_hold':True,
            'formal_workflow_launch_claimed':False,'publication_authorized':False,'remote_video_deletion_authorized':False}
        with self.store.transaction() as db:
            db.execute('INSERT INTO video_retention_authorizations_v2 VALUES(?,?)',(record['authorization_id'],encode(record)))
            db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                (time.time(),principal,VERSION,job_id or 'published-video-originals','new_owner_retention_instruction_old_holds_retained'))
        return record

    def _authorization(self,ident):
        if not _uuid(ident):fail('VIDEO_RETENTION_OWNER_INSTRUCTION_REQUIRED')
        rows=self.store.query('SELECT record FROM video_retention_authorizations_v2 WHERE id=?',(ident,))
        if len(rows)!=1:fail('VIDEO_RETENTION_OWNER_INSTRUCTION_REQUIRED')
        value=json.loads(rows[0]['record'])
        if value['target_site_uid']!=str(self.settings.config['target_bilibili_site_uid']):fail()
        return value

    def _context(self,ident,connection=None):
        def get(job_id):
            if connection is None:return self.store.get(job_id)
            row=connection.execute('SELECT * FROM jobs WHERE job_id=?',(job_id,)).fetchone()
            if not row:return None
            value=dict(row)
            for key in ('payload','result'):value[key]=json.loads(value[key])
            return value
        job=get(ident)
        if not job or job['source_platform']!='youtube' or job['queue']!='video' or job['state']!='completed':fail()
        binding=job['payload'];names=[key for key in STATES if key in job['result']];states=[job['result'][key] for key in names]
        if len(states)!=1:fail()
        job_type,kind=STATES[names[0]]
        if job['job_type']!=job_type:fail()
        state=states[0];source=get(binding['download_job_id'])
        if (state['state']!='completed' or not source or source['state']!='completed' or source['source_id']!=job['source_id'] or
                binding['source_id']!=job['source_id'] or binding['site_uid']!=str(self.settings.config['target_bilibili_site_uid']) or
                binding['channel_id']!=source['payload']['channel_id'] or binding['channel_id'] not in
                    {item['youtube_channel_id'] for item in self.settings.config['sources']} or
                any(value.get(key) for value in (job,source) for key in ('lease_owner','lease_until'))):fail()
        file=_path(self.root,binding['file']);expected={self.root/'media'/source['job_id']/directory/'original.mp4'
            for directory in ('youtube-download-v1','youtube-highest-v2','youtube-highest-v3')}
        if file not in expected or type(binding['file_bytes']) is not int or not 0<binding['file_bytes']<=MAX_BYTES:
            fail('VIDEO_RETENTION_EXACT_ORIGINAL_REQUIRED')
        rows=(connection.execute("SELECT * FROM publications WHERE source_platform='youtube' AND source_id=?",(job['source_id'],)).fetchall()
            if connection is not None else self.store.query("SELECT * FROM publications WHERE source_platform='youtube' AND source_id=?",(job['source_id'],)))
        publications=[dict(row) for row in rows]
        if len(publications)!=1:fail('VIDEO_RETENTION_PUBLICATION_UNRESOLVED')
        publication=publications[0];observation=state['result_observation']
        if (publication['job_id']!=ident or publication['state']!='completed' or publication['platform']!='bilibili' or
                publication['account_id']!='site_uid:'+binding['site_uid'] or publication['idempotency_key']!=state['key'] or
                publication['kind']!=kind or binding['source_url']!='https://www.youtube.com/watch?v='+job['source_id'] or
                publication['external_id']!=observation['external_id'] or publication['external_url']!=observation['url'] or
                job['external_publish_id']!=observation['external_id'] or observation['state']!='completed' or
                observation['site_uid']!=binding['site_uid'] or observation['source_url']!=binding['source_url'] or
                observation['metadata_sha256']!=binding['metadata_sha256']):fail('VIDEO_RETENTION_PUBLICATION_UNRESOLVED')
        alljobs=(connection.execute('SELECT * FROM jobs').fetchall() if connection is not None else self.store.query('SELECT * FROM jobs'))
        def references(value):
            if isinstance(value,dict):return any(references(item) for item in value.values())
            if isinstance(value,list):return any(references(item) for item in value)
            return value==str(file)
        for row in alljobs:
            row=dict(row)
            if row['job_id'] in (ident,source['job_id']):continue
            if references(json.loads(row['payload'])) or references(json.loads(row['result'])):
                fail('VIDEO_RETENTION_SHARED_ORIGINAL')
            if row['state'] not in ('completed','failed') and (row['source_platform']=='youtube' and row['source_id']==job['source_id'] or
                    references(json.loads(row['payload'])) or references(json.loads(row['result']))):
                fail('VIDEO_RETENTION_SOURCE_IN_USE')
        return job,binding,state,source,publication

    def _public_service(self,job):
        if self.highest_service_factory is not None:return self.highest_service_factory(self.settings,self.store)
        if 'replay_natural_video_web_v3' in job['result']:
            from .replay_natural_video_v3 import NaturalReplayVideoWeb
            return NaturalReplayVideoWeb(self.settings,self.store)
        from .replay_highest_video_v2 import HighestReplayVideoWeb
        return HighestReplayVideoWeb(self.settings,self.store)

    def _proof(self,ident,scope,*,check=lambda:None,defer_public=False,reuse_packets=None):
        job,binding,state,source,publication=self._context(ident);check()
        file=_path(self.root,binding['file']);actual=_inspect(self.root,file,MAX_BYTES)
        if actual['sha256']!=binding['artifact_sha256'] or actual['bytes']!=binding['file_bytes']:fail('VIDEO_RETENTION_ORIGINAL_CHANGED')
        if scope==DISPOSAL:
            deleted=state.get('owner_authorized_remote_deletion_v1',{})
            if deleted.get('remote_state')!='owner_deleted' or deleted.get('external_id')!=publication['external_id']:
                fail('VIDEO_RETENTION_EXPLICIT_ABANDONED_TEST_REQUIRED')
            basis={'version':'explicit-abandoned-test-disposal-v2','remote_state':'owner_deleted',
                'remote_recoverability_claimed':False,'tombstone_sha256':digest(deleted),
                'prior_local_hold_sha256':digest(state.get('test_retention_policy')),
                'formal_workflow_launch_claimed':False}
        else:
            if 'owner_authorized_remote_deletion_v1' in state:fail('VIDEO_RETENTION_PUBLIC_COMPLETION_REQUIRED')
            service=self._public_service(job)
            if not any(key in job['result'] for key in ('replay_highest_video_web_v2','replay_natural_video_web_v3')):
                fail('VIDEO_RETENTION_FULL_COVERAGE_REQUIRED')
            download=source['result'].get(service.DOWNLOAD_STATE,{});asset=download.get('source_asset',{})
            if (asset.get('file')!=binding['file'] or asset.get('sha256')!=actual['sha256'] or asset.get('bytes')!=actual['bytes'] or
                    download.get('receipt',{}).get('source_to_merged_packet_coverage_verified') is not True):
                fail('VIDEO_RETENTION_FULL_COVERAGE_REQUIRED')
            details=inspect_media(file,check=check,include_frame_rates=True)
            packets=(packet_evidence(file,details['stream_types'],check=check) if reuse_packets is None else reuse_packets)
            if packets!=asset.get('packet_evidence') or abs(details['duration']-download['binding']['duration'])>2:
                fail('VIDEO_RETENTION_FULL_COVERAGE_REQUIRED')
            # Last, reopen actual upload/public JSON and require fresh playback;
            # do not age that evidence while hashing/scanning a long original.
            coverage=None
            if not defer_public:
                summary=service.cleanup_coverage_summary(ident);check()
                if summary['job']!=job or summary['binding']!=binding:fail('VIDEO_RETENTION_JOB_CHANGED')
                coverage=summary['coverage']
                if coverage['external_id']!=publication['external_id'] or coverage['url']!=publication['external_url']:
                    fail('VIDEO_RETENTION_FULL_COVERAGE_REQUIRED')
            basis={'version':'verified-full-duration-published-original-v2','coverage':coverage,
                'local_packet_evidence':packets,'local_duration_seconds':details['duration'],
                'prior_local_hold_sha256':digest(state.get('test_retention_policy')),
                'server_file_hash_verified':False,'original_encoded_bytes_recoverable_from_bilibili':False,
                'meaning':'owner permits disposal after causally bound complete upload and full-duration public playback'}
        return actual,{'job_sha256':digest(job),'source_sha256':digest(source),'publication_sha256':digest(publication),
            'binding_sha256':digest(binding),'basis':basis,'job_id':ident,'download_job_id':source['job_id'],'source_id':job['source_id'],
            'pipeline_state':next(key for key in STATES if key in job['result'])}

    def plan(self,job_id,*,authorization_id,check=lambda:None):
        authorization=self._authorization(authorization_id);scope=authorization['scope']
        with resource_guard(self.root/'data/locks/heavy',self.folder/'lock'):
            actual,proof=self._proof(job_id,scope,check=check)
        if scope==DISPOSAL and authorization['artifact']!={'job_id':job_id,'download_job_id':proof['download_job_id'],
                'source_id':proof['source_id'],'artifact_sha256':actual['sha256'],'file_bytes':actual['bytes']}:fail()
        plan={'version':VERSION,'plan_id':str(uuid.uuid4()),'scope':scope,'job_id':job_id,
            'authorization_id':authorization_id,'authorization_sha256':digest(authorization),'candidate':actual,'proof':proof,
            'created_at':datetime.now(timezone.utc).isoformat(),'local_files_deleted':0,'publications_removed':False,
            'remote_video_deleted':False,'formal_workflow_launch_claimed':False}
        return {**plan,'plan_sha256':digest(plan)}

    def _release(self,plan):
        try:
            file=self.root/'config'/self.RELEASE_FILENAME;_root_protected(file)
            record=json.loads(file.read_bytes());source_root=Path(__file__).resolve().parents[2]
            names=['services/hub/video_source_retention_v2.py','services/hub/archive_source_retention_v2.py',*self.RELEASE_EXTRA_SOURCES]
            if plan['scope']==PUBLISHED:
                names+=['services/hub/replay_highest_video_v2.py','services/hub/replay_highest_coverage_v2.py']
                if plan['proof'].get('pipeline_state')=='replay_natural_video_web_v3':names+=['services/hub/replay_natural_video_v3.py']
            sources={name:hashlib.sha256((source_root/name).read_bytes()).hexdigest() for name in names}
            if (record['version']!='video-source-retention-release-v2' or record['authorization_id']!=plan['authorization_id'] or
                    record['scope']!=plan['scope'] or record['source_files']!=sources):fail('VIDEO_RETENTION_RELEASE_REQUIRED')
            ref=record['test_attestation'];path=Path(ref['file']);_root_protected(path);raw=path.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=ref['sha256']:fail('VIDEO_RETENTION_RELEASE_REQUIRED')
            evidence=json.loads(raw);junit=Path(evidence['junit_file']);_root_protected(junit);raw=junit.read_bytes()
            allowed_sources={'services/hub/video_source_retention_v2.py','services/hub/archive_source_retention_v2.py',
                'services/hub/replay_highest_video_v2.py','services/hub/replay_highest_coverage_v2.py','services/hub/replay_natural_video_v3.py',
                'services/hub/video_retention_scheduler_v2.py'}|set(self.RELEASE_EXTRA_SOURCES)
            if (evidence['version']!=self.TEST_VERSION or
                    not set(sources)<=set(evidence['source_files'])<=allowed_sources or
                    any(evidence['source_files'][name]!=checksum for name,checksum in sources.items()) or
                    evidence['network']!='none' or evidence['application_data_mounted'] is not False or
                    evidence['production_profile_mounted'] is not False or evidence['exit_code']!=0 or
                    evidence.get('full_suite_passed') is not True or evidence.get('oom_killed') is not False or
                    any(evidence.get(key) is not True for key in ('no_pid_or_oom_denial','runtime_matches_declared_candidate',
                        'mounts_match_isolated_allowlist')) or
                    hashlib.sha256(raw).hexdigest()!=evidence['junit_sha256']):fail('VIDEO_RETENTION_RELEASE_REQUIRED')
            cases=ET.fromstring(raw).findall('.//testcase')
            groups={}
            for case in cases:groups[case.get('classname')]=groups.get(case.get('classname'),0)+1
            if (len(cases)!=self.TEST_COUNT or len(cases)!=evidence['tests'] or groups!=self.TEST_GROUPS or
                    any(case.find(tag) is not None for case in cases for tag in ('failure','error','skipped'))):
                fail('VIDEO_RETENTION_RELEASE_REQUIRED')
            return hashlib.sha256(file.read_bytes()).hexdigest()
        except (OSError,ValueError,KeyError,TypeError,ET.ParseError):fail('VIDEO_RETENTION_RELEASE_REQUIRED')

    def apply(self,plan):
        if (plan.get('version')!=VERSION or not _uuid(plan.get('plan_id')) or
                digest({k:v for k,v in plan.items() if k!='plan_sha256'})!=plan.get('plan_sha256')):fail('VIDEO_RETENTION_PLAN_CHANGED')
        authorization=self._authorization(plan['authorization_id'])
        if digest(authorization)!=plan['authorization_sha256']:fail('VIDEO_RETENTION_PLAN_CHANGED')
        release_sha=self._release(plan);self.initialize()
        locks=(self.root/'data/locks/video',self.root/'data/locks/heavy',self.folder/'lock')
        with resource_guard(*locks):
            with sqlite3.connect(self.path) as index:
                previous=index.execute('SELECT plan_sha256,state,record FROM operations WHERE id=?',(plan['plan_id'],)).fetchone()
            if previous:
                if previous[0]!=plan['plan_sha256']:fail('VIDEO_RETENTION_PLAN_CHANGED')
                if previous[1]=='completed':return json.loads(previous[2])
            path=Path(plan['candidate']['file'])
            if previous and not path.exists():
                current=self.store.get(plan['job_id']);source=self.store.get(plan['proof']['download_job_id'])
                recorded=current['result'].get('local_original_retention_v2') if current else None
                if (recorded and source and source['result'].get('local_original_retention_v2')==recorded and
                        recorded.get('plan_id')==plan['plan_id'] and recorded.get('file_sha256')==plan['candidate']['sha256'] and
                        recorded.get('file')==str(path) and recorded.get('scope')==plan['scope']):
                    with sqlite3.connect(self.path) as index:
                        index.execute("UPDATE operations SET state='completed',record=? WHERE id=?",(encode(recorded),plan['plan_id']))
                    return recorded
            if path.exists():
                actual,proof=self._proof(plan['job_id'],plan['scope'])
                if actual!=plan['candidate'] or proof!=plan['proof']:fail('VIDEO_RETENTION_PLAN_CHANGED')
            elif not previous:fail('VIDEO_RETENTION_UNEXPECTED_MISSING_ORIGINAL')
            with self.store.transaction() as connection:
                job,binding,state,source,publication=self._context(plan['job_id'],connection)
                if any(digest(value)!=plan['proof'][key] for value,key in ((job,'job_sha256'),(source,'source_sha256'),(publication,'publication_sha256'))):
                    fail('VIDEO_RETENTION_PLAN_CHANGED')
                intent={'version':VERSION,'plan':plan,'release_sha256':release_sha,'state':'deleting'}
                with sqlite3.connect(self.path) as index:
                    index.execute('PRAGMA synchronous=FULL')
                    index.execute('INSERT OR IGNORE INTO operations VALUES(?,?,?,?)',
                        (plan['plan_id'],plan['plan_sha256'],'deleting',encode(intent)))
                if path.exists():
                    self._unlink_exact(plan['candidate'])
                result={'version':VERSION,'plan_id':plan['plan_id'],'job_id':job['job_id'],'download_job_id':source['job_id'],
                    'source_id':job['source_id'],'scope':plan['scope'],'file':str(path),'file_sha256':plan['candidate']['sha256'],
                    'file_bytes':plan['candidate']['bytes'],'local_files_deleted':1,'publication_records_preserved':True,
                    'source_id_idempotency_records_preserved':True,'remote_video_deleted':False,
                    'evidence_files_deleted':False,'formal_workflow_launch_claimed':False,'authorization_id':plan['authorization_id'],
                    'superseded_local_hold_sha256':plan['proof']['basis']['prior_local_hold_sha256'],
                    'actual_owner_response':authorization['actual_owner_response'],'owner_response_reference':authorization['owner_response_reference'],
                    'deleted_at':datetime.now(timezone.utc).isoformat()}
                for value in (job,source):
                    updated={**value['result'],'local_original_retention_v2':result}
                    connection.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',(encode(updated),time.time(),value['job_id']))
                connection.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (time.time(),PRINCIPAL,VERSION,job['job_id'],'exact_local_mp4_only_old_hold_and_publication_history_preserved'))
            with sqlite3.connect(self.path) as index:
                index.execute('PRAGMA synchronous=FULL')
                index.execute("UPDATE operations SET state='completed',record=? WHERE id=?",(encode(result),plan['plan_id']))
            return result

    def _unlink_exact(self,item):
        path=_path(self.root,item['file']);actual=_inspect(self.root,path,MAX_BYTES)
        if actual!=item:fail('VIDEO_RETENTION_ORIGINAL_CHANGED')
        if os.name=='posix':
            fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                for part in path.parent.relative_to(self.root).parts:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
                parent=os.fstat(fd);current=os.stat(path.name,dir_fd=fd,follow_symlinks=False)
                if ((parent.st_dev,parent.st_ino)!=(item['parent_device'],item['parent_inode']) or
                        not stat.S_ISREG(current.st_mode) or current.st_nlink!=1 or
                        (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)!=(item['device'],item['inode'],item['bytes'],item['mtime_ns'])):
                    fail('VIDEO_RETENTION_ORIGINAL_CHANGED')
                os.unlink(path.name,dir_fd=fd);os.fsync(fd)
            finally:os.close(fd)
        else:path.unlink()  # Owned Windows tests; production uses dir_fd above.


class VideoSourceTracksRetentionV2(VideoSourceRetentionV2):
    """Compatible explicit extension: original MP4 plus its two bound tracks."""
    RELEASE_FILENAME='video-source-retention-tracks-v2-release.json'
    RELEASE_EXTRA_SOURCES=('services/hub/video_retention_scheduler_v2.py','services/hub/replay_natural_video_v3.py')
    TEST_COUNT=38
    TEST_GROUPS={'tests.test_video_source_retention_v2':19,'tests.test_video_source_retention_tracks_v2':13,
        'tests.test_video_retention_scheduler_v2':6}
    TEST_VERSION='video-source-retention-tracks-v2-ubuntu-tests-v1'

    def _format_id_allowed(self,value,download_state):
        """V2/default behavior is unchanged; newer routes use an explicit class."""
        return isinstance(value,str) and re.fullmatch('[0-9]{1,5}',value) is not None

    def _tracks(self,job_id,proof,*,check=lambda:None,reuse_packets=None):
        try:return self._track_files(job_id,proof,check=check,reuse_packets=reuse_packets)
        except HubError:raise
        except (OSError,KeyError,TypeError,ValueError,AttributeError):fail('VIDEO_RETENTION_TRACK_RECEIPT_CHANGED')

    def _track_files(self,job_id,proof,*,check=lambda:None,reuse_packets=None):
        from adapters.ytdlp_replay import source_packet_evidence
        job,binding,state,source,publication=self._context(job_id)
        if digest(source)!=proof['source_sha256'] or digest(job)!=proof['job_sha256']:fail('VIDEO_RETENTION_JOB_CHANGED')
        download_state='youtube_download_v3' if proof['pipeline_state']=='replay_natural_video_web_v3' else 'youtube_download_v2'
        checkpoint=source['result'].get(download_state,{})
        expected_version='youtube-replay-highest-natural-download-v3' if download_state=='youtube_download_v3' else 'youtube-replay-highest-download-v2'
        folder=Path(binding['file']).parent;receipt_file=folder/'verified.json'
        expected_directory='youtube-highest-v3' if download_state=='youtube_download_v3' else 'youtube-highest-v2'
        expected_policy='youtube-highest-natural-download-v3' if download_state=='youtube_download_v3' else 'youtube-highest-download-v2'
        if (folder!=self.root/'media'/source['job_id']/expected_directory or
                source['job_type']!=download_state or source['policy_version']!=expected_policy):
            fail('VIDEO_RETENTION_TRACK_ROUTE_REJECTED')
        ref=_inspect(self.root,receipt_file,262144);receipt=json.loads(receipt_file.read_bytes())
        if (ref['sha256']!=binding['download_receipt_sha256'] or checkpoint.get('state')!='verified' or checkpoint.get('version')!=expected_version or
                checkpoint['binding'].get('job_id')!=source['job_id'] or checkpoint['binding'].get('video_id')!=source['source_id'] or
                checkpoint['binding'].get('channel_id')!=binding['channel_id'] or
                receipt.get('version')!=checkpoint.get('version') or receipt.get('binding_sha256')!=digest(checkpoint['binding']) or
                receipt.get('source_asset')!=checkpoint.get('source_asset') or receipt.get('receipt')!=checkpoint.get('receipt')):
            fail('VIDEO_RETENTION_TRACK_RECEIPT_CHANGED')
        selected=checkpoint['binding']['source_plan']['selected_formats'];tracks=checkpoint['receipt']['tracks']
        if (not isinstance(selected,list) or not isinstance(tracks,list) or len(selected)!=len(tracks) or len(tracks)!=2 or
                checkpoint['source_asset'].get('selected_formats')!=selected or checkpoint['receipt'].get('selected_formats')!=selected or
                checkpoint['source_asset'].get('source_plan')!=checkpoint['binding']['source_plan'] or
                checkpoint['receipt'].get('source_plan')!=checkpoint['binding']['source_plan']):
            fail('VIDEO_RETENTION_TRACK_RECEIPT_CHANGED')
        fields={'format_id','ext','vcodec','acodec','width','height','fps','filesize'};paths=[];candidates=[]
        for number,(expected,track) in enumerate(zip(selected,tracks)):
            if (not isinstance(expected,dict) or set(expected)!=fields or not isinstance(track,dict) or set(track)!=fields|{'bytes','sha256'} or
                    any(track.get(key)!=value for key,value in expected.items()) or not isinstance(expected['format_id'],str) or
                    not self._format_id_allowed(expected['format_id'],download_state) or
                    (expected['ext'],expected['vcodec'],expected['acodec'])!=(('mp4','h264','none') if number==0 else ('m4a','none','aac')) or
                    type(expected['filesize']) is not int or not 0<expected['filesize']<=MAX_BYTES or
                    type(track['bytes']) is not int or track['bytes']!=expected['filesize']):
                fail('VIDEO_RETENTION_TRACK_RECEIPT_CHANGED')
            file=folder/('stream-'+expected['format_id']+'.'+expected['ext']);actual=_inspect(self.root,file,MAX_BYTES)
            if actual['bytes']!=track['bytes'] or actual['sha256']!=track['sha256']:fail('VIDEO_RETENTION_TRACK_CHANGED')
            paths.append(file);candidates.append(actual);check()
        packet_proof=source_packet_evidence(paths,check) if reuse_packets is None else reuse_packets
        if packet_proof!=checkpoint['source_asset']['packet_evidence'] or packet_proof!=proof['basis']['local_packet_evidence']:
            fail('VIDEO_RETENTION_TRACK_PACKET_COVERAGE_CHANGED')
        with self.store.transaction() as connection:self._unshared(connection,[str(path) for path in paths],job['job_id'],source['job_id'])
        return candidates,{'version':'bound-source-track-cleanup-v1','verified_receipt':{key:ref[key] for key in ('file','bytes','sha256')},
            'source_plan_sha256':digest(checkpoint['binding']['source_plan']),'selected_formats':selected,
            'source_to_merged_packet_evidence':packet_proof,'cleanup_recomputed_source_packet_coverage':reuse_packets is None,
            'source_track_files':[{'file':item['file'],'bytes':item['bytes'],'sha256':item['sha256']} for item in candidates],
            'server_file_hash_verified':False}

    def _unshared(self,connection,paths,publish_job,source_job):
        paths=set(paths)
        def uses(value):
            if isinstance(value,dict):return any(uses(item) for item in value.values())
            if isinstance(value,list):return any(uses(item) for item in value)
            return isinstance(value,str) and value in paths
        for row in connection.execute('SELECT job_id,payload,result FROM jobs'):
            if row['job_id'] not in (publish_job,source_job) and (uses(json.loads(row['payload'])) or uses(json.loads(row['result']))):
                fail('VIDEO_RETENTION_SHARED_ORIGINAL')

    def plan(self,job_id,*,authorization_id,check=lambda:None,refresh_public_observation=None):
        authorization=self._authorization(authorization_id)
        if authorization['scope']!=PUBLISHED:fail('VIDEO_RETENTION_TRACKS_REQUIRE_PUBLIC_COMPLETION')
        with resource_guard(self.root/'data/locks/heavy',self.folder/'lock'):
            merged,proof=self._proof(job_id,PUBLISHED,check=check,defer_public=True)
            tracks,track_proof=self._tracks(job_id,proof,check=check)
        # The browser's existing lookup owns its own heavy/profile lock. Never
        # hold the local-scan heavy lock while asking that action to refresh.
        if refresh_public_observation is not None:refresh_public_observation()
        job,binding,state,source,publication=self._context(job_id)
        summary=self._public_service(job).cleanup_coverage_summary(job_id);check()
        if summary['binding']!=binding or digest(source)!=proof['source_sha256'] or digest(binding)!=proof['binding_sha256']:
            fail('VIDEO_RETENTION_JOB_CHANGED')
        coverage=summary['coverage']
        if coverage['external_id']!=publication['external_id'] or coverage['url']!=publication['external_url']:
            fail('VIDEO_RETENTION_FULL_COVERAGE_REQUIRED')
        proof.update(job_sha256=digest(job),publication_sha256=digest(publication))
        proof['basis']['coverage']=coverage
        plan={'version':VERSION,'cleanup_mode':'bound_original_and_source_tracks_v1','plan_id':str(uuid.uuid4()),
            'scope':PUBLISHED,'job_id':job_id,'authorization_id':authorization_id,'authorization_sha256':digest(authorization),
            'candidate':merged,'candidates':[*tracks,merged],'source_track_cleanup':track_proof,'proof':proof,
            'created_at':datetime.now(timezone.utc).isoformat(),'local_files_deleted':0,'publications_removed':False,
            'remote_video_deleted':False,'formal_workflow_launch_claimed':False}
        return {**plan,'plan_sha256':digest(plan)}

    def _persist(self,plan,record,state='deleting'):
        with sqlite3.connect(self.path) as index:
            index.execute('PRAGMA synchronous=FULL')
            index.execute('INSERT INTO operations VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,record=excluded.record',
                (plan['plan_id'],plan['plan_sha256'],state,encode(record)))

    def _stat_unlink(self,item,ctime_ns):
        path=_path(self.root,item['file'])
        if os.name=='posix':
            fd=os.open(self.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                for part in path.parent.relative_to(self.root).parts:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
                parent=os.fstat(fd);current=os.stat(path.name,dir_fd=fd,follow_symlinks=False)
                if ((parent.st_dev,parent.st_ino)!=(item['parent_device'],item['parent_inode']) or
                        not stat.S_ISREG(current.st_mode) or current.st_nlink!=1 or current.st_ctime_ns!=ctime_ns or
                        (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)!=(item['device'],item['inode'],item['bytes'],item['mtime_ns'])):
                    fail('VIDEO_RETENTION_ORIGINAL_CHANGED')
                os.unlink(path.name,dir_fd=fd);os.fsync(fd)
            finally:os.close(fd)
        else:
            current=path.stat()
            if (current.st_ino,current.st_size,current.st_mtime_ns,current.st_ctime_ns)!=(item['inode'],item['bytes'],item['mtime_ns'],ctime_ns):
                fail('VIDEO_RETENTION_ORIGINAL_CHANGED')
            path.unlink()

    def apply(self,plan):
        if (plan.get('version')!=VERSION or plan.get('cleanup_mode')!='bound_original_and_source_tracks_v1' or
                not _uuid(plan.get('plan_id')) or digest({k:v for k,v in plan.items() if k!='plan_sha256'})!=plan.get('plan_sha256') or
                plan.get('scope')!=PUBLISHED or len(plan.get('candidates',[]))!=3):fail('VIDEO_RETENTION_PLAN_CHANGED')
        authorization=self._authorization(plan['authorization_id'])
        if authorization['scope']!=PUBLISHED or digest(authorization)!=plan['authorization_sha256']:fail('VIDEO_RETENTION_PLAN_CHANGED')
        release_sha=self._release(plan);self.initialize()
        with resource_guard(self.root/'data/locks/video',self.root/'data/locks/heavy',self.folder/'lock'):
            with sqlite3.connect(self.path) as index:
                row=index.execute('SELECT plan_sha256,state,record FROM operations WHERE id=?',(plan['plan_id'],)).fetchone()
            if row and row[0]!=plan['plan_sha256']:fail('VIDEO_RETENTION_PLAN_CHANGED')
            if row and row[1]=='completed':return json.loads(row[2])
            current_job=self.store.get(plan['job_id']);current_source=self.store.get(plan['proof']['download_job_id'])
            marker=current_job['result'].get('local_original_retention_v2') if current_job else None
            if (row and marker and current_source and current_source['result'].get('local_original_retention_v2')==marker and
                    marker.get('plan_id')==plan['plan_id'] and marker.get('source_track_cleanup_sha256')==digest(plan['source_track_cleanup']) and
                    all(not Path(item['file']).exists() for item in plan['candidates'])):
                self._persist(plan,marker,'completed');return marker
            if row:
                intent=json.loads(row[2])
                if intent.get('plan')!=plan:fail('VIDEO_RETENTION_PLAN_CHANGED')
            else:
                actual,proof=self._proof(plan['job_id'],PUBLISHED,reuse_packets=plan['proof']['basis']['local_packet_evidence'])
                if actual!=plan['candidate'] or proof!=plan['proof']:fail('VIDEO_RETENTION_PLAN_CHANGED')
                tracks,track_proof=self._tracks(plan['job_id'],proof,reuse_packets=plan['source_track_cleanup']['source_to_merged_packet_evidence'])
                if [*tracks,actual]!=plan['candidates'] or any(track_proof[key]!=value for key,value in plan['source_track_cleanup'].items()
                        if key!='cleanup_recomputed_source_packet_coverage'):
                    fail('VIDEO_RETENTION_PLAN_CHANGED')
                # Exact receipt/hash/packet proof has already been computed by
                # plan(); unchanged full file hashes preserve that proof.
                intent={'version':'video-source-track-cleanup-intent-v1','plan':plan,'release_sha256':release_sha,
                    'progress':{item['file']:'pending' for item in plan['candidates']}}
            if set(intent['progress'])!={item['file'] for item in plan['candidates']} or any(
                    value not in ('pending','deleting','deleted') for value in intent['progress'].values()):fail('VIDEO_RETENTION_PLAN_CHANGED')
            observed_ctimes={}
            for item in plan['candidates']:
                path=_path(self.root,item['file']);phase=intent['progress'][item['file']]
                if path.exists():
                    if phase=='deleted' or _inspect(self.root,path,MAX_BYTES)!=item:fail('VIDEO_RETENTION_ORIGINAL_CHANGED')
                    observed_ctimes[item['file']]=path.stat().st_ctime_ns
                elif phase not in ('deleting','deleted'):fail('VIDEO_RETENTION_UNEXPECTED_MISSING_ORIGINAL')
            with self.store.transaction() as connection:
                job,binding,state,source,publication=self._context(plan['job_id'],connection)
                if any(digest(value)!=plan['proof'][key] for value,key in ((job,'job_sha256'),(source,'source_sha256'),(publication,'publication_sha256'))):
                    fail('VIDEO_RETENTION_PLAN_CHANGED')
                self._unshared(connection,[item['file'] for item in plan['candidates']],job['job_id'],source['job_id'])
                self._persist(plan,intent)
                for item in plan['candidates']:
                    path=Path(item['file']);phase=intent['progress'][item['file']]
                    if phase=='deleted':continue
                    intent['progress'][item['file']]='deleting';self._persist(plan,intent)
                    if path.exists():self._stat_unlink(item,observed_ctimes[item['file']])
                    intent['progress'][item['file']]='deleted';self._persist(plan,intent)
                result={'version':VERSION,'cleanup_mode':plan['cleanup_mode'],'plan_id':plan['plan_id'],'job_id':job['job_id'],
                    'download_job_id':source['job_id'],'source_id':job['source_id'],'scope':PUBLISHED,'file':binding['file'],
                    'file_sha256':binding['artifact_sha256'],'file_bytes':binding['file_bytes'],'local_files_deleted':3,
                    'source_tracks_deleted':2,'deleted_candidates':[{key:item[key] for key in ('file','bytes','sha256')} for item in plan['candidates']],
                    'total_logical_bytes_deleted':sum(item['bytes'] for item in plan['candidates']),
                    'source_track_cleanup_sha256':digest(plan['source_track_cleanup']),'publication_records_preserved':True,
                    'source_id_idempotency_records_preserved':True,'remote_video_deleted':False,'evidence_files_deleted':False,
                    'formal_workflow_launch_claimed':False,'authorization_id':authorization['authorization_id'],
                    'superseded_local_hold_sha256':plan['proof']['basis']['prior_local_hold_sha256'],
                    'actual_owner_response':authorization['actual_owner_response'],'owner_response_reference':authorization['owner_response_reference'],
                    'deleted_at':datetime.now(timezone.utc).isoformat()}
                for value in (job,source):connection.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',
                    (encode({**value['result'],'local_original_retention_v2':result}),time.time(),value['job_id']))
                connection.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                    (time.time(),PRINCIPAL,VERSION,job['job_id'],'exact_original_and_two_bound_tracks_only_all_receipts_retained'))
            self._persist(plan,result,'completed');return result
