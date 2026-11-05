"""Actual local media/packet/cover checks plus explicit corrupt-cache failures."""
from copy import deepcopy
import hashlib,json
from pathlib import Path
import pytest

from services.hub import replay_machine_checks as module
from services.hub import replay_packet_cache_v1 as cache_module
from services.hub.errors import HubError
from services.hub.db import encode
from test_short_video_web import short_video
from test_replay_video_web import replay
from test_replay_machine_checks import owned,verify,rebind_source_receipt


def index(f):return f['s'].root/'media'/f['job_id']/'replay-packet-cache-v1/index.json'


@pytest.fixture
def measured(owned,monkeypatch):
    f=owned;f['packet_calls']=[];f['video_reads']=[];actual_packet=module.packet_evidence;actual_read=module.read_checked
    def packet(file,*args,**kwargs):f['packet_calls'].append(str(file));return actual_packet(file,*args,**kwargs)
    def read(root,file,**kwargs):
        if Path(file)==f['file'] and kwargs.get('collect') is False:f['video_reads'].append(str(file))
        return actual_read(root,file,**kwargs)
    monkeypatch.setattr(module,'packet_evidence',packet);monkeypatch.setattr(module,'read_checked',read)
    return f


def test_real_cold_scan_then_warm_receipt_still_hashes_entire_current_file(measured):
    f=measured;before=f['file'].read_bytes();a=verify(f);b=verify(f)
    assert len(f['packet_calls'])==1 and len(f['video_reads'])==2
    assert a['checks']['download']['packet_verification']['mode']=='full_scan'
    warm=b['checks']['download']['packet_verification']
    assert warm['mode']=='reused_after_current_full_sha' and warm['packet_rescan_performed'] is False
    assert warm['full_scan_receipt']=={'file':a['evidence_file'],'bytes':a['evidence_bytes'],'sha256':a['evidence_sha256']}
    assert a['checks']['download']['packet_evidence']==b['checks']['download']['packet_evidence']==f['packets']
    assert f['file'].read_bytes()==before and a['evidence_file']!=b['evidence_file']
    assert not f['db'].query('SELECT * FROM publications')


def test_content_proof_reuse_crosses_prepared_to_uploaded_without_reusing_approval(measured):
    f=measured;a=verify(f);job=f['db'].get(f['job_id']);job['result']['replay_video_web_v1']['state']='uploaded'
    with f['db'].transaction() as db:db.execute('UPDATE jobs SET result=? WHERE job_id=?',(encode(job['result']),job['job_id']))
    b=verify(f)
    assert len(f['packet_calls'])==1 and b['checks']['download']['packet_verification']['full_scan_receipt']['sha256']==a['evidence_sha256']
    assert b['human_review'] is b['per_artifact_owner_acceptance'] is b['publication_authorized'] is False


def test_warm_cache_never_masks_current_artifact_mutation(measured):
    f=measured;verify(f)
    with f['file'].open('ab') as out:out.write(b'Owned current-byte mutation')
    job=f['db'].get(f['job_id'])
    with pytest.raises(HubError,match='REPLAY_MACHINE_ARTIFACT_HASH_MISMATCH'):
        f['checks'].verify(job,job['payload'],job['result']['replay_video_web_v1'],f['service'].policy())
    assert len(f['packet_calls'])==1


@pytest.mark.parametrize('change',['index_bytes','proof_bytes','wrong_job','schema','passed_only','warm_receipt'])
def test_corrupt_or_untrusted_cache_cannot_replace_a_genuine_full_scan(measured,change):
    f=measured;proof=verify(f);path=index(f);value=json.loads(path.read_bytes())
    if change=='index_bytes':path.write_bytes(b'{broken json')
    elif change=='proof_bytes':
        with Path(proof['evidence_file']).open('ab') as out:out.write(b'changed')
    elif change=='wrong_job':value['job_id']='00000000-0000-4000-8000-000000000000';path.write_text(encode(value))
    elif change=='schema':value['version']='unknown-cache-v999';path.write_text(encode(value))
    elif change=='passed_only':path.write_text(encode({'passed':True,'verified':True}))
    else:
        file=Path(proof['evidence_file']);body=json.loads(file.read_bytes())
        body['checks']['download']['packet_verification']['mode']='reused_after_current_full_sha'
        raw=encode(body).encode();file.write_bytes(raw)
        value['full_scan_receipt'].update(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw));path.write_text(encode(value))
    with pytest.raises(HubError,match='REPLAY_PACKET_CACHE_INVALID'):verify(f)
    assert len(f['packet_calls'])==1


@pytest.mark.parametrize('change',['algorithm','tool_binary','implementation','checker_version'])
def test_actual_tool_or_contract_change_requires_another_real_packet_scan(measured,monkeypatch,change):
    f=measured;a=verify(f)
    if change=='checker_version':f['checks'].VERSION='owned-new-machine-check-version'
    else:
        real=cache_module.tool_identity
        def different(*args):
            value=real(*args);key={'algorithm':'algorithm','tool_binary':'ffprobe_binary_sha256','implementation':'media_tools_source_sha256'}[change]
            value[key]='owned-new-identity';return value
        monkeypatch.setattr(cache_module,'tool_identity',different)
    b=verify(f)
    assert len(f['packet_calls'])==2 and b['checks']['download']['packet_verification']['mode']=='full_scan'
    assert json.loads(index(f).read_bytes())['full_scan_receipt']['sha256']==b['evidence_sha256']!=a['evidence_sha256']


