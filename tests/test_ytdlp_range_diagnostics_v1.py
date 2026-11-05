"""Installed yt-dlp wrapper, owned responses and SQLite; no external requests."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import hashlib
import io
import json
import stat

import pytest

from adapters import ytdlp_highest_v2 as hq
from services.hub.budget import Budget
from services.hub.errors import HubError
from test_ytdlp_highest_transport_v2 import transport
from test_youtube_highest_v2 import highest,info


def owned_range(f,monkeypatch,*,status=206,requested='bytes=100-200',content_range=None,
                content_type='video/mp4',content_length='101',different_track=False,read=False,extra_headers=()):
    from yt_dlp.networking.common import Request
    expected=f.binding['source_plan']['selected_formats'][0]['filesize']
    if content_range is None:content_range=f'bytes 100-200/{expected}'
    f.reads=[];f.closed=[];f.connections=[]
    class Reply(io.BytesIO):
        def __init__(self):super().__init__(b'x'*101);self.status=status
        def getheaders(self):
            return [('Content-Type',content_type),('Content-Range',content_range),('Content-Length',content_length),*extra_headers]
        def getheader(self,key,default=None):return dict((k.lower(),v) for k,v in self.getheaders()).get(key.lower(),default)
        def read(self,*args):f.reads.append(args);return super().read(*args)
        def close(self):f.closed.append(True);super().close()
    def open_owned(url,hosts,**kwargs):
        f.calls.append({'method':kwargs['method']})
        connection=SimpleNamespace(close=lambda:f.connections.append(True))
        return connection,Reply()
    def extract(ydl,url,download=True,**kwargs):return info(f.binding)
    def download(ydl,filename,fmt,*args,**kwargs):
        url=fmt['url']+('&different=1' if different_track else '')
        with ydl.urlopen(Request(url,headers={'Range':requested})) as response:
            if read:assert response.read(1)==b'x'
        raise HubError('OWNED_CONFORMING_RANGE_FINISHED',review=True)
    monkeypatch.setattr(hq,'open_request',open_owned)
    monkeypatch.setattr(f.ytdlp.YoutubeDL,'extract_info',extract)
    monkeypatch.setattr(f.ytdlp.YoutubeDL,'dl',download)


@pytest.mark.parametrize('status,actual,reason,code,retryable',[
    (200,'','http_status_not_206','YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED',False),
    (206,'bytes 99-200/{video}','range_start_mismatch','YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED',False),
    (206,'bytes 100-199/{video}','range_end_mismatch','YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED',False),
    (206,'bytes 100-201/{video}','range_end_mismatch','YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED',False),
    (206,'bytes 100-200/{audio}','range_total_mismatch','YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED',False),
    (206,'bytes 100-200/999','range_total_mismatch','YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED',False),
    (206,'bytes 100-99/{video}','invalid_content_range_bounds','YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED',False),
    (206,'invalid','invalid_content_range','YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED',False),
    (416,'bytes */{video}','http_status_not_206','YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED',False),
    (401,'','http_status_not_206','YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED',False),
    (403,'','http_status_not_206','YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED',False),
    (429,'','http_status_not_206','YOUTUBE_DOWNLOAD_RATE_LIMITED',False),
    (500,'','http_status_not_206','YOUTUBE_DOWNLOAD_NETWORK_ERROR',True),
    (503,'','http_status_not_206','YOUTUBE_DOWNLOAD_NETWORK_ERROR',True),
])
def test_real_wrapper_refuses_before_body_and_keeps_partial_and_ledger(transport,monkeypatch,status,actual,reason,code,retryable):
    f=transport;formats=f.binding['source_plan']['selected_formats']
    actual=actual.format(video=formats[0]['filesize'],audio=formats[1]['filesize'])
    part=f.folder/'stream-299.mp4.part';original=b'owned-partial-unchanged';part.write_bytes(original)
    before=f.db.get(f.job['job_id'])
    owned_range(f,monkeypatch,status=status,content_range=actual)
    assert hq.child_download(f.ticket)==1
    result=f.result();diag=result['range_diagnostic']
    assert result['error_code']==code and result['retryable'] is retryable
    assert diag['rejection_reason']==reason and diag['status']==status
    assert diag['requested_range']=='bytes=100-200' and diag['format_id']=='299'
    assert diag['expected_size']==formats[0]['filesize'] and diag['track_identity_matches'] is True
    assert diag['request_index']==result['http']['requests']==1
    assert result['http']['response_bytes']==0 and len(f.calls)==1
    assert f.reads==[] and f.closed and f.connections and not f.sockets
    assert part.read_bytes()==original and f.db.get(f.job['job_id'])==before
    calls=f.db.query('SELECT * FROM api_calls WHERE job_id=?',(f.job['job_id'],))
    assert len(calls)==1 and calls[0]['state']=='error' and calls[0]['error_code']==code


@pytest.mark.parametrize('change,reason',[
    ({'different_track':True},'track_identity_unbound'),
    ({'content_length':'102'},'content_length_mismatch'),
    ({'content_length':'secret=https://private.test/?token=private'},'content_length_mismatch'),
    ({'requested':'bytes=100-200?token=private'},'invalid_requested_range'),
    ({'content_range':'bytes 100-200/999?Cookie=private'},'invalid_content_range'),
])
def test_identity_headers_and_malformed_values_never_get_appended(transport,monkeypatch,change,reason):
    f=transport;part=f.folder/'stream-299.mp4.part';part.write_bytes(b'preserved')
    owned_range(f,monkeypatch,**change)
    assert hq.child_download(f.ticket)==1
    assert f.result()['range_diagnostic']['rejection_reason']==reason
    assert part.read_bytes()==b'preserved' and not f.reads and f.closed
    assert 'private' not in json.dumps(f.result()) and 'Cookie' not in json.dumps(f.result())


def test_conforming_range_is_accepted_and_does_not_leave_a_failure_diagnostic(transport,monkeypatch):
    f=transport;owned_range(f,monkeypatch,read=True)
    assert hq.child_download(f.ticket)==1
    result=f.result()
    assert result['error_code']=='OWNED_CONFORMING_RANGE_FINISHED' and 'range_diagnostic' not in result
    assert len(f.reads)==1 and result['http']['response_bytes']==1
    assert f.db.query('SELECT state FROM api_calls WHERE job_id=?',(f.job['job_id'],))[0]['state']=='done'


def test_failure_diagnostics_drop_url_query_cookie_header_and_mime_parameters(transport,monkeypatch):
    f=transport
    owned_range(f,monkeypatch,status=416,content_range='bytes */999',
        content_type='application/octet-stream; token=NEVER_PERSIST',
        extra_headers=(('Set-Cookie','NEVER_PERSIST'),('Location','https://secret.test/?key=NEVER_PERSIST')))
    assert hq.child_download(f.ticket)==1
    encoded=json.dumps(f.result()['range_diagnostic'])
    assert 'NEVER_PERSIST' not in encoded and 'https://' not in encoded and 'Set-Cookie' not in encoded
    assert f.result()['range_diagnostic']['content_type']=='application/octet-stream'
    assert not f.reads and f.closed


def test_pure_validator_accepts_exact_final_tail_and_rejects_other_track_total():
    expected={'format_id':'299','filesize':1000}
    tail=hq.range_response_diagnostic('bytes=900-2000',206,'bytes 900-999/1000','video/mp4','100',expected,True)
    assert tail['rejection_reason'] is None
    opened=hq.range_response_diagnostic('bytes=900-',206,'bytes 900-999/1000','video/mp4','100',expected,True)
    assert opened['rejection_reason'] is None
    invalid=hq.range_response_diagnostic('bytes=100-200',206,'bytes 100-200/500','x/SECRET','101',expected,True)
    assert invalid['rejection_reason']=='range_total_mismatch' and invalid['content_type']=='other'


def history(f,*,requests=1078,body_bytes=10957610203,accounted=()):
    stats={'requests':requests,'response_bytes':body_bytes,'request_limit':f.binding['source_plan']['max_http_requests'],
        'provider':hq.PROVIDER,'budget_history_reset':False,'response_kinds':{'media':requests}}
    if accounted:stats['accounted_probe_call_ids']=list(accounted)
    result={'error_code':'YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED','http':stats,'source_plan':f.binding['source_plan']}
    file=f.folder/'failure-before-attempt-1.json';hq.atomic_json(file,result)
    return file,stats


def owned_root_probe(f,monkeypatch,prior,probe_call,**changes):
    proof={'version':'youtube-whole-one-byte-range-probe-v1','job_id':f.job['job_id'],
        'source_plan_sha256':hq.digest(f.binding['source_plan']),'prior_result_sha256':hashlib.sha256(prior.read_bytes()).hexdigest(),
        'call_id':probe_call,'response_bytes':1,'binding_sha256':hq.digest(f.binding),'extra_numeric_proof':206}
    proof.update(changes);path=f.folder/'range-probe-v1.json';hq.atomic_json(path,proof);path.chmod(0o444)
    # Tests run as the app user and cannot create host-root-owned evidence.
    # Replace only that ownership check; proof, file mode, hashes and DB are real.
    check=hq._root_probe_permission
    monkeypatch.setattr(hq,'_root_probe_permission',lambda st:check(SimpleNamespace(st_mode=st.st_mode,st_uid=0),platform='posix'))
    return path


def test_same_job_ledger_and_probe_byte_carry_once_without_resetting_1079(transport,monkeypatch):
    f=transport;budget=Budget(f.db,hq.budget_limits(f.binding))
    old_call=budget.reserve(hq.PROVIDER,f.job['job_id'],units=1078);budget.finish(old_call,'done')
    prior,stats=history(f)
    probe=budget.reserve(hq.PROVIDER,f.job['job_id']);budget.finish(probe,'done')
    owned_root_probe(f,monkeypatch,prior,probe)
    before=f.db.query('SELECT * FROM api_calls ORDER BY call_id')
    carried=hq.resume_http_stats(f.db,f.job,f.binding,f.folder)
    assert carried['requests']==1079 and carried['response_bytes']==stats['response_bytes']+1
    assert carried['request_limit']==stats['request_limit'] and carried['accounted_probe_call_ids']==[probe]
    hq.atomic_json(f.folder/'progress.json',{'http':carried,'source_plan':f.binding['source_plan']})
    again=hq.resume_http_stats(f.db,f.job,f.binding,f.folder)
    assert again==carried and f.db.query('SELECT * FROM api_calls ORDER BY call_id')==before
    assert hashlib.sha256(prior.read_bytes()).hexdigest()==json.loads((f.folder/'range-probe-v1.json').read_bytes())['prior_result_sha256']
    owned_range(f,monkeypatch,status=416,content_range='bytes */999')
    assert hq.child_download(f.ticket)==1
    result=f.result()
    assert result['http']['requests']==1080 and result['range_diagnostic']['request_index']==1080
    assert result['http']['response_bytes']==stats['response_bytes']+1
    assert hq.resume_http_stats(f.db,f.job,f.binding,f.folder)['response_bytes']==stats['response_bytes']+1


@pytest.mark.parametrize('changes',[
    {'job_id':'different'}, {'source_plan_sha256':'0'*64}, {'prior_result_sha256':'0'*64},
    {'call_id':'00000000-0000-0000-0000-000000000000'}, {'response_bytes':2}, {'response_bytes':True},
    {'binding_sha256':'0'*64}, {'version':'unexpected'},
])
def test_unverified_probe_cannot_change_history(transport,monkeypatch,changes):
    f=transport;budget=Budget(f.db,hq.budget_limits(f.binding))
    first=budget.reserve(hq.PROVIDER,f.job['job_id'],units=1078);budget.finish(first,'done')
    prior,_=history(f);probe=budget.reserve(hq.PROVIDER,f.job['job_id']);budget.finish(probe,'done')
    owned_root_probe(f,monkeypatch,prior,probe,**changes)
    with pytest.raises(HubError,match='YOUTUBE_HIGHEST_RANGE_PROBE_INVALID'):
        hq.resume_http_stats(f.db,f.job,f.binding,f.folder)


def test_missing_probe_does_not_turn_an_unexplained_ledger_gap_into_zero(transport):
    f=transport;budget=Budget(f.db,hq.budget_limits(f.binding))
    budget.reserve(hq.PROVIDER,f.job['job_id'],units=1079);history(f)
    with pytest.raises(HubError,match='YOUTUBE_HIGHEST_HTTP_HISTORY_INCOMPLETE'):
        hq.resume_http_stats(f.db,f.job,f.binding,f.folder)


@pytest.mark.parametrize('uid,mode,platform,allowed',[
    (0,stat.S_IFREG|0o444,'posix',True),(10001,stat.S_IFREG|0o444,'posix',False),
    (0,stat.S_IFREG|0o644,'posix',False),(0,stat.S_IFLNK|0o444,'posix',False),
    (0,stat.S_IFREG|0o444,'nt',False),
])
def test_probe_receipt_must_be_root_owned_read_only_regular_file(uid,mode,platform,allowed):
    assert hq._root_probe_permission(SimpleNamespace(st_uid=uid,st_mode=mode),platform=platform) is allowed
