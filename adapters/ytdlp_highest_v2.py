"""Exact 299+140 pilot acquisition; ordinary authenticated HTTPS only.

Uses the established v1 destination/auth/protection and packet helpers. No
legacy workspace is visited, no quality fallback, proxy or challenge bypass.
"""
from copy import deepcopy
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
import hashlib,http.client,json,math,os,re,signal,stat,subprocess,sys,time,uuid
from urllib.parse import urljoin
from urllib.request import Request as CookieRequest
from zoneinfo import ZoneInfo

from adapters import ytdlp_replay as legacy
from adapters.media_tools import inspect_media,packet_evidence,process_lines
from adapters.safe_https import open_request
from services.hub.budget import Budget
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.locks import resource_guard
from services.hub.maintenance import Maintenance
from services.hub.media_processing import atomic_json
from services.hub.youtube_download import digest
from services.hub.youtube_download_v2 import VERSION,YouTubeHighestDownload
from services.hub.youtube_highest_plan_v2 import VIDEO_ID,QUALITY,MAX_BYTES,OPERATING_NOT_BEFORE,require_operating_date,validate_source_plan

PROVIDER=legacy.PROVIDER


def clear_stream_player_guard(response):
    """Keep access restrictions; defer license availability to stream selection.

    In pinned yt-dlp 2026.10.26, a format's drmFamilies determines has_drm.
    Response-wide licenseInfos can accompany clear formats. It is therefore
    insufficient to reject every stream before the ordinary extractor runs.
    select_highest still requires explicit has_drm=False for both exact tracks;
    no license retrieval, decryption or alternative client is enabled here.
    The legacy downloader retains its existing stricter response-wide guard.
    """
    if not isinstance(response,dict):return
    status=response.get('playabilityStatus') or {}
    reason=str(status.get('reason','')).lower()
    if (status.get('status') in ('LOGIN_REQUIRED','AGE_VERIFICATION_REQUIRED','CONTENT_CHECK_REQUIRED','AGE_CHECK_REQUIRED')
            or any(x in reason for x in ('sign in','confirm your age','not a bot','captcha',
                                        'not available in your country','not available in your region','drm'))):
        raise HubError('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED')


def require_clear_selected_formats(selected):
    """Apply the same last gate to pilot, natural and whole-download callbacks."""
    if (not isinstance(selected,list) or not selected or
            any(not isinstance(fmt,dict) or fmt.get('has_drm') is not False for fmt in selected)):
        raise HubError('YOUTUBE_HIGHEST_CLEAR_FORMAT_REQUIRED',review=True)


def access_response_diagnostic(phase,target_id,response=None,*,client=None,request_index=None,http_status=None):
    """Bounded classification only. It never changes an access decision.

    Client names come only from the extractor call arguments, not response
    text. Unknown/opaque details stay unknown; no body, URL or header survives.
    """
    if phase not in ('initial','client','age','http'):return None
    clients={'web','web_embedded','web_creator','web_safari','mweb','tv','tv_samsung','tv_embedded','tv_downgraded',
             'android','android_vr','android_embedded','android_creator','ios','ios_embedded','visionos'}
    statuses={'OK','LOGIN_REQUIRED','AGE_VERIFICATION_REQUIRED','CONTENT_CHECK_REQUIRED','AGE_CHECK_REQUIRED',
              'UNPLAYABLE','ERROR','LIVE_STREAM_OFFLINE','LIVE_STREAM_NOT_STARTED'}
    def video_id(value):return value if type(value) is str and re.fullmatch('[A-Za-z0-9_-]{11}',value) else None
    body=response if type(response) is dict else {}
    playability=body.get('playabilityStatus');playability=playability if type(playability) is dict else {}
    details=body.get('videoDetails');details=details if type(details) is dict else {}
    streaming=body.get('streamingData');streaming=streaming if type(streaming) is dict else {}
    target,returned=video_id(target_id),video_id(details.get('videoId'))
    status=playability.get('status');status=status if type(status) is str and status in statuses else 'unknown'
    reason=playability.get('reason');reason=reason.lower() if type(reason) is str else ''
    categories=[]
    if status=='LOGIN_REQUIRED' or 'sign in' in reason:categories.append('login')
    if phase=='age' or status in ('AGE_VERIFICATION_REQUIRED','AGE_CHECK_REQUIRED') or 'confirm your age' in reason:categories.append('age')
    if status=='CONTENT_CHECK_REQUIRED':categories.append('content_check')
    if any(x in reason for x in ('not a bot','captcha')):categories.append('challenge')
    if any(x in reason for x in ('not available in your country','not available in your region')):categories.append('region')
    licenses=streaming.get('licenseInfos')
    if 'drm' in reason or type(licenses) in (list,dict) and len(licenses)>0:categories.append('drm')
    if phase=='http' and type(http_status) is int and http_status in (401,403):categories.append('http_authorization')
    return {'version':'youtube-access-diagnostic-v1','phase':phase,
        'request_index':request_index if type(request_index) is int and 0<=request_index<=20000 else None,
        'http_status':http_status if type(http_status) is int and 100<=http_status<=599 else None,
        'target_video_id':target,'returned_video_id':returned,
        'video_id_relation':'unknown' if target is None or returned is None else 'matched' if target==returned else 'different',
        'client':client if type(client) is str and client in clients else None,
        'playability_status':status,'restriction_categories':categories}


