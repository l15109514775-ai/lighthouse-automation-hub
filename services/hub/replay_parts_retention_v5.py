"""Automatic, exact V5 media retention after every ordinary part is public.

Root installation binds real isolated qualification and the existing global
owner retention instruction. No new per-video approval, cloud-copy claim,
upload, resubmission, remote deletion or recursive file operation lives here.
"""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib,json,math,os,re,sqlite3,stat,time,uuid
from pathlib import Path
import xml.etree.ElementTree as ET

from .archive_source_retention_v2 import _inspect,_path,_root_protected,digest
from .db import encode
from .errors import HubError
from .locks import resource_guard
from .replay_parts_v5 import (ReplayPartGroupsV5,ReplayPartVideoWeb,GROUP_JOB,GROUP_STATE,STATE,POLICY,
    part_kind,verified_delivery,reference_body)
from .video_source_retention_v2 import VideoSourceRetentionV2,VideoSourceTracksRetentionV2,PUBLISHED

VERSION='replay-parts-local-media-retention-v5'
MARKER='local_media_retention_v5'
RELEASE='replay-parts-retention-v5-release.json'
TEST_VERSION='replay-parts-retention-v5-ubuntu-tests'
TEST_COUNT=33
TEST_GROUPS={'tests.test_replay_parts_retention_v5':TEST_COUNT}
CRITICAL_SOURCES=('services/hub/replay_parts_retention_v5.py','services/hub/replay_parts_v5.py',
    'services/hub/replay_part_public_evidence_v5.py','services/hub/replay_delivery_v5.py',
    'services/hub/video_source_retention_v2.py','services/hub/archive_source_retention_v2.py',
    'services/hub/replay_part_publish_worker_v5.py','services/hub/replay_multipart_private_v4.py',
    'services/hub/replay_video_browser.py','services/hub/replay_video_web.py','services/hub/short_video_web.py',
    'services/hub/locks.py','services/hub/db.py','adapters/browser_replay_part_v5.mjs',
    'adapters/replay_part_contract_v5.mjs','adapters/replay_native_driver.mjs','adapters/replay_later_result_lookup_v2.mjs',
    'adapters/bilibili_video_guard.mjs','adapters/short_video_saved_draft_navigation_control.mjs',
    'scripts/ops/native_control_sources/short-video-native-metadata-v5.mjs','vendor/subtitle-cloud-worker/observer/store.py')

def fail(code='REPLAY_PART_RETENTION_EVIDENCE_REQUIRED'):raise HubError(code,review=True)
def _uuid(value):
    try:return str(uuid.UUID(value))==value
    except (ValueError,TypeError,AttributeError):return False
def _read(file,maximum=2_000_000):
    file=Path(file)
    if file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)):fail()
    fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        before=os.fstat(stream.fileno())
        if not 0<before.st_size<=maximum:fail()
        raw=stream.read(maximum+1);after=os.fstat(stream.fileno())
    if len(raw)!=before.st_size or any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')):fail()
    return raw


