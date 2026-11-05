"""Per-job deterministic packet proofs, reused only after a current full SHA.

This cache is not an approval, backup, Bilibili hash or deletion permission.
Its sole input is an exact, previously persisted full-scan machine receipt.
"""
import hashlib,json,os,re,stat,subprocess,time,uuid
from pathlib import Path

from .db import encode
from .errors import HubError

VERSION='replay-content-packet-cache-v1'
ALGORITHM='ffprobe-show-packets-sha256-order-chain-v1'
LIMIT=262144


def fail():raise HubError('REPLAY_PACKET_CACHE_INVALID',review=True)
def normalized(value):return json.loads(encode(value))
def sha(value):return hashlib.sha256(encode(value).encode()).hexdigest()
def fingerprint(value):
    keys=('st_dev','st_ino','st_size','st_mtime_ns')+(('st_ctime_ns',) if os.name=='posix' else ())
    return tuple(getattr(value,k) for k in keys)


def binary_sha(file,check,*,maximum=256*1024*1024):
    file=Path(file).resolve(strict=True);fd=os.open(file,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0)|getattr(os,'O_BINARY',0))
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0<before.st_size<=maximum:fail()
        digest=hashlib.sha256();total=0;check();next_check=time.monotonic()+.25
        while True:
            if time.monotonic()>=next_check:check();next_check=time.monotonic()+.25
            part=os.read(fd,1024*1024)
            if not part:break
            digest.update(part);total+=len(part)
        check()
        if total!=before.st_size or fingerprint(os.fstat(fd))!=fingerprint(before) or fingerprint(file.stat())!=fingerprint(before):fail()
        return digest.hexdigest()
    finally:os.close(fd)


def tool_identity(ffprobe,check):
    from adapters import media_tools
    try:
        version=subprocess.run([ffprobe,'-version'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=5)
        if version.returncode or not 0<len(version.stdout)<=65536 or len(version.stderr)>65536:fail()
        return {'algorithm':ALGORITHM,'ffprobe_binary_sha256':binary_sha(ffprobe,check),
            'ffprobe_version_sha256':hashlib.sha256(version.stdout).hexdigest(),
            'media_tools_source_sha256':binary_sha(media_tools.__file__,check,maximum=1024*1024)}
    except HubError:raise
    except (OSError,subprocess.TimeoutExpired):raise HubError('REPLAY_PACKET_CACHE_TOOL_IDENTITY_UNAVAILABLE',review=True) from None


class PacketProofCache:
    def __init__(self,root,job_id,checker_version,evidence_directory,*,check,reader):
        try:
            if str(uuid.UUID(job_id))!=job_id or not re.fullmatch('[a-z0-9_-]{1,80}',evidence_directory):raise ValueError()
        except (ValueError,TypeError,AttributeError):fail()
        self.root,self.job_id,self.checker_version,self.evidence_directory=Path(root),job_id,checker_version,evidence_directory
        self.check,self.reader=check,reader
        self.directory=self.root/'media'/job_id/'replay-packet-cache-v1';self.index=self.directory/'index.json'

    def identity(self,binding,download_binding,verified_ref,artifact,media,tool):
        return normalized({'version':VERSION,'checker_version':self.checker_version,'job_id':self.job_id,
            'binding_sha256':sha(binding),'policy_sha256':binding['policy_sha256'],
            'download_binding_sha256':sha(download_binding),'verified_receipt':verified_ref,'artifact':artifact,
            'media':media,'tool':tool})

    def _safe(self,file):
        if (not file.is_absolute() or file.resolve()!=file or not file.is_relative_to(self.root) or
                any(p.is_symlink() for p in (file,*file.parents))):fail()
        return file

    def load(self,identity,expected_packets):
        self.check();self._safe(self.index)
        if not self.index.exists():return None
        try:
            ref,raw=self.reader(self.index,LIMIT);value=json.loads(raw)
            if (set(value)!={'version','job_id','identity','identity_sha256','full_scan_receipt'} or value['version']!=VERSION or
                    value['job_id']!=self.job_id or value['identity_sha256']!=sha(value['identity'])):fail()
            if value['identity']!=identity:return None # A genuine tool/contract change requires a new full scan.
            proof_ref=value['full_scan_receipt'];file=Path(proof_ref['file'])
            parent=self.root/'media'/self.job_id/self.evidence_directory
            if file.name!='receipt.json' or file.parent.parent!=parent or str(uuid.UUID(file.parent.name))!=file.parent.name:fail()
            actual,body=self.reader(self._safe(file),LIMIT)
            if actual!=proof_ref:fail()
            proof=json.loads(body);checks=proof['checks'];download=checks['download'];verification=download['packet_verification']
            if (proof['version']!=self.checker_version or proof['job_id']!=self.job_id or
                    proof['binding_sha256']!=identity['binding_sha256'] or proof['policy_sha256']!=identity['policy_sha256'] or
                    any(proof.get(k) is not False for k in ('human_review','per_artifact_owner_acceptance','publication_authorized')) or
                    download['binding_sha256']!=identity['download_binding_sha256'] or download['artifact']!=identity['artifact'] or
                    download['verified_receipt']!=identity['verified_receipt'] or checks['media']!=identity['media'] or
                    verification!={'version':VERSION,'mode':'full_scan','identity':identity,'identity_sha256':sha(identity)} or
                    download['packet_evidence']!=normalized(expected_packets)):fail()
            self.check()
            return {'packets':download['packet_evidence'],'verification':{'version':VERSION,'mode':'reused_after_current_full_sha',
                'identity':identity,'identity_sha256':sha(identity),'full_scan_receipt':actual,'packet_rescan_performed':False}}
        except HubError:raise
        except (OSError,ValueError,TypeError,KeyError,AttributeError):fail()

    def full_scan(self,identity):
        return {'version':VERSION,'mode':'full_scan','identity':identity,'identity_sha256':sha(identity)}

    def commit(self,identity,reference,proof):
        # Only this call site, after a genuine full scan and all remaining
        # media/cover/policy gates, can make a receipt eligible for reuse.
        if proof['checks']['download']['packet_verification']!=self.full_scan(identity):fail()
        self.check();self._safe(self.directory);self.directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        if os.name=='posix' and self.directory.stat().st_mode&0o077:fail()
        actual,raw=self.reader(Path(reference['file']),LIMIT)
        if actual!=reference or json.loads(raw)!=proof:fail()
        value={'version':VERSION,'job_id':self.job_id,'identity':identity,'identity_sha256':sha(identity),'full_scan_receipt':reference}
        temporary=self.directory/('index-'+uuid.uuid4().hex+'.partial');body=encode(value).encode()
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
            with os.fdopen(fd,'wb') as out:out.write(body);out.flush();os.fsync(out.fileno())
            self._safe(self.index);os.replace(temporary,self.index)
            if os.name=='posix':
                fd=os.open(self.directory,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(fd)
                finally:os.close(fd)
            stored,raw=self.reader(self.index,LIMIT)
            if raw!=body:fail()
            self.check()
        except HubError:raise
        except OSError:raise HubError('REPLAY_PACKET_CACHE_WRITE_FAILED',review=True) from None