def range_response_diagnostic(requested_range,status,content_range,content_type,content_length,expected_format,identity_matches):
    """Only bounded numeric range facts; never persist raw headers or URLs.

    This is evidence for the current response, not permission to retry it.
    A resumable response must belong to the active selected track. In
    particular, the other selected track's length is not an acceptable total.
    """
    def number(value):
        if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,19}',value):return None
        parsed=int(value)
        return parsed if parsed<=9_223_372_036_854_775_807 else None
    request=(re.fullmatch(r'bytes=([0-9]{1,19})-([0-9]{0,19})',requested_range)
        if isinstance(requested_range,str) and len(requested_range)<=48 else None)
    start=number(request[1]) if request else None
    end=number(request[2]) if request and request[2] else None
    request_valid=request is not None and start is not None and (not request[2] or end is not None) and (end is None or end>=start)
    actual=(re.fullmatch(r'bytes ([0-9]{1,19})-([0-9]{1,19})/([0-9]{1,19})',content_range)
        if isinstance(content_range,str) and len(content_range)<=68 else None)
    first,last,total=(tuple(number(actual[i]) for i in (1,2,3)) if actual else (None,None,None))
    actual_valid=actual is not None and all(item is not None for item in (first,last,total))
    unsatisfied=(re.fullmatch(r'bytes \*/([0-9]{1,19})',content_range)
        if isinstance(content_range,str) and len(content_range)<=28 else None)
    unsatisfied_total=number(unsatisfied[1]) if unsatisfied else None
    identity=expected_format if isinstance(expected_format,dict) else {}
    size=identity.get('filesize')
    size=size if type(size) is int and 0<size<=9_223_372_036_854_775_807 else None
    ident=identity.get('format_id')
    ident=ident if isinstance(ident,str) and re.fullmatch(r'[0-9A-Za-z_-]{1,32}',ident) else None
    mime=content_type.split(';',1)[0].strip().lower() if isinstance(content_type,str) else ''
    mime=mime if mime in ('video/mp4','audio/mp4','video/webm','audio/webm','application/octet-stream',
        'text/plain','text/html','application/json','application/xml','text/xml') else 'other' if mime else 'unknown'
    length=number(content_length)
    diagnostic={'version':'youtube-range-response-v1','status':status if type(status) is int and 100<=status<=599 else None,
        'requested_range':f'bytes={start}-{end if end is not None else ""}' if request_valid else None,
        'requested_range_valid':request_valid,'requested_start':start if request_valid else None,
        'content_range':f'bytes {first}-{last}/{total}' if actual_valid else f'bytes */{unsatisfied_total}' if unsatisfied_total is not None else None,
        'content_range_valid':actual_valid,'content_type':mime,'content_length':length,
        'content_length_present':content_length is not None,'format_id':ident,'expected_size':size,
        'track_identity_matches':identity_matches is True}
    reason=None
    if not request_valid:reason='invalid_requested_range'
    elif status!=206:reason='http_status_not_206'
    elif not actual_valid:reason='invalid_content_range'
    elif identity_matches is not True or ident is None or size is None:reason='track_identity_unbound'
    elif first!=start:reason='range_start_mismatch'
    elif not first<=last<total:reason='invalid_content_range_bounds'
    elif total!=size:reason='range_total_mismatch'
    elif last!=min(end if end is not None else total-1,total-1):reason='range_end_mismatch'
    elif content_length is not None and length!=last-first+1:reason='content_length_mismatch'
    diagnostic['rejection_reason']=reason
    return diagnostic


def _root_probe_permission(info,*,platform=os.name):
    return platform=='posix' and stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_mode&0o222==0


def request_budget_revision(job,binding):
    """Validate the one explicit redirect-budget revision used for a held job."""
    revision=(job.get('result') or {}).get('download_request_budget_revision_v1')
    if revision is None:return None
    try:
        old_plan=deepcopy(revision['old_source_plan']);new_plan=deepcopy(revision['new_source_plan'])
        old_digest=digest(old_plan);new_digest=digest(new_plan)
        media=old_plan['media_requests_plan'];metadata=old_plan['metadata_request_allowance']
        allowed_changes={'max_http_requests','redirect_resume_request_allowance','whole_plan_sha256'}
        evidence=revision['evidence']
        changed={key for key in old_plan if old_plan[key]!=new_plan.get(key)}
        if (revision.get('version')!='youtube-whole-request-budget-revision-v1' or
                revision.get('job_id')!=job['job_id'] or revision.get('previous_attempts')!=job['attempts']-1 or
                revision.get('old_source_plan_sha256')!=old_digest or revision.get('new_source_plan_sha256')!=new_digest or
                new_plan!=binding['source_plan'] or
                not changed<=allowed_changes or
                not {'max_http_requests','redirect_resume_request_allowance'}<=changed or
                type(media) is not int or media<1 or type(metadata) is not int or metadata<1 or
                old_plan.get('redirect_resume_request_allowance')!=max(64,math.ceil(media/4)) or
                old_plan.get('max_http_requests')!=media+metadata+max(64,math.ceil(media/4)) or
                new_plan.get('redirect_resume_request_allowance')!=media or
                new_plan.get('max_http_requests')!=media+metadata+media or
                not isinstance(revision.get('old_binding_sha256'),str) or
                not re.fullmatch('[a-f0-9]{64}',revision['old_binding_sha256']) or
                not isinstance(evidence,dict) or
                not re.fullmatch('[a-f0-9]{64}',evidence.get('selection_sha256',''))):
            raise ValueError()
        return {'old_plan':old_plan,'new_plan':new_plan,'old_binding_sha256':revision['old_binding_sha256'],
            'selection_sha256':evidence['selection_sha256']}
    except (KeyError,TypeError,ValueError,AttributeError):
        raise HubError('YOUTUBE_HIGHEST_HTTP_HISTORY_INVALID',review=True) from None