class ReplayPartsRetentionV5:
    def __init__(self,settings,store):
        self.settings,self.store=settings,store;self.root=Path(settings.root)
        self.folder=self.root/'data/replay-parts-retention-v5';self.index=self.folder/'intent.sqlite3'
        self._scans={}

    @contextmanager
    def _scope(self):
        with resource_guard(self.root/'data/locks/video',self.root/'data/locks/moderation',self.folder/'lock'):
            yield

    def initialize(self):
        _path(self.root,self.folder);self.folder.mkdir(mode=0o700,exist_ok=True);_path(self.root,self.index)
        if os.name=='posix' and self.folder.stat().st_mode&0o077:fail('REPLAY_PART_RETENTION_WORKSPACE_INVALID')
        with sqlite3.connect(self.index) as db:
            db.executescript('''PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY,group_id TEXT NOT NULL UNIQUE,
                    plan_sha256 TEXT NOT NULL,state TEXT NOT NULL,record TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS maintenance(group_id TEXT PRIMARY KEY,next_due REAL,status TEXT NOT NULL,last_code TEXT,updated_at REAL NOT NULL);''')
        if os.name=='posix':self.index.chmod(0o600)

    def _authorization(self,ident):
        value=VideoSourceRetentionV2(self.settings,self.store)._authorization(ident)
        if (value.get('version')!='video-source-retention-v2' or value.get('authorization_id')!=ident or value.get('scope')!=PUBLISHED or
                value.get('artifact') is not None or value.get('formal_workflow_launch_claimed') is not False or
                value.get('publication_authorized') is not False or value.get('remote_video_deletion_authorized') is not False or
                not isinstance(value.get('owner_response_reference'),str) or not value['owner_response_reference'].strip() or
                not isinstance(value.get('actual_owner_response'),str) or any(s not in value['actual_owner_response'] for s in ('录播','B站','保留'))):
            fail('REPLAY_PART_RETENTION_REAL_OWNER_INSTRUCTION_REQUIRED')
        return value

    def _release(self):
        file=self.root/'config'/RELEASE
        try:
            _root_protected(file);raw=_read(file,200000);record=json.loads(raw)
            if (record['version']!='replay-parts-retention-release-v5' or record['enabled'] is not True or
                    record['target_site_uid']!=str(self.settings.config['target_bilibili_site_uid']) or
                    record['scope']!='all_parts_verified_public_exact_local_media_only' or not _uuid(record['authorization_id'])):raise ValueError()
            owner=self._authorization(record['authorization_id'])
            if record['authorization_sha256']!=digest(owner):raise ValueError()
            ref=record['test_attestation'];proof=Path(ref['file']);_root_protected(proof);praw=_read(proof)
            if hashlib.sha256(praw).hexdigest()!=ref['sha256']:raise ValueError()
            value=json.loads(praw);junit=Path(value['junit_file']);_root_protected(junit);jraw=_read(junit)
            cases=ET.fromstring(jraw).findall('.//testcase');groups={}
            for case in cases:groups[case.get('classname')]=groups.get(case.get('classname'),0)+1
            if (value['version']!=TEST_VERSION or value['tests']!=TEST_COUNT or len(cases)!=TEST_COUNT or groups!=TEST_GROUPS or
                    hashlib.sha256(jraw).hexdigest()!=value['junit_sha256'] or
                    any(case.find(tag) is not None for case in cases for tag in ('failure','error','skipped')) or
                    any(value.get(k)!=0 for k in ('exit_code','failures','errors','skipped')) or value['network']!='none' or
                    any(value.get(k) is not False for k in ('application_data_mounted','production_profile_mounted','secrets_environment_injected','oom_killed')) or
                    any(value.get(k) is not True for k in ('full_suite_passed','no_pid_or_oom_denial','runtime_matches_declared_candidate',
                        'mounts_match_isolated_allowlist','mounted_sources_unchanged'))):raise ValueError()
            sources=record['source_files'];code=Path(__file__).resolve().parents[2]
            if not set(CRITICAL_SOURCES)<=set(sources) or not 21<=len(sources)<=160 or sources!=value['source_files']:raise ValueError()
            for name,checksum in sources.items():
                if (not re.fullmatch(r'(?:services/hub|adapters|scheduler|scripts/ops/native_control_sources|vendor/subtitle-cloud-worker/observer)/[A-Za-z0-9_./-]+\.(?:py|mjs)',name) or
                        '..' in Path(name).parts or not re.fullmatch('[a-f0-9]{64}',checksum) or
                        hashlib.sha256(_read(_path(code,code/name),500000)).hexdigest()!=checksum):raise ValueError()
            return owner,hashlib.sha256(raw).hexdigest()
        except HubError:raise
        except (OSError,ValueError,KeyError,TypeError,AttributeError,ET.ParseError,sqlite3.Error):fail('REPLAY_PART_RETENTION_RELEASE_REQUIRED')

    def _retirement_record(self,reference,folder):
        if not isinstance(reference,dict) or set(reference)!={'file','bytes','sha256'}:return None
        file=_path(self.root,Path(reference['file']))
        if file.parent!=folder or type(reference['bytes']) is not int or not 0<reference['bytes']<=2_000_000:return None
        raw=_read(file,2_000_000)
        if len(raw)!=reference['bytes'] or hashlib.sha256(raw).hexdigest()!=reference['sha256']:return None
        value=json.loads(raw)
        return value if isinstance(value,dict) else None

    def _proven_history(self,job,source,result):
        """Recognize only explicitly audited superseded/retired source history."""
        if job.get('state')!='failed' or job.get('lease_owner') or job.get('lease_until'):return False
        name=job['job_type'];error=job.get('error_code');stage=job.get('stage')
        if name.endswith('_superseded'):
            if error!='REPLAY_PARTS_SUPERSEDED_BY_NEW_LIMITS' or stage not in ('superseded','superseded_by_web_upload_limit_16gb_10h'):return False
            for audit in self.store.query("SELECT outcome FROM audit WHERE action='archive_superseded_child_types' AND target=?",(source['job_id'],)):
                try:children=json.loads(audit['outcome']).get('children',[])
                except (ValueError,TypeError,AttributeError):continue
                if any(isinstance(item,dict) and item.get('job_id')==job['job_id'] and item.get('new')==name and
                        item.get('old')==name[:-len('_superseded')] for item in children):return True
                if name=='replay_parts_publish_parent_v5_superseded':
                    snapshots=[]
                    for item in children:
                        if not isinstance(item,dict) or not isinstance(item.get('job_id'),str):break
                        child=self.store.get(item['job_id'])
                        if (not child or child['source_platform']!='youtube' or child['source_id']!=source['source_id'] or
                                child['job_type']!=item.get('new') or item.get('new')!=item.get('old','')+'_superseded' or
                                child['state']!='failed' or child['error_code']!='REPLAY_PARTS_SUPERSEDED_BY_NEW_LIMITS' or
                                child['stage']!='superseded' or child['lease_owner'] or child['lease_until']):break
                        snapshots.append(child)
                    expected=(result.get('replay_parts_v5') or {}).get('part_count')
                    if expected==len(children)==len(snapshots) and expected:return True
            return False
        retired=re.fullmatch(r'(.+)_retired_v5_([a-f0-9]{12})',name)
        if retired:
            if error!='REPLAY_PART_GROUP_SUPERSEDED' or stage!='superseded_after_interrupted_upload':return False
            marker=result.get('replay_part_retirement_v5')
            if not isinstance(marker,dict):return False
            plan=marker.get('plan_sha256')
            if (marker.get('version')!='replay-part-interrupted-group-retirement-v5' or not isinstance(plan,str) or
                    not plan.startswith(retired.group(2)) or
                    marker.get('platform_retry_performed') is not False or marker.get('new_attempts')!=0):return False
            record=self._retirement_record(marker.get('record'),self.root/'media'/source['job_id']/'replay-part-retirement-v5'/plan)
            if (not record or record.get('version')!='retire-interrupted-replay-part-group-v5' or
                    record.get('source_id')!=source['source_id'] or
                    record.get('download_job_id')!=source['job_id']):return False
            snapshots={item.get('job_id'):item for item in record.get('jobs',[]) if isinstance(item,dict)}
            return snapshots.get(job['job_id']) is not None and digest(snapshots[job['job_id']])==marker.get('old_job_sha256')
        if name=='video_process':
            if error!='YOUTUBE_LEGACY_VIDEO_PROCESS_SUPERSEDED' or stage!='superseded_by_replay_part_group':return False
            marker=result.get('youtube_legacy_video_process_superseded_v5')
            if not isinstance(marker,dict):return False
            plan=marker.get('plan_sha256')
            if (marker.get('version')!='youtube-legacy-video-process-superseded-v5' or
                    not isinstance(plan,str) or not re.fullmatch('[a-f0-9]{64}',plan) or
                    marker.get('remote_retry_performed') is not False or
                    marker.get('publication_created') is not False):return False
            record=self._retirement_record(marker.get('record'),self.root/'media'/source['job_id']/'legacy-source-process-retirement-v5')
            if (not record or record.get('version')!='retire-legacy-source-video-process-v5' or
                    record.get('source_id')!=source['source_id'] or
                    record.get('download_job_id')!=source['job_id'] or
                    not isinstance(record.get('job'),dict) or
                    record['job'].get('job_id')!=job['job_id'] or
                    digest(record['job'])!=marker.get('old_job_sha256')):return False
            for audit in self.store.query("SELECT outcome FROM audit WHERE action='youtube_legacy_video_process_superseded_v5' AND target=?",(job['job_id'],)):
                try:value=json.loads(audit['outcome'])
                except (ValueError,TypeError):continue
                if isinstance(value,dict) and value.get('plan_sha256')==plan:return True
            return False
        return False

    def _context(self,group_id,*,candidates=None):
        if not _uuid(group_id):fail()
        parent=self.store.get(group_id)
        if (not parent or parent['job_type']!=GROUP_JOB or parent['policy_version']!=POLICY or parent['source_platform']!='youtube' or
                parent['state']!='completed' or parent.get('lease_owner') or parent.get('lease_until') or
                parent['payload']['site_uid']!=str(self.settings.config['target_bilibili_site_uid']) or
                parent['result'][GROUP_STATE]['state']!='completed' or
                parent['result'][GROUP_STATE]['completed_parts']!=parent['payload']['part_count']):fail('REPLAY_PART_RETENTION_COMPLETE_GROUP_REQUIRED')
        rows=self.store.query('SELECT * FROM replay_part_children_v5 WHERE parent_job_id=? ORDER BY part_index',(group_id,))
        if not 1<=len(rows)==parent['payload']['part_count']<=32:fail()
        source=self.store.get(parent['payload']['download_job_id'])
        context=verified_delivery(self.settings,self.store,source['job_id'])
        if (source['state']!='completed' or source.get('lease_owner') or source.get('lease_until') or
                context['manifest']['sha256']!=parent['payload']['parts_manifest_sha256'] or parent['source_id']!=source['source_id'] or
                len(context['delivery']['outputs'])!=len(rows)):fail()
        first=parent['result'].get('replay_parts_first_completion_v5')
        completion=reference_body(self.root,first,expected=self.root/'media'/group_id/'replay-parts-v5/first-completion.json')
        if (completion['version']!='replay-parts-first-completion-v5' or completion['parent_job_id']!=group_id or
                completion['group_binding_sha256']!=digest(parent['payload']) or completion['summary']['state']!='completed'):fail()
        selected={context['asset']['file']:{k:context['asset'][k] for k in ('file','bytes','sha256')}}
        for output in context['delivery']['outputs']:selected[output['file']]={k:output[k] for k in ('file','bytes','sha256')}
        area=self.root/'media'/source['job_id']/'youtube-whole-v4';expected={str(area/'original.mp4')}
        if context['delivery']['processing_model']=='lossless_segmented_v4':expected|={str(area/'parts-v4'/f'part-{i:03d}.mp4') for i in range(len(rows))}
        if set(selected)!=expected or len(selected)>33:fail('REPLAY_PART_RETENTION_EXACT_MEDIA_REQUIRED')
        if candidates is not None and [{k:v[k] for k in ('file','bytes','sha256')} for v in candidates]!=list(selected.values()):fail('REPLAY_PART_RETENTION_PLAN_CHANGED')
        allowed={group_id,source['job_id']};children=[];publications=[]
        for index,row in enumerate(rows):
            service=ReplayPartVideoWeb(self.settings,self.store,part_index=index);job,binding,state=service.snapshot(row['child_job_id'],write=False)
            output=context['delivery']['outputs'][index]
            observed=state['result_observation']
            if (row['part_index']!=index or row['state']!='completed' or job['state']!='completed' or state['state']!='completed' or
                    job.get('lease_owner') or job.get('lease_until') or state.get('test_retention_policy') or
                    not re.fullmatch('BV[A-Za-z0-9]{10}',row['external_id'] or '') or job['external_publish_id']!=row['external_id'] or
                    state['result_observation']['external_id']!=row['external_id'] or binding['source_contract']['output']!=output or
                    binding['source_contract']['delivery_manifest']!=context['manifest'] or
                    observed.get('version')!=service.RESULT_OBSERVATION_VERSION or observed.get('state')!='completed' or
                    observed.get('public_accessible') is not True or observed.get('origin')!='later_native_lookup_reconciled_v5' or
                    observed.get('same_submit_attempt_observed') is not False or observed.get('site_uid')!=binding['site_uid'] or
                    observed.get('source_url')!=binding['source_url'] or observed.get('metadata_sha256')!=binding['metadata_sha256'] or
                    state['public_coverage'].get('complete_part_playability_verified') is not True or
                    any(binding[a]!=output[b] for a,b in (('file','file'),('file_bytes','bytes'),('artifact_sha256','sha256'),('duration_seconds','duration')))):
                fail('REPLAY_PART_RETENTION_CHILD_INCOMPLETE')
            pubs=self.store.query('SELECT * FROM publications WHERE job_id=? AND idempotency_key=?',(job['job_id'],state['key']))
            if (len(pubs)!=1 or pubs[0]['state']!='completed' or pubs[0]['kind']!=part_kind(index) or pubs[0]['external_id']!=row['external_id'] or
                    pubs[0]['external_url']!='https://www.bilibili.com/video/'+row['external_id'] or pubs[0]['source_id']!=source['source_id'] or
                    pubs[0]['account_id']!='site_uid:'+parent['payload']['site_uid']):fail()
            stable=deepcopy(state);stable.pop('result_observation',None);stable.pop('public_coverage',None)
            children.append({'job_id':job['job_id'],'payload_sha256':digest(binding),'attempts':job['attempts'],'state_sha256':digest(stable)})
            publications.append({k:v for k,v in pubs[0].items() if k!='updated_at'});allowed.add(job['job_id'])
        all_publications=self.store.query("SELECT * FROM publications WHERE source_platform='youtube' AND source_id=?",(source['source_id'],))
        if (len(all_publications)!=len(publications) or
                sorted(encode({k:v for k,v in p.items() if k!='updated_at'}) for p in all_publications)!=sorted(encode(p) for p in publications)):
            fail('REPLAY_PART_RETENTION_FOREIGN_PUBLICATION')
        if self.store.query("SELECT 1 FROM source_tombstones WHERE source_platform='youtube' AND source_id=?",(source['source_id'],)):fail('REPLAY_PART_RETENTION_SOURCE_TOMBSTONED')
        def uses(value):
            if isinstance(value,dict):return any(uses(v) for v in value.values())
            if isinstance(value,list):return any(uses(v) for v in value)
            return isinstance(value,str) and value in selected
        for job in self.store.query('SELECT job_id,source_platform,source_id,job_type,policy_version,queue,state,error_code,stage,attempts,lease_owner,lease_until,payload,result FROM jobs'):
            payload=json.loads(job['payload']);result=json.loads(job['result'])
            if job['job_id'] in allowed:continue
            if self._proven_history(job,source,result):continue
            if uses(payload) or uses(result):fail('REPLAY_PART_RETENTION_SHARED_MEDIA')
            lookup=(job['source_platform']=='bilibili' and job['job_type']=='replay_part_lookup_v5' and job['policy_version']==POLICY and payload.get('parent_publish_job_id') in allowed)
            if lookup and job['state'] in ('completed','failed','auth_expired','awaiting_review') and not job['lease_owner'] and not job['lease_until']:continue
            related=(job['source_platform']=='youtube' and job['source_id']==source['source_id'] or payload.get('parent_publish_job_id') in allowed)
            if (related and job['state']!='completed' or job['queue']=='video' and job['state']=='processing'):
                fail('REPLAY_PART_RETENTION_SOURCE_IN_USE')
        identity={'group_payload_sha256':digest(parent['payload']),'first_completion':first,'source_payload_sha256':digest(source['payload']),
            'source_binding_sha256':digest(context['state']['binding']),'asset_sha256':digest(context['asset']),'receipt_sha256':digest(context['state']['receipt']),
            'delivery_manifest':context['manifest'],'children':children,'publications':publications}
        return {'parent':parent,'source':source,'children':rows,'context':context,'files':list(selected.values()),'identity':identity,'completion':completion}

    def _inspect(self,file,maximum):
        item=_inspect(self.root,file,maximum);item['ctime_ns']=Path(file).stat().st_ctime_ns;return item

    def _stat_matches(self,item):
        file=_path(self.root,item['file']);current=file.stat();parent=file.parent.stat()
        return (stat.S_ISREG(current.st_mode) and current.st_nlink==1 and
            (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns,current.st_ctime_ns)==
            (item['device'],item['inode'],item['bytes'],item['mtime_ns'],item['ctime_ns']) and
            (parent.st_dev,parent.st_ino)==(item['parent_device'],item['parent_inode']))

    def plan(self,group_id):
        self.initialize();owner,release_sha=self._release()
        with self._scope():
            context=self._context(group_id);public=self._public(group_id);candidates=[]
            with resource_guard(self.root/'data/locks/heavy'):
                for item in context['files']:
                    actual=self._inspect(Path(item['file']),context['context']['plan']['whole_budget']['merged_max_bytes'])
                    if any(actual[k]!=item[k] for k in ('file','bytes','sha256')):fail('REPLAY_PART_RETENTION_MEDIA_CHANGED')
                    candidates.append(actual)
            if self._context(group_id)['identity']!=context['identity'] or any(not self._stat_matches(i) for i in candidates):fail('REPLAY_PART_RETENTION_PLAN_CHANGED')
            plan={'version':VERSION,'plan_id':str(uuid.uuid4()),'group_id':group_id,'download_job_id':context['source']['job_id'],
                'source_id':context['source']['source_id'],'authorization_id':owner['authorization_id'],'authorization_sha256':digest(owner),
                'release_sha256':release_sha,'identity':context['identity'],'candidates':candidates,'public_parts':public,
                'historical_confirmation':True,'current_access_requeried':False,'external_requests':0,
                'created_at':datetime.now(timezone.utc).isoformat(),'remote_video_deleted':False,'cloud_backup_claimed':False,'human_review':False}
            result={**plan,'plan_sha256':digest(plan)}
            # This private in-memory entry is created only after real complete
            # hashes and final fingerprints. No serialized passed flag can
            # supply it. A restarted process rehashes remaining files once.
            self._scans[plan['plan_id']]=result['plan_sha256']
            return result

    def _public(self,group_id):
        from .replay_part_public_evidence_v5 import validate_result
        from .replay_video_web import WORKER_PRINCIPAL
        from .short_video_web import PRINCIPAL as OPERATOR_PRINCIPAL
        context=self._context(group_id);completion=context['completion'];values=[]
        if len(completion['parts'])!=len(context['children']) or len(completion['summary']['publications'])!=len(context['children']):fail()
        for row in context['children']:
            service=ReplayPartVideoWeb(self.settings,self.store,part_index=row['part_index']);job,binding,state=service.snapshot(row['child_job_id'],write=False)
            originals=[p for p in completion['parts'] if p['job_id']==job['job_id']]
            summaries=[p for p in completion['summary']['publications'] if p['job_id']==job['job_id']]
            if len(originals)!=1 or len(summaries)!=1:fail()
            original=originals[0];observation=original['result_observation'];coverage=original['public_coverage'];summary=summaries[0]
            if (original['binding_sha256']!=digest(binding) or observation['external_id']!=row['external_id'] or
                    summary['public_result_sha256']!=digest(observation) or summary['public_coverage_sha256']!=digest(coverage) or summary['completed'] is not True):fail()
            accepted=[]
            for audit in self.store.query("SELECT at,principal,outcome FROM audit WHERE action='replay_part_later_public_result_v5' AND target=? ORDER BY at",(job['job_id'],)):
                try:outcome=json.loads(audit['outcome'])
                except (ValueError,TypeError):continue
                at=audit['at']
                if (audit['principal'] in (WORKER_PRINCIPAL,OPERATOR_PRINCIPAL,'worker:replay-part-lookup-v5') and
                        type(at) in (int,float) and math.isfinite(at) and 0<=at<=time.time() and
                        outcome.get('external_id')==row['external_id'] and outcome.get('native_result_evidence')==observation['native_result_evidence'] and
                        outcome.get('same_submit_attempt_observed') is False and outcome.get('human_review') is False):accepted.append(at)
            if not accepted:fail('REPLAY_PART_RETENTION_ORIGINAL_ACCEPTANCE_AUDIT_REQUIRED')
            historical=deepcopy(state);historical['result_observation']=observation;historical['public_coverage']=coverage
            at=min(accepted)
            actual=validate_result(service,job,binding,historical,observation,validation_time=at)
            if actual!=coverage:fail('REPLAY_PART_RETENTION_PUBLIC_COVERAGE_CHANGED')
            values.append({'part_index':row['part_index'],'job_id':job['job_id'],'external_id':row['external_id'],
                'historical_confirmation':True,'current_access_requeried':False,'validation_time_source':'first_matching_record_result_audit',
                'original_accepted_at':at,'first_completion':context['identity']['first_completion'],'coverage':coverage})
        return values

    def _persist(self,plan,intent,state='deleting'):
        with sqlite3.connect(self.index) as db:
            db.execute('PRAGMA synchronous=FULL')
            old=db.execute('SELECT id,plan_sha256 FROM operations WHERE group_id=?',(plan['group_id'],)).fetchone()
            if old and old!=(plan['plan_id'],plan['plan_sha256']):fail('REPLAY_PART_RETENTION_OTHER_PLAN_PENDING')
            db.execute('INSERT INTO operations VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,record=excluded.record',
                (plan['plan_id'],plan['group_id'],plan['plan_sha256'],state,encode(intent)))

    def apply(self,plan):
        if (plan.get('version')!=VERSION or not _uuid(plan.get('plan_id')) or
                digest({k:v for k,v in plan.items() if k!='plan_sha256'})!=plan.get('plan_sha256')):fail('REPLAY_PART_RETENTION_PLAN_CHANGED')
        self.initialize();owner,release_sha=self._release()
        if owner['authorization_id']!=plan['authorization_id'] or digest(owner)!=plan['authorization_sha256'] or release_sha!=plan['release_sha256']:fail('REPLAY_PART_RETENTION_PLAN_CHANGED')
        with self._scope():
            rows=[]
            with sqlite3.connect(self.index) as index:rows=index.execute('SELECT id,plan_sha256,state,record FROM operations WHERE group_id=?',(plan['group_id'],)).fetchall()
            if rows and rows[0][:2]!=(plan['plan_id'],plan['plan_sha256']):fail('REPLAY_PART_RETENTION_PLAN_CHANGED')
            if rows and rows[0][2]=='completed':return json.loads(rows[0][3])
            intent=json.loads(rows[0][3]) if rows else {'version':VERSION,'plan':plan,'progress':{i['file']:'pending' for i in plan['candidates']}}
            if intent.get('plan')!=plan or set(intent['progress'])!={i['file'] for i in plan['candidates']} or any(v not in ('pending','deleting','deleted') for v in intent['progress'].values()):fail()
            context=self._context(plan['group_id'],candidates=plan['candidates'])
            if context['identity']!=plan['identity']:fail('REPLAY_PART_RETENTION_PLAN_CHANGED')
            marker=context['parent']['result'].get(MARKER)
            if (rows and marker and context['source']['result'].get(MARKER)==marker and marker.get('plan_id')==plan['plan_id'] and
                    marker.get('authorization_id')==owner['authorization_id'] and marker.get('deleted_candidates')==
                    [{k:i[k] for k in ('file','bytes','sha256')} for i in plan['candidates']] and
                    all(intent['progress'][i['file']]=='deleted' and not Path(i['file']).exists() for i in plan['candidates'])):
                self._persist(plan,marker,'completed');return marker
            with resource_guard(self.root/'data/locks/heavy'):
                for item in plan['candidates']:
                    path=_path(self.root,item['file']);phase=intent['progress'][item['file']]
                    if path.exists():
                        if phase=='deleted' or not self._stat_matches(item):fail('REPLAY_PART_RETENTION_MEDIA_CHANGED')
                        if self._scans.get(plan['plan_id'])!=plan['plan_sha256'] and self._inspect(path,context['context']['plan']['whole_budget']['merged_max_bytes'])!=item:fail('REPLAY_PART_RETENTION_MEDIA_CHANGED')
                    elif phase not in ('deleting','deleted'):fail('REPLAY_PART_RETENTION_UNEXPECTED_MISSING_MEDIA')
            self._scans[plan['plan_id']]=plan['plan_sha256'];self._persist(plan,intent)
            public=self._public(plan['group_id']);context=self._context(plan['group_id'],candidates=plan['candidates'])
            if context['identity']!=plan['identity']:fail('REPLAY_PART_RETENTION_PLAN_CHANGED')
            current_owner,current_release=self._release()
            if current_owner!=owner or current_release!=release_sha:fail('REPLAY_PART_RETENTION_PLAN_CHANGED')
            with resource_guard(self.root/'data/locks/bilibili-video-profile',self.root/'data/locks/heavy'):
                with self.store.transaction() as db:
                    # The immediate transaction blocks creation of new media
                    # dependants between the last shared-reference check and
                    # exact unlinks. Separate FULL ledger survives rollback.
                    actual=self._context(plan['group_id'],candidates=plan['candidates'])
                    if actual['identity']!=plan['identity']:fail('REPLAY_PART_RETENTION_PLAN_CHANGED')
                    self._persist(plan,intent)
                    engine=VideoSourceTracksRetentionV2(self.settings,self.store)
                    for item in plan['candidates']:
                        file=Path(item['file']);phase=intent['progress'][item['file']]
                        if phase=='deleted':continue
                        intent['progress'][item['file']]='deleting';self._persist(plan,intent)
                        if file.exists():engine._stat_unlink(item,item['ctime_ns'])
                        intent['progress'][item['file']]='deleted';self._persist(plan,intent)
                    receipt={'version':VERSION,'plan_id':plan['plan_id'],'group_id':plan['group_id'],'download_job_id':plan['download_job_id'],
                        'source_id':plan['source_id'],'authorization_id':owner['authorization_id'],'owner_response_reference':owner['owner_response_reference'],
                        'actual_owner_response':owner['actual_owner_response'],'deleted_candidates':[{k:i[k] for k in ('file','bytes','sha256')} for i in plan['candidates']],
                        'local_files_deleted':len(plan['candidates']),'total_logical_bytes_deleted':sum(i['bytes'] for i in plan['candidates']),
                        'public_parts':public,'deleted_at':datetime.now(timezone.utc).isoformat(),'remote_video_deleted':False,
                        'cloud_backup_claimed':False,'historical_confirmation':True,'current_access_requeried':False,'external_requests':0,'source_id_idempotency_records_preserved':True,'evidence_files_deleted':False,'human_review':False}
                    for ident in (plan['group_id'],plan['download_job_id']):
                        job=self.store.get(ident);db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',(encode({**job['result'],MARKER:receipt}),time.time(),ident))
                    db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                        (time.time(),VERSION,VERSION,plan['group_id'],'all_parts_public_exact_local_media_deleted_metadata_and_bvs_retained'))
            self._persist(plan,receipt,'completed');return receipt


