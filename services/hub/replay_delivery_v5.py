"""Verified local delivery outputs for independent posts; never a publication grant.

An actual fitting master is referenced in place. Only a genuinely oversized
master enters the existing lossless segment engine. The immutable manifest and
its raw-byte hash are the cross-language identity, not a float-array digest.
"""
from copy import deepcopy
import hashlib,json,os,stat,time,uuid
from pathlib import Path

from adapters.media_tools import empty_evidence
from .db import encode
from .errors import HubError
from .locks import resource_guard
from .media_processing import sync_directory
from .replay_multipart_v4 import MultipartMediaProcessingV4,digest

VERSION='replay-parts-delivery-v5'
MANIFEST_VERSION='replay-parts-delivery-manifest-v5'
TABLE='replay_delivery_outputs_v5'
SINGLE='single_whole_passthrough_v5'
SEGMENTED='lossless_segmented_v4'

def fail(code='REPLAY_DELIVERY_V5_INVALID'):raise HubError(code,review=True)

def actual_processing_model(source_bytes,source_seconds):
    if (type(source_bytes) is not int or source_bytes<=0 or type(source_seconds) not in (int,float) or
            not 0<source_seconds<float('inf')):fail()
    return SINGLE if source_bytes<=14_000_000_000 and source_seconds<=36000 else SEGMENTED