def resume_http_stats(store,job,binding,folder):
    """Carry the same job's budget ledger and verified byte counters forward.

    The optional owner-operated one-byte probe has a root-owned read-only
    receipt. Its request is already in api_calls; only its at-most-one body
    byte is added here, once. Neither binding nor selected formats are changed.
    Unknown gaps fail before network dispatch rather than resetting a cap.
    """
    plan=binding['source_plan'];limit=plan['max_http_requests'];byte_limit=plan['source_bytes']*2+32_000_000
    revision=request_budget_revision(job,binding)
    old_plan=revision['old_plan'] if revision is not None else None
    old_binding_sha=revision['old_binding_sha256'] if revision is not None else None
    history_plans={digest(plan)} | ({digest(old_plan)} if old_plan is not None else set())
    request_limits={digest(plan):limit}
    if old_plan is not None:request_limits[digest(old_plan)]=old_plan['max_http_requests']
    ledger=store.query('SELECT COALESCE(SUM(units),0) n FROM api_calls WHERE provider=? AND job_id=?',(PROVIDER,job['job_id']))[0]['n']
    if type(ledger) is not int or not 0<=ledger<=limit:raise HubError('YOUTUBE_HIGHEST_REQUEST_LIMIT',review=True)
    snapshots=[];prior_hashes={}
    names=[file for file in folder.iterdir() if file.name in ('download-result.json','progress.json') or
        re.fullmatch(r'failure-before-attempt-[0-9]{1,3}\.json',file.name)]
    if len(names)>16:raise HubError('YOUTUBE_HIGHEST_HTTP_HISTORY_INVALID',review=True)
    for file in names:
        info=file.lstat()
        if not stat.S_ISREG(info.st_mode) or not 0<info.st_size<=262144:
            raise HubError('YOUTUBE_HIGHEST_HTTP_HISTORY_INVALID',review=True)
        try:raw=file.read_bytes();value=json.loads(raw)
        except (OSError,ValueError):raise HubError('YOUTUBE_HIGHEST_HTTP_HISTORY_INVALID',review=True) from None
        file_plan=value.get('source_plan') if isinstance(value,dict) else None
        file_plan_digest=digest(file_plan) if isinstance(file_plan,dict) else None
        if file_plan_digest not in history_plans:raise HubError('YOUTUBE_HIGHEST_HTTP_HISTORY_INVALID',review=True)
        http=value.get('http',{})
        if not isinstance(http,dict):raise HubError('YOUTUBE_HIGHEST_HTTP_HISTORY_INVALID',review=True)
        requests=http.get('requests');body_bytes=http.get('response_bytes')
        accounted=http.get('accounted_probe_call_ids',[])
        kinds=http.get('response_kinds',{})
        if (http.get('provider')!=PROVIDER or http.get('budget_history_reset') is not False or
                http.get('request_limit')!=request_limits[file_plan_digest] or type(requests) is not int or not 0<=requests<=ledger or
                type(body_bytes) is not int or not 0<=body_bytes<=byte_limit or
                not isinstance(accounted,list) or len(accounted)>1 or any(not isinstance(call,str) or not re.fullmatch(r'[0-9a-f-]{36}',call) for call in accounted) or
                not isinstance(kinds,dict) or any(k not in ('manifest','media','json','html','javascript','other') or type(v) is not int or not 0<=v<=requests for k,v in kinds.items())):
            raise HubError('YOUTUBE_HIGHEST_HTTP_HISTORY_INVALID',review=True)
        snapshots.append(http);prior_hashes[hashlib.sha256(raw).hexdigest()]=http
    base=max(snapshots,key=lambda item:(item['requests'],item['response_bytes'])) if snapshots else {
        'requests':0,'response_bytes':0,'response_kinds':{},'accounted_probe_call_ids':[]}
    if any(item['response_bytes']>base['response_bytes'] for item in snapshots):
        raise HubError('YOUTUBE_HIGHEST_HTTP_HISTORY_INVALID',review=True)
    carried=list(base.get('accounted_probe_call_ids',[]));body_bytes=base['response_bytes']
    probe=folder/'range-probe-v1.json';proof_call=None;probe_bytes=0
    if probe.exists() or probe.is_symlink():
        info=probe.lstat()
        if not _root_probe_permission(info) or not 0<info.st_size<=16384:
            raise HubError('YOUTUBE_HIGHEST_RANGE_PROBE_INVALID',review=True)
        try:proof=json.loads(probe.read_bytes())
        except (OSError,ValueError):raise HubError('YOUTUBE_HIGHEST_RANGE_PROBE_INVALID',review=True) from None
        if not isinstance(proof,dict):raise HubError('YOUTUBE_HIGHEST_RANGE_PROBE_INVALID',review=True)
        proof_call=proof.get('call_id');probe_bytes=proof.get('response_bytes');previous=prior_hashes.get(proof.get('prior_result_sha256'))
        if (proof.get('version')!='youtube-whole-one-byte-range-probe-v1' or proof.get('job_id')!=job['job_id'] or
                proof.get('source_plan_sha256') not in history_plans or previous is None or
                proof.get('binding_sha256',digest(binding)) not in ({digest(binding)} | ({old_binding_sha} if old_binding_sha else set())) or
                not isinstance(proof_call,str) or not re.fullmatch(r'[0-9a-f-]{36}',proof_call) or
                type(probe_bytes) is not int or probe_bytes not in (0,1)):
            raise HubError('YOUTUBE_HIGHEST_RANGE_PROBE_INVALID',review=True)
        calls=store.query('SELECT provider,job_id,units,state FROM api_calls WHERE call_id=?',(proof_call,))
        if (len(calls)!=1 or calls[0]['provider']!=PROVIDER or calls[0]['job_id']!=job['job_id'] or
                calls[0]['units']!=1 or calls[0]['state']!='done' or previous['requests']>=ledger):
            raise HubError('YOUTUBE_HIGHEST_RANGE_PROBE_INVALID',review=True)
        if proof_call not in carried:body_bytes+=probe_bytes
    if any(call!=proof_call for call in carried):raise HubError('YOUTUBE_HIGHEST_RANGE_PROBE_INVALID',review=True)
    gap=ledger-base['requests']
    if gap!=(1 if proof_call is not None and proof_call not in carried else 0):
        raise HubError('YOUTUBE_HIGHEST_HTTP_HISTORY_INCOMPLETE',review=True)
    if proof_call is not None and proof_call not in carried:carried.append(proof_call)
    if body_bytes>byte_limit:raise HubError('YOUTUBE_HIGHEST_SIZE_LIMIT',review=True)
    result={'requests':ledger,'response_bytes':body_bytes,'request_limit':limit,'provider':PROVIDER,
        'budget_history_reset':False,'response_kinds':dict(base.get('response_kinds',{}))}
    if carried:result['accounted_probe_call_ids']=carried
    return result


def checked_folder(settings,ident):
    try:
        if str(uuid.UUID(ident))!=ident:raise ValueError()
        path=settings.root/'media'/ident/'youtube-highest-v2'
        if not path.is_absolute() or path.resolve()!=path or any(p.is_symlink() for p in (path,*path.parents)):raise ValueError()
        path.mkdir(mode=0o700,parents=True,exist_ok=True)
        if os.name=='posix' and path.stat().st_mode&0o077:raise ValueError()
        return path
    except (OSError,ValueError,TypeError,AttributeError):raise HubError('YOUTUBE_HIGHEST_WORKSPACE_INVALID',review=True) from None