def retention_tick(settings,store):
    service=ReplayPartsRetentionV5(settings,store)
    if not (settings.root/'config'/RELEASE).exists():return {'version':VERSION,'state':'disabled','files_deleted':0,'external_requests':0}
    service.initialize();service._release()
    with sqlite3.connect(service.index) as db:pending=db.execute("SELECT record FROM operations WHERE state<>'completed' ORDER BY rowid LIMIT 1").fetchone()
    plan=json.loads(pending[0])['plan'] if pending else None
    if plan:group_id=plan['group_id']
    else:
        rows=store.query("SELECT job_id FROM jobs WHERE job_type=? AND policy_version=? AND state='completed' ORDER BY created_at,job_id",(GROUP_JOB,POLICY))
        done=[]
        with sqlite3.connect(service.index) as db:done=[r[0] for r in db.execute("SELECT group_id FROM operations WHERE state='completed'")]
        with sqlite3.connect(service.index) as db:cooling={r[0] for r in db.execute('SELECT group_id FROM maintenance WHERE next_due>?',(time.time(),))}
        ids=[r['job_id'] for r in rows if r['job_id'] not in done and r['job_id'] not in cooling]
        if not ids:return {'version':VERSION,'state':'idle','files_deleted':0,'external_requests':0}
        group_id=ids[0]
    with sqlite3.connect(service.index) as db:
        row=db.execute('SELECT next_due FROM maintenance WHERE group_id=?',(group_id,)).fetchone()
        if row and row[0] and row[0]>time.time():return {'version':VERSION,'state':'cooldown','files_deleted':0,'external_requests':0}
        db.execute('PRAGMA synchronous=FULL')
        db.execute("INSERT INTO maintenance VALUES(?,?,'running',NULL,?) ON CONFLICT(group_id) DO UPDATE SET next_due=excluded.next_due,status='running',updated_at=excluded.updated_at",(group_id,time.time()+300,time.time()))
    try:
        plan=plan or service.plan(group_id)
        receipt=service.apply(plan)
    except HubError as exc:
        with sqlite3.connect(service.index) as db:
            db.execute('PRAGMA synchronous=FULL');db.execute("UPDATE maintenance SET status='held',last_code=?,next_due=?,updated_at=? WHERE group_id=?",(exc.code,time.time()+300,time.time(),group_id))
        if exc.code!='HEAVY_RESOURCE_BUSY':store.event('review.required',{'job_id':group_id,'code':exc.code,'cleanup_only':True},dedup_key=VERSION+':'+group_id+':'+exc.code)
        with sqlite3.connect(service.index) as db:progress=db.execute('SELECT record FROM operations WHERE group_id=?',(group_id,)).fetchone()
        record=json.loads(progress[0]) if progress else {};known=sum(phase in ('deleting','deleted') and not Path(file).exists() for file,phase in record.get('progress',{}).items())
        return {'version':VERSION,'state':'held','code':exc.code,'files_deleted':known,'deletions_complete':False,'partial_progress_preserved':True}
    with sqlite3.connect(service.index) as db:
        db.execute('PRAGMA synchronous=FULL');db.execute("UPDATE maintenance SET status='completed',next_due=NULL,last_code=NULL,updated_at=? WHERE group_id=?",(time.time(),group_id))
    return {'version':VERSION,'state':'completed','files_deleted':receipt['local_files_deleted'],'receipt':receipt}