def _read(file,maximum=2_097_152):
    if file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)):fail()
    before=file.stat();fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0))
    with os.fdopen(fd,'rb') as stream:
        opened=os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or not 0<opened.st_size<=maximum:fail()
        raw=stream.read(maximum+1);closed=os.fstat(stream.fileno())
    after=file.stat();keys=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
    cross=keys if os.name!='nt' else keys[:-1]
    if (any(getattr(before,k)!=getattr(opened,k) or getattr(closed,k)!=getattr(after,k) for k in cross) or
            any(getattr(before,k)!=getattr(after,k) or getattr(opened,k)!=getattr(closed,k) for k in keys)):fail()
    try:value=json.loads(raw)
    except (ValueError,UnicodeError):fail()
    return value,{'file':str(file),'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}

def _immutable(file,value):
    raw=encode(value).encode()
    if len(raw)>2_097_152:fail()
    if file.exists():
        existing,ref=_read(file)
        if existing!=value or ref['bytes']!=len(raw) or ref['sha256']!=hashlib.sha256(raw).hexdigest():fail('REPLAY_DELIVERY_V5_MANIFEST_CHANGED')
        return ref
    temporary=file.parent/('.'+file.name+'.'+str(uuid.uuid4())+'.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        try:os.link(temporary,file)
        except FileExistsError:
            existing,_=_read(file)
            if existing!=value:fail('REPLAY_DELIVERY_V5_MANIFEST_CHANGED')
        sync_directory(file.parent)
    finally:
        if temporary.exists():temporary.unlink();sync_directory(file.parent)
    actual,ref=_read(file)
    if actual!=value:fail('REPLAY_DELIVERY_V5_MANIFEST_CHANGED')
    return ref


class ReplayDeliveryV5:
    def __init__(self,settings,store,*,policy,plan,check=lambda:None):
        self.settings,self.store=settings,store
        self.processor=MultipartMediaProcessingV4(settings,store,policy=policy,plan=plan,check=check)
        self.policy=self.processor.policy;self.plan=self.processor.whole_plan;self.plan_sha=digest(self.plan)
        if self.policy.get('prefer_single_whole_passthrough_v5') is not True or self.plan.get('processing_model')!='whole_or_lossless_segments_v5':fail()

    def _job(self,job):
        self.processor.check();current=self.store.get(job['job_id'])
        if (not current or current['state']!='processing' or not job.get('lease_owner') or
                current['lease_owner']!=job['lease_owner'] or (current['lease_until'] or 0)<=time.time() or
                job['job_type']!='youtube_replay_download_v4' or job['queue']!='video' or job.get('source_platform')!='youtube' or
                job.get('policy_version')!='replay-fifo-v4' or current['payload']!=job['payload'] or
                job['source_id']!=self.plan['source_id'] or job['payload'].get('whole_replay_plan')!=self.plan or
                job['payload'].get('whole_replay_plan_sha256')!=self.plan_sha):raise RuntimeError('LEASE_LOST')

    def _ledger(self,job,manifest,ref):
        self._job(job)
        with self.store.transaction() as db:
            current=db.execute('SELECT state,lease_owner,lease_until,payload FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if (current['state']!='processing' or current['lease_owner']!=job['lease_owner'] or (current['lease_until'] or 0)<=time.time() or
                    json.loads(current['payload'])!=job['payload']):raise RuntimeError('LEASE_LOST')
            db.execute('''CREATE TABLE IF NOT EXISTS replay_delivery_outputs_v5(job_id TEXT NOT NULL,output_index INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,manifest_sha256 TEXT NOT NULL,state TEXT NOT NULL,
                external_publish_id TEXT,record TEXT NOT NULL,PRIMARY KEY(job_id,output_index))''')
            old=db.execute('SELECT * FROM '+TABLE+' WHERE job_id=? ORDER BY output_index',(job['job_id'],)).fetchall()
            if old and len(old)!=len(manifest['outputs']):fail('REPLAY_DELIVERY_V5_LEDGER_CHANGED')
            for output in manifest['outputs']:
                record=encode(output);row=next((r for r in old if r['output_index']==output['index']),None)
                if row:
                    if (row['record']!=record or row['idempotency_key']!=output['part_idempotency_key'] or
                            row['manifest_sha256']!=ref['sha256'] or row['state']!='prepared' or row['external_publish_id'] is not None):
                        fail('REPLAY_DELIVERY_V5_EXTERNAL_STATE_RECONCILIATION_REQUIRED')
                else:db.execute('INSERT INTO '+TABLE+' VALUES(?,?,?,?,?,?,?)',
                    (job['job_id'],output['index'],output['part_idempotency_key'],ref['sha256'],'prepared',None,record))

    def run(self,job,asset):
        self._job(job);p=self.processor
        expected=self.settings.root/'media'/job['job_id']/'youtube-whole-v4'/'original.mp4'
        if Path(asset['file'])!=expected:fail('REPLAY_DELIVERY_V5_SOURCE_PATH_INVALID')
        directory=expected.parent/'delivery-v5';document=directory/'manifest.json'
        # The existing splitter takes its own heavy lock. Do not nest that lock.
        p.active_job=job;p.next_lease_check=0
        try:
            with resource_guard(self.settings.root/'data/locks/heavy'):
                source=p._source(job,asset);details=p._inspect(source);model=actual_processing_model(details['bytes'],details['duration'])
                previous,oldref=_read(document) if document.exists() else (None,None)
                if previous is not None and previous.get('processing_model')!=model:fail('REPLAY_DELIVERY_V5_MANIFEST_CHANGED')
        finally:p.active_job=None
        split=None
        if model==SEGMENTED and previous is None:split=p.run(job,asset)
        p.active_job=job;p.next_lease_check=0
        try:
            with resource_guard(self.settings.root/'data/locks/heavy'):
                self._job(job);source=p._source(job,asset);details=p._inspect(source)
                if actual_processing_model(details['bytes'],details['duration'])!=model:fail('REPLAY_DELIVERY_V5_SOURCE_CHANGED')
                packets=p._packets(source,details['stream_types'],timeout=self.policy['max_processing_seconds'])
                if packets!=asset['packet_evidence']:fail('REPLAY_DELIVERY_V5_PACKET_MISMATCH')
                if directory.resolve()!=directory or any(v.is_symlink() for v in (directory,*directory.parents)):fail()
                directory.mkdir(mode=0o700,exist_ok=True)
                if os.name=='posix' and directory.stat().st_mode&0o077:fail()
                source_record={'file':str(source),'bytes':details['bytes'],'sha256':asset['sha256'],'duration':details['duration'],
                    'stream_config':details['stream_config'],'packet_evidence':packets}
                if model==SINGLE:
                    proof={'version':'verified-whole-passthrough-proof-v5','job_id':job['job_id'],'whole_plan_sha256':self.plan_sha,
                        'source':source_record,'inspection':json.loads(encode(details)),'human_review':False,'publishable':False,'media_copied':False}
                    evidence=_immutable(directory/'whole-proof.json',proof)
                    raw_outputs=[source_record];coverage=[{'index':0,'packet_before':empty_evidence(),'packet_after':packets}]
                else:
                    splitfile=Path(split['manifest']) if split else expected.parent/'parts-v4'/'manifest.json'
                    splitbody,evidence=_read(splitfile)
                    if (splitbody.get('version')!=p.VERSION or splitbody.get('source_sha256')!=asset['sha256'] or
                            splitbody.get('whole_plan_sha256')!=self.plan_sha or splitbody.get('source_packets')!=packets or
                            splitbody.get('packet_coverage_verified') is not True):fail('REPLAY_DELIVERY_V5_SPLIT_PROOF_CHANGED')
                    raw_outputs=splitbody['outputs'];coverage=[];combined=empty_evidence()
                    if not 1<=len(raw_outputs)<=self.policy['max_parts']:fail()
                    for index,output in enumerate(raw_outputs):
                        file=expected.parent/'parts-v4'/f'part-{index:03d}.mp4'
                        if output['file']!=str(file) or p._sha(file)!=output['sha256']:fail('REPLAY_DELIVERY_V5_OUTPUT_CHANGED')
                        actual=p._inspect(file)
                        if (actual['bytes']!=output['bytes'] or actual['duration']!=output['duration'] or
                                actual['bytes']>self.policy['max_part_bytes'] or actual['duration']>self.policy['max_part_seconds'] or
                                actual['stream_config']!=details['stream_config']):fail('REPLAY_DELIVERY_V5_OUTPUT_CHANGED')
                        after=p._packets(file,actual['stream_types'],initial=combined,timeout=self.policy['max_processing_seconds'])
                        coverage.append({'index':index,'packet_before':deepcopy(combined),'packet_after':after});combined=after
                    if combined!=packets:fail('REPLAY_DELIVERY_V5_PACKET_MISMATCH')
                outputs=[]
                for index,output in enumerate(raw_outputs):
                    key=digest({'version':'replay-delivery-output-idempotency-v5','job_id':job['job_id'],'whole_plan_sha256':self.plan_sha,
                        'source_sha256':asset['sha256'],'processing_model':model,'index':index,'output_sha256':output['sha256']})
                    outputs.append({k:output[k] for k in ('file','bytes','sha256','duration')}|{'index':index,'part_idempotency_key':key})
                body={'version':MANIFEST_VERSION,'processing_model':model,'whole_plan_sha256':self.plan_sha,'source':source_record,
                    'outputs':outputs,'output_packet_coverage':coverage,'processing_evidence':evidence}
                if previous is not None and previous!=body:fail('REPLAY_DELIVERY_V5_MANIFEST_CHANGED')
                if p._sha(source)!=asset['sha256']:fail('REPLAY_DELIVERY_V5_SOURCE_CHANGED')
                ref=_immutable(document,body);self._ledger(job,body,ref);self._job(job)
                return {'version':VERSION,'processing_model':model,'outputs':outputs,'manifest':str(document),'manifest_reference':ref,
                    'whole_plan_sha256':self.plan_sha,'source_sha256':asset['sha256'],'source_duration_seconds':details['duration'],
                    'packet_coverage_verified':True,'publishable':False}
        finally:p.active_job=None