def select_highest(info,binding):
    validate_source_plan(binding.get('source_plan'))
    started=require_operating_date(binding.get('source_actual_start_time'))
    if (binding.get('operating_not_before')!=OPERATING_NOT_BEFORE or
            binding.get('source_plan',{}).get('source_actual_start_time')!=started or
            binding.get('source_plan',{}).get('operating_not_before')!=OPERATING_NOT_BEFORE):
        raise HubError('YOUTUBE_HIGHEST_ACTUAL_START_REQUIRED',review=True)
    if (not isinstance(info,dict) or info.get('id')!=VIDEO_ID or info.get('channel_id')!=binding['channel_id'] or
            info.get('entries') is not None or info.get('_type','video')!='video' or
            info.get('is_live') is True or info.get('live_status') in ('is_live','is_upcoming','post_live') or
            info.get('availability') not in (None,'public') or info.get('age_limit',0) or info.get('has_drm') is True or
            type(info.get('duration')) not in (int,float) or not math.isfinite(info['duration']) or
            abs(info['duration']-binding['duration'])>2):
        raise HubError('YOUTUBE_HIGHEST_SOURCE_CHANGED',review=True)
    formats=info.get('formats',[])
    if not isinstance(formats,list) or len(formats)>300:raise HubError('YOUTUBE_HIGHEST_FORMATS_INVALID',review=True)
    for fmt in formats:
        if not isinstance(fmt,dict):continue
        height,fps=fmt.get('height'),fmt.get('fps')
        if (type(height) is int and height>1080 or type(height) is int and height==1080 and
                type(fps) in (int,float) and math.isfinite(fps) and fps>60.1):
            raise HubError('YOUTUBE_HIGHEST_HIGHER_QUALITY_UNSUPPORTED',review=True)
    selected=[]
    for expected in binding['source_plan']['selected_formats']:
        found=[f for f in formats if isinstance(f,dict) and str(f.get('format_id'))==expected['format_id']]
        if len(found)!=1:raise HubError('YOUTUBE_HIGHEST_EXACT_FORMAT_REQUIRED',review=True)
        fmt=found[0];video=expected['format_id']=='299'
        if fmt.get('has_drm') is not False:
            raise HubError('YOUTUBE_HIGHEST_CLEAR_FORMAT_REQUIRED',review=True)
        if (fmt.get('protocol')!='https' or fmt.get('fragments') or fmt.get('manifest_url') or
                fmt.get('ext')!=expected['ext'] or type(fmt.get('filesize')) is not int or fmt['filesize']!=expected['filesize'] or
                not isinstance(fmt.get('vcodec'),str) or not isinstance(fmt.get('acodec'),str)):
            raise HubError('YOUTUBE_HIGHEST_FORMAT_SIZE_OR_TRANSPORT_CHANGED',review=True)
        if video:
            if (not fmt['vcodec'].startswith(('avc1','h264')) or fmt['acodec']!='none' or
                    fmt.get('width')!=1920 or fmt.get('height')!=1080 or type(fmt.get('fps')) not in (int,float) or
                    not math.isfinite(fmt['fps']) or not 59.9<=fmt['fps']<=60.1):
                raise HubError('YOUTUBE_HIGHEST_QUALITY_CHANGED',review=True)
        elif fmt['vcodec']!='none' or not fmt['acodec'].startswith(('mp4a','aac')):
            raise HubError('YOUTUBE_HIGHEST_QUALITY_CHANGED',review=True)
        legacy.destination(fmt.get('url'),track=True)
        if any(k.lower() in ('cookie','authorization','proxy-authorization') for k in (fmt.get('http_headers') or {})):
            raise HubError('YOUTUBE_DOWNLOAD_AUTHENTICATION_FORBIDDEN')
        selected.append(fmt)
    return selected


def budget_limits(binding):
    b=binding['transport_budget']
    if b!={'provider':PROVIDER,'daily_requests':2500,'monthly_requests':30000,'history_reset':False}:
        raise HubError('YOUTUBE_HIGHEST_BUDGET_INVALID',review=True)
    return {PROVIDER:dict(daily_usd=0,monthly_usd=0,daily_calls=2500,monthly_calls=30000,max_call_usd=0,paid_authorized=False)}


def preflight_budget(store,binding,remaining,*,limits_function=budget_limits,request_cap=2048):
    limits=limits_function(binding)[PROVIDER];now=datetime.now(ZoneInfo('Asia/Shanghai'))
    starts={'daily':now.replace(hour=0,minute=0,second=0,microsecond=0).timestamp(),
            'monthly':now.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp()}
    if (type(request_cap) is not int or not 1<=request_cap<=20000 or
            type(remaining) is not int or not 0<=remaining<=request_cap):raise HubError('YOUTUBE_HIGHEST_BUDGET_INVALID',review=True)
    with store.transaction() as db:
        for label,start in starts.items():
            used=db.execute('SELECT COALESCE(SUM(units),0) FROM api_calls WHERE provider=? AND started_at>=?',(PROVIDER,start)).fetchone()[0]
            if used+remaining>limits[label+'_calls']:raise HubError('API_BUDGET_EXCEEDED',review=True)


def needed_disk(folder,binding):
    plan=binding['source_plan'];existing=legacy.bounded_files(folder,plan['required_working_bytes'])
    return max(0,plan['required_working_bytes']-existing)


def probe_track(file,kind,check):
    raw=bytearray();command=['ffprobe','-v','error','-protocol_whitelist','file,pipe','-show_data_hash','sha256',
        '-show_entries','stream=index,codec_type,codec_name,extradata_hash,width,height,avg_frame_rate,r_frame_rate,sample_rate,channels:format=duration,size',
        '-of','json',str(file)]
    for line in process_lines(command,file.parent,30,check):
        raw.extend(line)
        if len(raw)>65536:raise HubError('YOUTUBE_HIGHEST_PROBE_INVALID',review=True)
    try:
        value=json.loads(raw);streams=value['streams'];duration=float(value['format']['duration'])
        if len(streams)!=1 or streams[0]['codec_type']!=kind or not math.isfinite(duration) or duration<=0:
            raise ValueError()
        return {k:v for k,v in streams[0].items() if k not in ('index','codec_type')},duration
    except (ValueError,KeyError,TypeError):raise HubError('YOUTUBE_HIGHEST_PROBE_INVALID',review=True) from None


