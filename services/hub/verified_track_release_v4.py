"""V4 opt-in custody of encoded payload after its durable verified merge.

Only the two exactly bound, fully verified source tracks may be unlinked. The
complete merged MP4 remains. The original track container bytes are NOT claimed
recoverable; neither Bilibili publication nor cloud backup is implied.
"""
from copy import deepcopy
import hashlib,json,os,shutil,stat,time,uuid
from pathlib import Path

from adapters.media_tools import inspect_media,packet_evidence
from adapters.ytdlp_replay import source_packet_evidence
from .db import encode
from .errors import HubError
from .locks import resource_guard
from .media_processing import atomic_json
from .replay_multipart_v4 import digest,validate_whole_replay_plan

VERSION='verified-merge-track-release-v1'
TABLE='youtube_track_custody_v4'
STATE='youtube_download_v4'

def fail(code='YOUTUBE_WHOLE_TRACK_CUSTODY_INVALID'):raise HubError(code,review=True)


class VerifiedTrackReleaseV4:
    def __init__(self,settings,store):self.settings,self.store=settings,store

    def _context(self,job,binding,check):
        check();current=self.store.get(job['job_id']);state=current['result'].get(STATE,{}) if current else {}
        if (not current or current['job_type']!='youtube_replay_download_v4' or current['policy_version']!='replay-fifo-v4' or
                current['source_platform']!='youtube' or current['queue']!='video' or current['state']!='processing' or
                not job.get('lease_owner') or current['lease_owner']!=job['lease_owner'] or (current['lease_until'] or 0)<=time.time() or
                current['payload']!=job['payload'] or state.get('state') not in ('verified','parts_verified','single_output_verified_v5') or state.get('binding')!=binding or
                binding['planning_policy'].get('raw_track_release_after_verified_merge') is not True):
            fail('YOUTUBE_WHOLE_TRACK_RELEASE_CHECKPOINT_REQUIRED')
        whole=validate_whole_replay_plan(current['payload']['whole_replay_plan'],binding['planning_policy'],official_raw=binding['official_raw'])
        if state.get('state')=='single_output_verified_v5' and binding['planning_policy'].get('prefer_single_whole_passthrough_v5') is not True:fail()
        if (digest(whole)!=current['payload']['whole_replay_plan_sha256'] or binding['whole_replay_plan']!=whole or
                whole['whole_budget'].get('resource_model')!=VERSION):fail()
        if self.store.query("SELECT 1 FROM publications WHERE source_platform='youtube' AND source_id=?",(current['source_id'],)):
            fail('YOUTUBE_WHOLE_TRACK_RELEASE_EXTERNAL_STATE_PRESENT')
        floor=max(binding['planning_policy']['disk_floor_bytes'],int(self.settings.config['disk_floor_gb']*1_000_000_000))
        if shutil.disk_usage(self.settings.root).free<floor+binding['planning_policy']['workspace_overhead_bytes']:
            raise HubError('DISK_LOW',review=True)
        area=self.settings.root/'media'/job['job_id']/'youtube-whole-v4'
        if area.resolve()!=area or any(p.is_symlink() for p in (area,*area.parents)) or os.name=='posix' and area.stat().st_mode&0o077:fail()
        if any(p.name.endswith(('.part','.partial','.partial.mp4')) for p in area.iterdir()):
            fail('YOUTUBE_WHOLE_TRACK_RELEASE_UNRESOLVED_PARTIAL')
        return current,state,area

    def _inspect(self,file,maximum,check):
        check()
        if file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)) or not file.is_file():fail()
        before=file.stat();fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0));h=hashlib.sha256()
        with os.fdopen(fd,'rb') as stream:
            opened=os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink!=1 or not 0<opened.st_size<=maximum:fail()
            next_check=0
            while block:=stream.read(1024*1024):
                if time.monotonic()>=next_check:check();next_check=time.monotonic()+.25
                h.update(block)
            closed=os.fstat(stream.fileno())
        after=file.stat();check()
        keys=('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
        if any(getattr(before,k)!=getattr(after,k) for k in keys) or any(getattr(opened,k)!=getattr(closed,k) for k in keys):fail()
        if any(getattr(before,k)!=getattr(opened,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns')):fail()
        parent=file.parent.stat()
        return {'file':str(file),'bytes':before.st_size,'sha256':h.hexdigest(),'device':before.st_dev,
            'inode':before.st_ino,'mtime_ns':before.st_mtime_ns,'ctime_ns':before.st_ctime_ns,
            'parent_device':parent.st_dev,'parent_inode':parent.st_ino}

    @staticmethod
    def _read_json(file,reference):
        fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
        with os.fdopen(fd,'rb') as stream:
            before=os.fstat(stream.fileno())
            if before.st_size!=reference['bytes']:fail()
            raw=stream.read(reference['bytes']+1);after=os.fstat(stream.fileno())
        if (len(raw)!=reference['bytes'] or hashlib.sha256(raw).hexdigest()!=reference['sha256'] or
                any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns'))):fail()
        try:return json.loads(raw)
        except (ValueError,UnicodeError):fail()

    def _unshared(self,db,job,paths):
        def uses(value):
            if isinstance(value,dict):return any(uses(v) for v in value.values())
            if isinstance(value,list):return any(uses(v) for v in value)
            return isinstance(value,str) and value in paths
        for row in db.execute('SELECT job_id,payload,result FROM jobs'):
            if row['job_id']!=job['job_id'] and (uses(json.loads(row['payload'])) or uses(json.loads(row['result']))):
                fail('YOUTUBE_WHOLE_TRACK_RELEASE_SHARED_SOURCE')

    def _merged(self,state,binding,area,check,*,expected=None):
        asset=state['source_asset'];file=area/'original.mp4';actual=self._inspect(file,binding['source_plan']['planned_output_max_bytes'],check)
        if asset['file']!=str(file) or actual['bytes']!=asset['bytes'] or actual['sha256']!=asset['sha256']:fail('YOUTUBE_WHOLE_TRACK_RELEASE_MERGED_CHANGED')
        if expected is not None and actual!=expected:fail('YOUTUBE_WHOLE_TRACK_RELEASE_MERGED_CHANGED')
        return actual

    def _persist(self,job,binding,record,*,previous=None):
        body=encode(record);checksum=hashlib.sha256(body.encode()).hexdigest();now=time.time()
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM jobs WHERE job_id=?',(job['job_id'],)).fetchone()
            if (not row or row['state']!='processing' or row['lease_owner']!=job['lease_owner'] or (row['lease_until'] or 0)<=now or
                    json.loads(row['payload'])!=job['payload']):fail('YOUTUBE_WHOLE_TRACK_RELEASE_LEASE_LOST')
            result=json.loads(row['result']);state=result.get(STATE,{})
            if state.get('binding')!=binding or state.get('state') not in ('verified','parts_verified','single_output_verified_v5'):fail()
            self._unshared(db,job,{p['file'] for p in record['tracks']})
            if previous is None:
                db.execute('INSERT INTO youtube_track_custody_v4 VALUES(?,?,?,?,?)',
                    (job['job_id'],record['operation_id'],digest(binding),checksum,body))
            elif db.execute('UPDATE youtube_track_custody_v4 SET record_sha256=?,record=? WHERE job_id=? AND record_sha256=?',
                    (checksum,body,job['job_id'],previous)).rowcount!=1:fail('YOUTUBE_WHOLE_TRACK_RELEASE_CAS_FAILED')
            pointer={'version':VERSION,'operation_id':record['operation_id'],'binding_sha256':digest(binding),'state':record['state'],
                'encoded_payload_preserved':True,'raw_container_bytes_recoverable':False,'merged_deleted':False,
                'cloud_backup_claimed':False,'publication_authorized':False,'human_review':False}
            state['raw_track_custody_v4']=pointer;result[STATE]=state
            db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=?',(encode(result),now,job['job_id']))
        # Audit mirror only; the synchronous database intent is the authority.
        atomic_json(Path(record['area'])/'raw-track-custody-v4.json',record)
        return checksum

    def _unlink(self,item,*,check):
        check()
        file=Path(item['file'])
        if os.name=='posix':
            fd=os.open(self.settings.root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                for part in file.parent.relative_to(self.settings.root).parts:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);os.close(fd);fd=child
                parent=os.fstat(fd)
                if (parent.st_dev,parent.st_ino)!=(item['parent_device'],item['parent_inode']):fail()
                actual=os.stat(file.name,dir_fd=fd,follow_symlinks=False)
                if (not stat.S_ISREG(actual.st_mode) or actual.st_nlink!=1 or
                        any(getattr(actual,k)!=item[v] for k,v in (('st_dev','device'),('st_ino','inode'),('st_size','bytes'),
                            ('st_mtime_ns','mtime_ns'),('st_ctime_ns','ctime_ns')))):fail()
                check();os.unlink(file.name,dir_fd=fd);os.fsync(fd)
            finally:os.close(fd)
        else:
            actual=file.stat()
            if file.is_symlink() or any(getattr(actual,k)!=item[v] for k,v in (('st_ino','inode'),('st_size','bytes'),
                    ('st_mtime_ns','mtime_ns'),('st_ctime_ns','ctime_ns'))):fail()
            check();file.unlink()

    def run(self,job,binding,*,check):
        current,state,area=self._context(job,binding,check)
        with resource_guard(self.settings.root/'data/locks/heavy',area/'raw-release-lock-v4'):
            # The private directory is an OS lock, not unknown media. Existing
            # download allocation code does not see it: verified jobs skip it.
            current,state,area=self._context(job,binding,check)
            with self.store.transaction() as db:
                db.execute('''CREATE TABLE IF NOT EXISTS youtube_track_custody_v4(job_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL UNIQUE,binding_sha256 TEXT NOT NULL,record_sha256 TEXT NOT NULL,record TEXT NOT NULL)''')
            rows=self.store.query('SELECT * FROM youtube_track_custody_v4 WHERE job_id=?',(job['job_id'],))
            merged=self._merged(state,binding,area,check)
            receipt_file=area/'verified.json';ref=self._inspect(receipt_file,300000,check)
            receipt=self._read_json(receipt_file,ref)
            if (receipt.get('version')!='youtube-whole-replay-download-v4' or receipt.get('binding_sha256')!=digest(binding) or
                    receipt.get('source_asset')!=state['source_asset'] or receipt.get('receipt')!=state['receipt']):fail()
            maximum=binding['planning_policy']['max_total_packets'];timeout=binding['planning_policy']['max_processing_seconds']
            if rows:
                row=rows[0];record=json.loads(row['record']);previous=row['record_sha256']
                if (hashlib.sha256(row['record'].encode()).hexdigest()!=previous or row['binding_sha256']!=digest(binding) or
                        record.get('version')!=VERSION or record.get('job_id')!=job['job_id'] or record.get('area')!=str(area) or
                        record.get('operation_id')!=row['operation_id'] or record.get('binding_sha256')!=digest(binding) or
                        record.get('merged')!=merged or record.get('verified_receipt')!=ref or record.get('state') not in ('releasing','released')):fail()
            else:
                selected=binding['source_plan']['selected_formats'];tracks=state['receipt']['tracks'];items=[];paths=[]
                if len(selected)!=2 or len(tracks)!=2 or tracks!=receipt['receipt']['tracks']:fail()
                for expected,track in zip(selected,tracks):
                    if any(track.get(k)!=v for k,v in expected.items()):fail()
                    name='stream-'+expected['format_id']+'.'+expected['ext']
                    if Path(name).name!=name or '/' in name or '\\' in name:fail()
                    file=area/name;actual=self._inspect(file,binding['source_plan']['source_bytes'],check)
                    if actual['bytes']!=track['bytes'] or actual['bytes']!=expected['filesize'] or actual['sha256']!=track['sha256']:fail()
                    items.append(actual);paths.append(file)
                details=inspect_media(area/'original.mp4',check=check,include_frame_rates=True)
                packet=packet_evidence(area/'original.mp4',details['stream_types'],check=check,max_packets=maximum,timeout=timeout)
                source=source_packet_evidence(paths,check,max_packets=maximum,timeout=timeout)
                if packet!=source or packet!=state['source_asset']['packet_evidence'] or details['stream_config']!=state['source_asset']['stream_config']:
                    fail('YOUTUBE_WHOLE_TRACK_RELEASE_PACKET_MISMATCH')
                self._merged(state,binding,area,check,expected=merged)
                record={'version':VERSION,'operation_id':str(uuid.uuid4()),'job_id':job['job_id'],'area':str(area),
                    'binding_sha256':digest(binding),'whole_plan_sha256':digest(binding['whole_replay_plan']),
                    'state':'releasing','merged':merged,'verified_receipt':ref,'tracks':items,'packet_evidence':packet,
                    'progress':{p['file']:'pending' for p in items},'encoded_payload_preserved':True,
                    'raw_container_bytes_recoverable':False,'merged_deleted':False,'unknown_partials_deleted':False,
                    'cloud_backup_claimed':False,'publication_authorized':False,'human_review':False,'created_at':time.time()}
                previous=self._persist(job,binding,record)
            if (set(record['progress'])!={p['file'] for p in record['tracks']} or len(record['tracks'])!=2 or
                    any(v not in ('pending','deleting','deleted') for v in record['progress'].values())):fail()
            selected=binding['source_plan']['selected_formats'];tracks=state['receipt']['tracks']
            if (len(selected)!=2 or len(tracks)!=2 or record.get('whole_plan_sha256')!=digest(binding['whole_replay_plan']) or
                    record.get('packet_evidence')!=state['source_asset']['packet_evidence'] or
                    record.get('encoded_payload_preserved') is not True or record.get('raw_container_bytes_recoverable') is not False or
                    record.get('merged_deleted') is not False or record.get('cloud_backup_claimed') is not False):fail()
            for item,expected,track in zip(record['tracks'],selected,tracks):
                exact=area/('stream-'+expected['format_id']+'.'+expected['ext'])
                if (item['file']!=str(exact) or item['bytes']!=track['bytes'] or item['bytes']!=expected['filesize'] or
                        item['sha256']!=track['sha256'] or any(track.get(k)!=v for k,v in expected.items())):fail()
            for item in record['tracks']:
                file=Path(item['file']);phase=record['progress'][item['file']]
                if file.parent!=area or not file.name.startswith('stream-'):fail()
                self._context(job,binding,check);self._merged_stat(record['merged'])
                if file.exists():
                    if phase=='deleted' or self._inspect(file,binding['source_plan']['source_bytes'],check)!=item:fail()
                elif phase not in ('deleting','deleted'):fail('YOUTUBE_WHOLE_TRACK_RELEASE_UNEXPECTED_MISSING')
                if phase=='deleted':continue
                record['progress'][item['file']]='deleting';previous=self._persist(job,binding,record,previous=previous)
                check();self._merged_stat(record['merged'])
                if file.exists():self._unlink(item,check=check)
                record['progress'][item['file']]='deleted';previous=self._persist(job,binding,record,previous=previous)
            self._merged_stat(record['merged']);record['state']='released';record['released_at']=record.get('released_at',time.time())
            self._persist(job,binding,record,previous=previous)
            return deepcopy(record)

    @staticmethod
    def _merged_stat(item):
        file=Path(item['file']);info=file.stat()
        if (file.resolve()!=file or any(p.is_symlink() for p in (file,*file.parents)) or not stat.S_ISREG(info.st_mode) or any(getattr(info,k)!=item[v] for k,v in
                (('st_dev','device'),('st_ino','inode'),('st_size','bytes'),('st_mtime_ns','mtime_ns'),('st_ctime_ns','ctime_ns')))):
            fail('YOUTUBE_WHOLE_TRACK_RELEASE_MERGED_CHANGED')