def test_changed_cover_is_rechecked_even_when_packet_content_has_a_cache(measured):
    f=measured;verify(f);path=Path(f['derived']['provenance_file']);value=json.loads(path.read_bytes());value['output']['sha256']='0'*64;path.write_text(encode(value))
    with pytest.raises(HubError,match='REPLAY_MACHINE_COVER_'):verify(f)
    assert len(f['packet_calls'])==1 and len(f['video_reads'])==2


def test_lost_lease_during_cache_reopen_cannot_issue_a_new_machine_receipt(measured,monkeypatch):
    f=measured;verify(f);actual=module.read_checked;expired=[False]
    def read(root,file,**kwargs):
        value=actual(root,file,**kwargs)
        if Path(file)==index(f):expired[0]=True
        return value
    monkeypatch.setattr(module,'read_checked',read)
    def lease():
        if expired[0]:raise HubError('REPLAY_WORKER_LEASE_LOST',review=True)
    folder=f['s'].root/'media'/f['job_id']/'replay-machine-checks-v1'
    with pytest.raises(HubError,match='REPLAY_WORKER_LEASE_LOST'):verify(f,check=lease)
    assert len(list(folder.glob('*/receipt.json')))==1 and len(f['packet_calls'])==1


def test_index_write_interruption_does_not_create_a_reusable_boolean(measured,monkeypatch):
    f=measured;real=cache_module.os.replace
    def fail(*args,**kwargs):raise OSError('Owned interrupted cache-index publication')
    monkeypatch.setattr(cache_module.os,'replace',fail)
    with pytest.raises(HubError,match='REPLAY_PACKET_CACHE_WRITE_FAILED'):verify(f)
    assert not index(f).exists()
    monkeypatch.setattr(cache_module.os,'replace',real);verify(f)
    assert len(f['packet_calls'])==2


def test_read_checked_keeps_current_full_sha_and_mandatory_first_last_lease_checks(tmp_path,monkeypatch):
    file=tmp_path/'owned-32m.bin';chunk=b'Owned streaming bytes.'*4096
    with file.open('wb') as out:
        for _ in range(384):out.write(chunk)
    calls=[];monkeypatch.setattr(module.time,'monotonic',lambda:0)
    reference,_=module.read_checked(tmp_path,file,maximum=64_000_000,collect=False,check=lambda:calls.append('lease'))
    with file.open('rb') as source:actual=hashlib.file_digest(source,'sha256').hexdigest()
    assert reference['sha256']==actual and reference['bytes']==file.stat().st_size and calls==['lease','lease']


def test_read_checked_cannot_skip_a_lease_lost_at_the_mandatory_end(tmp_path,monkeypatch):
    file=tmp_path/'owned.bin';file.write_bytes(b'Owned entire current content');calls=[];monkeypatch.setattr(module.time,'monotonic',lambda:0)
    def lease():
        calls.append('lease')
        if len(calls)==2:raise HubError('REPLAY_WORKER_LEASE_LOST',review=True)
    with pytest.raises(HubError,match='REPLAY_WORKER_LEASE_LOST'):
        module.read_checked(tmp_path,file,maximum=1024,collect=False,check=lease)
    assert calls==['lease','lease']


def test_read_checked_renews_lease_checks_during_slow_reads_without_one_db_check_per_chunk(tmp_path,monkeypatch):
    file=tmp_path/'owned-slow.bin';file.write_bytes(b'x'*(12*1024*1024));clock=[0.0];checks=[];actual=module.os.read
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    def slow(fd,length):
        value=actual(fd,length);clock[0]+=.1;return value
    monkeypatch.setattr(module.os,'read',slow)
    reference,_=module.read_checked(tmp_path,file,maximum=16_000_000,collect=False,check=lambda:checks.append(clock[0]))
    assert reference['bytes']==12*1024*1024 and checks[0]==0 and checks[-1]==clock[0]
    assert len(checks)>=5 and len(checks)<13 and all(b-a>=.25 for a,b in zip(checks[:-2],checks[1:-1]))


def test_lost_lease_while_first_packet_scan_runs_cannot_register_a_full_scan_receipt(measured,monkeypatch):
    f=measured
    def interrupted(*args,**kwargs):raise HubError('REPLAY_WORKER_LEASE_LOST',review=True)
    monkeypatch.setattr(module,'packet_evidence',interrupted)
    with pytest.raises(HubError,match='REPLAY_WORKER_LEASE_LOST'):verify(f)
    assert not index(f).exists() and not list((f['s'].root/'media'/f['job_id']).glob('replay-machine-checks-v1/*/receipt.json'))