class HighestReplayDownloader:
    VERSION=VERSION
    CHILD_MODULE='adapters.ytdlp_highest_v2'
    PLAN_VALIDATOR=staticmethod(validate_source_plan)
    NEEDED_DISK=staticmethod(needed_disk)
    MAX_PACKET_COUNT=10_000_000
    PACKET_TIMEOUT=7200

    def __init__(self,settings):
        self.settings=settings;self.store=Store(settings.db_path);self.maintenance=Maintenance(settings,self.store)

    def folder(self,ident):return checked_folder(self.settings,ident)

    def validate_quality(self,video,audio,binding):
        if (video['codec_name']!='h264' or video.get('width')!=1920 or video.get('height')!=1080 or audio['codec_name']!='aac' or
                any(Fraction(video[k]) not in (Fraction(60),Fraction(60000,1001)) for k in ('avg_frame_rate','r_frame_rate'))):
            raise HubError('YOUTUBE_HIGHEST_QUALITY_CHANGED',review=True)

    def acquire(self,job,binding,*,check):
        folder=self.folder(job['job_id']);plan=binding['source_plan'];check()
        self.maintenance.check_disk(self.NEEDED_DISK(folder,binding))
        receipt_file=folder/'verified.json'
        if receipt_file.exists():
            if receipt_file.is_symlink() or receipt_file.stat().st_size>262144:raise HubError('YOUTUBE_HIGHEST_RECEIPT_INVALID',review=True)
            receipt=json.loads(receipt_file.read_bytes())
            asset=receipt.get('source_asset',{});file=Path(asset.get('file',''))
            if (receipt.get('version')!=self.VERSION or receipt.get('binding_sha256')!=digest(binding) or
                    file!=folder/'original.mp4' or not file.is_file() or file.is_symlink() or
                    file.stat().st_size!=asset.get('bytes') or sha256_file(file)!=asset.get('sha256')):
                raise HubError('YOUTUBE_HIGHEST_RESUME_BINDING_CHANGED',review=True)
            check();return receipt
        ticket=folder/'ticket.json';output=folder/'download-result.json'
        atomic_json(ticket,{'root':str(self.settings.root),'job_id':job['job_id'],'lease_owner':job['lease_owner'],'binding':binding});ticket.chmod(0o600)
        result=None
        if output.exists():
            if output.is_symlink() or output.stat().st_size>262144:raise HubError('YOUTUBE_HIGHEST_RESULT_INVALID',review=True)
            previous=json.loads(output.read_bytes())
            if not previous.get('error_code') and previous.get('binding_sha256')==digest(binding):result=previous
            else:
                atomic_json(folder/('failure-before-attempt-'+str(job['attempts'])+'.json'),previous);output.unlink()
        if result is None:result=self._helper(job,binding,folder,ticket,output,check)
        return self._finalize(job,binding,folder,result,check)

    def _helper(self,job,binding,folder,ticket,output,check):
        try:
            process=subprocess.Popen([sys.executable,'-m',self.CHILD_MODULE,'--download',str(ticket)],
                cwd=Path(__file__).resolve().parents[1],env=legacy.private_environment(self.settings.root),stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=os.name!='nt')
        except OSError:raise HubError('YOUTUBE_HIGHEST_RUNTIME_UNAVAILABLE',retryable=True) from None
        deadline=time.monotonic()+binding['timeout_seconds']
        try:
            while process.poll() is None:
                check();self.maintenance.check_disk(self.NEEDED_DISK(folder,binding))
                if time.monotonic()>deadline:raise HubError('YOUTUBE_HIGHEST_TIMEOUT',retryable=True)
                time.sleep(.5)
            check()
            if output.is_symlink() or not output.is_file() or output.stat().st_size>262144:
                raise HubError('YOUTUBE_HIGHEST_HELPER_INTERRUPTED',retryable=True)
            result=json.loads(output.read_bytes())
            if process.returncode or result.get('error_code'):
                raise HubError(result.get('error_code','YOUTUBE_HIGHEST_DOWNLOAD_FAILED'),retryable=result.get('retryable') is True,
                    review=result.get('review') is True)
            if result.get('binding_sha256')!=digest(binding):raise HubError('YOUTUBE_HIGHEST_RESULT_INVALID',review=True)
            return result
        finally:
            if process.poll() is None:
                process.terminate() if os.name=='nt' else os.killpg(process.pid,signal.SIGTERM)
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill() if os.name=='nt' else os.killpg(process.pid,signal.SIGKILL)
                    process.wait(timeout=5)

    def _finalize(self,job,binding,folder,result,check):
        plan=self.PLAN_VALIDATOR(binding['source_plan']);tracks=result['tracks']
        if result.get('source_plan')!=plan or result.get('selected_formats')!=plan['selected_formats'] or len(tracks)!=2:
            raise HubError('YOUTUBE_HIGHEST_RESULT_INVALID',review=True)
        sources=[]
        for track,expected in zip(tracks,plan['selected_formats']):
            file=folder/('stream-'+expected['format_id']+'.'+expected['ext'])
            if (any(track.get(k)!=v for k,v in expected.items()) or file.is_symlink() or not file.is_file() or
                    file.stat().st_size!=expected['filesize'] or track.get('bytes')!=expected['filesize'] or sha256_file(file)!=track.get('sha256')):
                raise HubError('YOUTUBE_HIGHEST_TRACK_CHANGED',review=True)
            sources.append(file)
        guard=None
        for _ in range(600):
            check();self.maintenance.check_disk(self.NEEDED_DISK(folder,binding));candidate=resource_guard(self.settings.root/'data/locks/heavy')
            try:candidate.__enter__();guard=candidate;break
            except HubError as exc:
                if exc.code!='HEAVY_RESOURCE_BUSY':raise
                time.sleep(.5)
        if guard is None:raise HubError('HEAVY_RESOURCE_BUSY',retryable=True)
        try:
            def verify_check():check();self.maintenance.check_disk(self.NEEDED_DISK(folder,binding))
            source_config={};source_durations={}
            for file,kind in zip(sources,('video','audio')):
                config,duration=probe_track(file,kind,verify_check);source_config[kind]=config;source_durations[kind]=duration
                if abs(duration-binding['duration'])>2:raise HubError('YOUTUBE_HIGHEST_DURATION_CHANGED',review=True)
            video=source_config['video'];audio=source_config['audio']
            self.validate_quality(video,audio,binding)
            source_proof=(legacy.source_packet_evidence(sources,verify_check) if self.MAX_PACKET_COUNT==10_000_000 and self.PACKET_TIMEOUT==7200 else
                legacy.source_packet_evidence(sources,verify_check,max_packets=self.MAX_PACKET_COUNT,timeout=self.PACKET_TIMEOUT))
            merged=folder/'merged.partial.mp4';final=folder/'original.mp4'
            if merged.is_symlink() or final.is_symlink():raise HubError('YOUTUBE_HIGHEST_WORKSPACE_INVALID',review=True)
            command=['ffmpeg','-nostdin','-v','error','-y','-protocol_whitelist','file,pipe','-i',str(sources[0]),
                '-protocol_whitelist','file,pipe','-i',str(sources[1]),'-map','0:v:0','-map','1:a:0','-c','copy','-threads','1','-f','mp4',str(merged)]
            for _ in process_lines(command,folder,min(7200,binding['timeout_seconds']),verify_check):pass
            details=inspect_media(merged,check=verify_check,include_frame_rates=True)
            if (details['bytes']>plan['planned_output_max_bytes'] or abs(details['duration']-binding['duration'])>2 or
                    details['stream_config']!=source_config):raise HubError('YOUTUBE_HIGHEST_MERGE_CHANGED',review=True)
            proof=(packet_evidence(merged,details['stream_types'],timeout=7200,check=verify_check) if self.MAX_PACKET_COUNT==10_000_000 and self.PACKET_TIMEOUT==7200 else
                packet_evidence(merged,details['stream_types'],timeout=self.PACKET_TIMEOUT,check=verify_check,max_packets=self.MAX_PACKET_COUNT))
            if proof!=source_proof:raise HubError('YOUTUBE_HIGHEST_PACKET_MISMATCH',review=True)
            for file,track in zip(sources,tracks):
                if file.stat().st_size!=track['bytes'] or sha256_file(file)!=track['sha256']:
                    raise HubError('YOUTUBE_HIGHEST_TRACK_CHANGED',review=True)
            digest_file=sha256_file(merged);verify_check()
            if final.exists():
                if final.stat().st_size!=details['bytes'] or sha256_file(final)!=digest_file:
                    raise HubError('YOUTUBE_HIGHEST_EXISTING_OUTPUT_CONFLICT',review=True)
                merged.unlink()  # Only this reproducible derived merge; original is retained.
            else:os.replace(merged,final)
            with final.open('r+b') as stream:os.fsync(stream.fileno())
            if os.name!='nt':
                descriptor=os.open(folder,os.O_DIRECTORY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
            asset={'file':str(final),'bytes':details['bytes'],'sha256':digest_file,'duration':details['duration'],
                'stream_types':{str(k):v for k,v in details['stream_types'].items()},'stream_config':details['stream_config'],'packet_evidence':proof,
                'source_plan':plan,'selected_formats':plan['selected_formats']}
            receipt={'version':self.VERSION,'binding_sha256':digest(binding),'source_asset':asset,
                'receipt':{'yt_dlp_version':legacy.YTDLP_VERSION,'ejs_version':legacy.EJS_VERSION,'tracks':tracks,'http':result['http'],
                    'source_plan':plan,'selected_formats':plan['selected_formats'],'source_stream_config':source_config,
                    'source_track_durations':source_durations,'local_merge':'ffmpeg_stream_copy','resumable_partials_retained':True,
                    'authenticated_session':True,'credential_origin_scope':['https://www.youtube.com'],'hash_kind':'computed_after_download',
                    'expected_source_hash_verified':False,'source_to_merged_packet_coverage_verified':True},
                'human_review':False,'publication_authorized':False,'creator_original_bytes_verified':False}
            atomic_json(folder/'verified.json',receipt);(folder/'verified.json').chmod(0o600);return receipt
        finally:guard.__exit__(None,None,None)


def child_download(ticket,*,service_type=YouTubeHighestDownload,folder_fn=checked_folder,
        plan_validator=validate_source_plan,selector=select_highest,version_label=VERSION,
        video_id=VIDEO_ID,metadata_only=False,limits_function=budget_limits,observation_recorder=None,
        disk_needed=needed_disk,maintenance_factory=Maintenance,request_cap=2048,media_start_guard=None):
    from services.hub.config import Settings
    from yt_dlp.networking.common import Request,Response
    from yt_dlp.networking.exceptions import HTTPError
    from yt_dlp.extractor.youtube._video import YoutubeIE as BaseYoutubeIE
    from yt_dlp.version import __version__
    from importlib.metadata import version
    import yt_dlp
    data=json.loads(ticket.read_bytes());settings=Settings.load();folder=folder_fn(settings,data['job_id']);output=folder/'download-result.json'
    if ticket!=folder/'ticket.json' or ticket.is_symlink():raise HubError('YOUTUBE_HIGHEST_WORKSPACE_INVALID')
    store=Store(settings.db_path);service=service_type(settings,store);job=store.get(data['job_id']);binding=data['binding']
    if not job or job.get('lease_owner')!=data['lease_owner']:raise HubError('YOUTUBE_HIGHEST_LEASE_LOST')
    job['lease_owner']=data['lease_owner'];maintenance=maintenance_factory(settings,store);plan=binding['source_plan']
    stats={'requests':0,'response_bytes':0,'request_limit':plan['max_http_requests'],'provider':PROVIDER,'budget_history_reset':False}
    budget=Budget(store,limits_function(binding));last_progress=0;info=None;observation=None
    active_track=None;active_track_url=None;range_failure=None;access_failure=None
    def check():
        service.check_binding(job,binding);maintenance.check_disk(disk_needed(folder,binding))
    def progress(message):
        nonlocal last_progress
        check()
        if time.monotonic()-last_progress>=2:
            last_progress=time.monotonic();atomic_json(folder/'progress.json',{'version':version_label,'checked_at':time.time(),
                'format_id':message.get('info_dict',{}).get('format_id'),'status':message.get('status'),
                'downloaded_bytes':message.get('downloaded_bytes'),'total_bytes':message.get('total_bytes'),'http':stats,'source_plan':plan})
    class Log:
        def debug(self,*a):pass
        def warning(self,*a):pass
        def error(self,*a):pass
    class YoutubeIE(BaseYoutubeIE):
        def _extract_player_response(self,*a,**k):
            nonlocal access_failure
            value=super()._extract_player_response(*a,**k)
            try:clear_stream_player_guard(value)
            except HubError as exc:
                if exc.code=='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED':access_failure=access_response_diagnostic('client',video_id,value,client=a[0] if a else k.get('client'),request_index=stats['requests'])
                raise
            return value
        def _extract_player_responses(self,clients,video_id,webpage,webpage_client,webpage_ytcfg,is_premium_subscriber):
            nonlocal access_failure
            if webpage:
                initial=self._search_json(self._YT_INITIAL_PLAYER_RESPONSE_RE,webpage,'initial player protection check',video_id,fatal=False)
                try:clear_stream_player_guard(initial)
                except HubError as exc:
                    if exc.code=='YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED':access_failure=access_response_diagnostic('initial',video_id,initial,client=webpage_client,request_index=stats['requests'])
                    raise
            return super()._extract_player_responses(clients,video_id,webpage,webpage_client,webpage_ytcfg,is_premium_subscriber)
        @staticmethod
        def _is_agegated(response):
            nonlocal access_failure
            if BaseYoutubeIE._is_agegated(response):
                access_failure=access_response_diagnostic('age',video_id,response,request_index=stats['requests'])
                raise HubError('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED')
            return False
    class StreamResponse(Response):
        def __init__(self,connection,response,url):
            super().__init__(response,url,dict(response.getheaders()),status=response.status);self.connection=connection;self.count=0
            self.bound=plan['source_bytes'] if legacy.destination(url).endswith('.googlevideo.com') else 8_388_608
        def read(self,amt=None):
            check();metadata=amt is None or amt<0
            raw=self.fp.read(min(8_388_609,self.bound-self.count+1)) if metadata else self.fp.read(min(amt,8_388_608))
            self.count+=len(raw);stats['response_bytes']+=len(raw)
            if metadata and len(raw)>8_388_608:raise HubError('YOUTUBE_DOWNLOAD_METADATA_RESPONSE_LIMIT')
            if self.count>self.bound or stats['response_bytes']>plan['source_bytes']*2+32_000_000:raise HubError('YOUTUBE_HIGHEST_SIZE_LIMIT')
            return raw
        def close(self):
            try:super().close()
            finally:self.connection.close()
    class Guarded(yt_dlp.YoutubeDL):
        def urlopen(self,request):
            nonlocal range_failure,access_failure
            range_failure=None
            request=Request(request) if isinstance(request,str) else request
            if (not isinstance(request,Request) or any(request.proxies.values()) or request.extensions.get('impersonate') or
                    request.method not in ('GET','HEAD','POST') or request.data is not None and
                    (not isinstance(request.data,bytes) or len(request.data)>1_048_576)):
                raise HubError('YOUTUBE_DOWNLOAD_REQUEST_REJECTED')
            url=request.url;origin=legacy.destination(url)
            for hop in range(4):
                check();host=legacy.destination(url);headers=legacy.scoped_headers(self.params.get('http_headers'),request.headers,url,
                    authenticated=True,jar=self.cookiejar,original_host=origin)
                if stats['requests']>=plan['max_http_requests']:raise HubError('YOUTUBE_HIGHEST_REQUEST_LIMIT')
                call=budget.reserve(PROVIDER,job['job_id']);stats['requests']+=1;connection=None;response=None;wrapped=None;diagnostic=None
                try:
                    connection,response=open_request(url,[host],method=request.method,headers=headers,body=request.data)
                    status=response.status
                    mime=(response.getheader('Content-Type','').split(';',1)[0] or 'unknown').lower()
                    kind=('manifest' if 'mpegurl' in mime or 'dash+xml' in mime else 'media' if mime.startswith(('video/','audio/')) else
                        'json' if 'json' in mime else 'html' if 'html' in mime else 'javascript' if 'javascript' in mime else 'other')
                    counts=stats.setdefault('response_kinds',{});counts[kind]=counts.get(kind,0)+1
                    range_value=next((v for k,v in headers.items() if k.lower()=='range'),None)
                    if range_value is not None:
                        diagnostic=range_response_diagnostic(range_value,status,response.getheader('Content-Range'),
                            response.getheader('Content-Type'),response.getheader('Content-Length'),active_track,
                            active_track is not None and request.url==active_track_url and host.endswith('.googlevideo.com'))
                        diagnostic['request_index']=stats['requests']
                    if status in (301,302,303,307,308):
                        if request.method not in ('GET','HEAD') or hop==3:raise HubError('YOUTUBE_DOWNLOAD_REDIRECT_REJECTED')
                        url=urljoin(url,response.getheader('Location',''));legacy.destination(url);connection.close();connection=None;budget.finish(call,'done');continue
                    if status in (401,403):
                        access_failure=access_response_diagnostic('http',video_id,request_index=stats['requests'],http_status=status)
                        raise HubError('YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED')
                    if status==429:raise HubError('YOUTUBE_DOWNLOAD_RATE_LIMITED')
                    if status>=500:raise HubError('YOUTUBE_DOWNLOAD_NETWORK_ERROR',retryable=True)
                    if metadata_only and kind=='media':raise HubError('YOUTUBE_NATURAL_PLANNING_MEDIA_FORBIDDEN',review=True)
                    if host=='www.youtube.com':
                        self.cookiejar.extract_cookies(response,CookieRequest(url))
                        for cookie in list(self.cookiejar):
                            if cookie.domain not in legacy.COOKIE_DOMAINS:self.cookiejar.clear(cookie.domain,cookie.path,cookie.name)
                    if diagnostic and (diagnostic['requested_start'] is not None and diagnostic['requested_start']>0 or
                            active_track is not None and not diagnostic['requested_range_valid']):
                        if diagnostic['rejection_reason'] is not None:
                            raise HubError('YOUTUBE_HIGHEST_RESUME_RANGE_NOT_HONORED',review=True)
                    wrapped=StreamResponse(connection,response,url);connection=None;budget.finish(call,'done')
                    if status not in (200,206):raise HTTPError(wrapped)
                    if response.getheader('Content-Encoding','identity') not in ('identity',''):
                        wrapped.close();raise HubError('YOUTUBE_DOWNLOAD_CONTENT_ENCODING_REJECTED')
                    return wrapped
                except BaseException as exc:
                    if diagnostic is not None:range_failure=diagnostic
                    # Reject before exposing a response body or allowing yt-dlp's
                    # invalid-Range fallback to reopen an existing partial in wb.
                    try:
                        if wrapped is not None:wrapped.close()
                        elif response is not None:response.close()
                    except OSError:pass
                    budget.finish(call,'error',getattr(exc,'code','YOUTUBE_DOWNLOAD_NETWORK_ERROR'));raise
                finally:
                    if connection:connection.close()
            raise HubError('YOUTUBE_DOWNLOAD_REDIRECT_REJECTED')
    try:
        if __version__!=legacy.YTDLP_VERSION or version('yt-dlp-ejs')!=legacy.EJS_VERSION:
            raise HubError('YOUTUBE_DOWNLOAD_DEPENDENCY_VERSION_CHANGED')
        plan_validator(plan)
        if not metadata_only:stats.update(resume_http_stats(store,job,binding,folder))
        check();session=legacy.load_youtube_session(settings,binding)
        options={'quiet':True,'logger':Log(),'cachedir':False,'noplaylist':True,'proxy':'','usenetrc':False,'cookiefile':None,
            'cookiesfrombrowser':None,'username':None,'password':None,'nocheckcertificate':False,'geo_bypass':False,'xff':'never',
            'extractor_retries':0,'retries':0,'fragment_retries':0,'socket_timeout':20,'concurrent_fragment_downloads':1,
            'skip_unavailable_fragments':False,'js_runtimes':{'node':{'path':'/usr/local/bin/node'}},'remote_components':[],
            'postprocessors':[],'fixup':'never','continuedl':True,'nopart':False,'overwrites':False,'max_filesize':plan['source_bytes'],
            'ratelimit':2_000_000,'buffersize':65536,'noprogress':True,'progress_hooks':[progress],'mark_watched':False,
            'impersonate':None,'writeinfojson':False,'writesubtitles':False,'writethumbnail':False,'getcomments':False,
            'allowed_extractors':['youtube'],'extractor_args':{'youtube':{'player_client':list(BaseYoutubeIE._DEFAULT_AUTHED_CLIENTS)}}}
        with Guarded(options) as ydl:
            ydl.cookiejar=session;ydl.add_info_extractor(YoutubeIE())
            info=ydl.extract_info('https://www.youtube.com/watch?v='+video_id,download=False)
            if observation_recorder is not None:observation=observation_recorder(info,binding,folder,stats)
            selected=selector(info,binding)
            if not metadata_only:require_clear_selected_formats(selected)
            if metadata_only:
                atomic_json(output,{'version':version_label,'binding_sha256':digest(binding),'source_plan':selected,
                    'http':stats,'media_downloaded':False,'human_review':False,'publication_authorized':False,
                    **({'metadata_observation':observation} if observation is not None else {})})
                output.chmod(0o600);return 0
            if media_start_guard is not None:media_start_guard(service,job,binding)
            remaining=max(0,plan['max_http_requests']-stats['requests'])
            if request_cap==2048:preflight_budget(store,binding,remaining,limits_function=limits_function)
            else:preflight_budget(store,binding,remaining,limits_function=limits_function,request_cap=request_cap)
            expected={'binding_sha256':digest(binding),'source_plan':plan,'selected_formats':plan['selected_formats']}
            selection=folder/'selection.json'
            if selection.exists():
                if selection.is_symlink() or selection.stat().st_size>262144:raise HubError('YOUTUBE_HIGHEST_RESUME_FORMAT_CHANGED',review=True)
                previous=json.loads(selection.read_bytes())
                if previous!=expected:
                    revision=request_budget_revision(job,binding)
                    if (revision is None or digest(previous)!=revision['selection_sha256'] or
                            previous!={'binding_sha256':revision['old_binding_sha256'],'source_plan':revision['old_plan'],
                                'selected_formats':revision['old_plan']['selected_formats']}):
                        raise HubError('YOUTUBE_HIGHEST_RESUME_FORMAT_CHANGED',review=True)
            atomic_json(selection,expected);selection.chmod(0o600);tracks=[]
            for fmt,identity in zip(selected,plan['selected_formats']):
                check();file=folder/('stream-'+identity['format_id']+'.'+identity['ext'])
                if file.is_symlink() or Path(str(file)+'.part').is_symlink():raise HubError('YOUTUBE_HIGHEST_WORKSPACE_INVALID',review=True)
                active_track=identity;active_track_url=fmt['url']
                try:ok,_=ydl.dl(str(file),fmt)
                finally:active_track=None;active_track_url=None
                if not ok or not file.is_file() or file.stat().st_size!=identity['filesize']:
                    raise HubError('YOUTUBE_HIGHEST_TRACK_INCOMPLETE',review=True)
                tracks.append({**identity,'bytes':file.stat().st_size,'sha256':sha256_file(file)})
        atomic_json(output,{'binding_sha256':digest(binding),'tracks':tracks,'http':stats,'source_plan':plan,'selected_formats':plan['selected_formats']})
        output.chmod(0o600);return 0
    except HubError as exc:error={'error_code':exc.code,'retryable':exc.retryable,'review':exc.review}
    except (OSError,http.client.HTTPException):error={'error_code':'YOUTUBE_DOWNLOAD_NETWORK_ERROR','retryable':True,'review':False}
    except Exception as exc:
        text=str(exc).lower();restricted=any(x in text for x in ('403','sign in','captcha','not a bot','age-restrict','country','drm','po token'))
        error={'error_code':'YOUTUBE_DOWNLOAD_ACCESS_RESTRICTED' if restricted else 'YOUTUBE_HIGHEST_EXTRACTION_FAILED','retryable':False,'review':True}
    atomic_json(output,{**error,'http':stats,'partial_files_retained':True,'source_plan':plan,
        **({'range_diagnostic':range_failure} if range_failure is not None else {}),
        **({'access_diagnostic':access_failure} if access_failure is not None else {}),
        **({'metadata_observation':observation} if observation is not None else {})});output.chmod(0o600);return 1


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1]!='--download':raise SystemExit('INTERNAL_HIGHEST_DOWNLOAD_HELPER_ONLY')
    raise SystemExit(child_download(Path(sys.argv[2])))
