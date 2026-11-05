"""Opt-in, operator-only public material acquisition; no production route or upload.

Only a fixed official-monitor video binding and a separate explicit platform
download permission can reach yt-dlp. Its optional dependency is never installed
or updated here. A conservative single HTTPS MP4 is fetched through assets.py;
no HLS/DASH/network FFmpeg, cookies, proxy, PO token or client rotation.
"""
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from urllib.parse import parse_qs,urljoin,urlsplit

from adapters.assets import download
from adapters.media_tools import inspect_media,packet_evidence
from adapters.safe_https import open_request
from services.hub.budget import Budget
from services.hub.db import Store,encode
from services.hub.errors import HubError
from services.hub.evidence import sha256_file
from services.hub.locks import resource_guard
from services.hub.maintenance import Maintenance
from services.hub.media_processing import atomic_json
from services.hub.video_pipeline import VideoPipeline

VERSION='youtube-public-material-v1'
STATE='youtube_public_material_v1'
YTDLP_VERSION='2026.10.26'
PROVIDER='youtube_public_material'
MAXIMUM=8_000_000_000
WEB_LIMITS={PROVIDER:{'daily_usd':0,'monthly_usd':0,'daily_calls':100,'monthly_calls':1000,
                     'max_call_usd':0,'paid_authorized':False}}


def digest(value):return hashlib.sha256(encode(value).encode()).hexdigest()


def validate_url(url,*,media=False):
    try:
        if not isinstance(url,str) or len(url)>16384 or any(ord(c)<33 for c in url):raise ValueError()
        value=urlsplit(url);host=value.hostname
        if (value.scheme!='https' or value.username or value.password or value.fragment or value.port not in (None,443)
                or not host or not re.fullmatch(r'[a-z0-9.-]+',host)):raise ValueError()
        cdn=host.endswith('.googlevideo.com') and host!='googlevideo.com'
        if not cdn and (media or host not in {'www.youtube.com','youtube.com','m.youtube.com','s.ytimg.com','i.ytimg.com'}):raise ValueError()
        if media and value.path!='/videoplayback':raise ValueError()
        if {'pot','po_token','token'} & set(parse_qs(value.query)):raise HubError('YOUTUBE_PLATFORM_TOKEN_REQUIRED')
        return host
    except (ValueError,TypeError,AttributeError):raise HubError('YOUTUBE_MATERIAL_DESTINATION_NOT_ALLOWED') from None


def open_media_request(url,allowed_hosts):
    """Preserve platform/rate restrictions before the generic asset adapter."""
    validate_url(url,media=True)
    connection,response=open_request(url,allowed_hosts)
    code=('YOUTUBE_MATERIAL_RATE_LIMITED' if response.status==429 else
          'YOUTUBE_MATERIAL_ACCESS_RESTRICTED' if response.status in (401,403) else
          'YOUTUBE_MATERIAL_REDIRECT_NOT_ALLOWED' if 300<=response.status<400 else None)
    if code:
        connection.close();raise HubError(code)
    return connection,response


def _seconds(value):
    match=re.fullmatch(r'P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?',value or '')
    if not match or not any(match.groups()):raise HubError('YOUTUBE_OFFICIAL_DURATION_INVALID')
    seconds=sum(float(v or 0)*n for v,n in zip(match.groups(),(86400,3600,60,1)))
    if not 0<seconds<=86400:raise HubError('YOUTUBE_OFFICIAL_DURATION_INVALID')
    return seconds


class YouTubePublicMaterial:
    def __init__(self,settings,store,*,extractor=None,fetch=None,inspector=None,packets=None):
        self.settings,self.store=settings,store
        self.extractor=extractor or self._extract
        self.fetch=fetch;self.inspector=inspector or inspect_media;self.packets=packets or packet_evidence
        self.custom_transport=extractor is not None or fetch is not None
        self.custom_extractor=extractor is not None
        self.custom_media_validation=inspector is not None or packets is not None
        self.maintenance=Maintenance(settings,store)

    def _binding(self,job):
        if self.settings.config.get('youtube_public_material_enabled') is not True:raise HubError('YOUTUBE_PUBLIC_MATERIAL_DISABLED')
        current=self.store.get(job.get('job_id')) if isinstance(job,dict) else None
        if (not current or current['payload']!=job['payload'] or current['source_platform']!='youtube' or
                current['job_type']!='video_process' or current['queue']!='video' or current['state']!='processing' or
                not job.get('lease_owner') or current['lease_owner']!=job['lease_owner'] or
                (current.get('lease_until') or 0)<=time.time()):raise HubError('YOUTUBE_MATERIAL_LEASE_OR_INPUT_CHANGED')
        try:
            if str(uuid.UUID(current['job_id']))!=current['job_id']:raise ValueError()
        except (ValueError,TypeError,AttributeError):raise HubError('YOUTUBE_MATERIAL_IDENTITY_INVALID') from None
        ident=current['source_id'];source=current['payload'];channel=source.get('channel_id')
        channels={s['youtube_channel_id'] for s in self.settings.config['sources']}
        if (not re.fullmatch(r'[A-Za-z0-9_-]{11}',ident) or not isinstance(channel,str) or
                not re.fullmatch(r'UC[A-Za-z0-9_-]{22}',channel) or channel not in channels or source.get('video_id')!=ident or
                source.get('url')!='https://www.youtube.com/watch?v='+ident or source.get('source_api')!='youtube-data-v3-official'):
            raise HubError('YOUTUBE_MATERIAL_IDENTITY_INVALID')
        observations=self.store.query('SELECT * FROM youtube_videos WHERE video_id=?',(ident,))
        if (not observations or observations[0]['state']!='ended' or observations[0]['source_api']!='youtube-data-v3-official' or
                observations[0]['channel_id']!=channel or observations[0]['replay_job_id']!=current['job_id'] or
                observations[0]['duration']!=source.get('duration') or
                not observations[0]['last_api_check'] or observations[0]['last_api_check']>time.time()+30 or
                json.loads(observations[0]['metadata'])!=source.get('raw')):raise HubError('YOUTUBE_OFFICIAL_METADATA_BINDING_REQUIRED')
        raw=source['raw']
        if (raw.get('id')!=ident or raw.get('snippet',{}).get('channelId')!=channel or
                raw.get('status',{}).get('privacyStatus')!='public' or not raw.get('liveStreamingDetails',{}).get('actualEndTime') or
                raw.get('contentDetails',{}).get('duration')!=source.get('duration')):
            raise HubError('YOUTUBE_OFFICIAL_METADATA_BINDING_REQUIRED')
        duration=_seconds(source['duration'])
        # Reuse the existing per-video rights parser. Its old source_sha256
        # continues to mean an expected hash, never a newly invented attestation.
        grant=VideoPipeline(self.settings,self.store,None).rights(ident)
        if (not grant or 'download' not in grant.get('actions',[]) or not isinstance(grant.get('evidence'),str) or
                not grant['evidence'].strip() or not isinstance(grant.get('authorized_by'),str) or not grant['authorized_by'].strip()):
            raise HubError('YOUTUBE_DOWNLOAD_RIGHTS_REQUIRED')
        maximum=grant.get('max_bytes')
        if type(maximum) is not int or not 0<maximum<=min(MAXIMUM,self.settings.config['max_media_bytes']):
            raise HubError('YOUTUBE_MATERIAL_SIZE_BOUND_REQUIRED')
        permit=grant.get('youtube_acquisition')
        if (not isinstance(permit,dict) or permit.get('version')!='youtube-public-material-grant-v1' or
                permit.get('channel_id')!=channel or permit.get('metadata_sha256')!=digest(raw) or
                not isinstance(permit.get('platform_download_permission_reference'),str) or
                not permit['platform_download_permission_reference'].strip()):raise HubError('YOUTUBE_PLATFORM_DOWNLOAD_PERMISSION_REQUIRED')
        expected=grant.get('source_sha256')
        if expected is not None and (not isinstance(expected,str) or not re.fullmatch('[a-f0-9]{64}',expected)):
            raise HubError('YOUTUBE_MATERIAL_EXPECTED_HASH_INVALID')
        if not 1<=current['attempts']<=min(4,self.settings.config['max_attempts']):raise HubError('YOUTUBE_MATERIAL_RETRY_EXHAUSTED')
        binding={'version':VERSION,'job_id':current['job_id'],'video_id':ident,'channel_id':channel,
            'metadata_sha256':digest(raw),'source_payload_sha256':digest(source),'rights_sha256':digest(grant),
            'duration':duration,'max_bytes':maximum,'expected_sha256':expected,
            'metadata_binding_kind':'persisted_official_monitor_record','metadata_http_receipt_verified':False}
        return current,binding

    def _check(self,job,binding,reserve=0):
        current,now=self._binding(job)
        if now!=binding:raise HubError('YOUTUBE_MATERIAL_BINDING_CHANGED')
        self.maintenance.check_disk(reserve)
        return current

    def _select(self,value,binding):
        if (not isinstance(value,dict) or value.get('_type') in ('playlist','multi_video') or value.get('entries') is not None or
                value.get('id')!=binding['video_id'] or value.get('channel_id')!=binding['channel_id']):
            raise HubError('YOUTUBE_EXTRACTED_IDENTITY_CHANGED')
        if (value.get('is_live') is True or value.get('availability') not in (None,'public') or value.get('age_limit',0) or value.get('has_drm') is True):
            raise HubError('YOUTUBE_MATERIAL_ACCESS_RESTRICTED')
        duration=value.get('duration')
        if type(duration) not in (int,float) or not math.isfinite(duration) or abs(duration-binding['duration'])>2:
            raise HubError('YOUTUBE_EXTRACTED_DURATION_MISMATCH')
        candidates=[]
        for item in value.get('formats',[]):
            if (not isinstance(item,dict) or item.get('protocol')!='https' or item.get('ext')!='mp4' or
                    item.get('has_drm') is True or item.get('fragments') or item.get('manifest_url') or
                    not isinstance(item.get('vcodec'),str) or not item['vcodec'].startswith(('avc1','h264')) or
                    not isinstance(item.get('acodec'),str) or not item['acodec'].startswith(('mp4a','aac'))):continue
            size=item.get('filesize')
            if type(size) is not int or not 0<size<=binding['max_bytes']:continue
            validate_url(item.get('url'),media=True)
            if item.get('http_headers') and any(k.lower() in ('cookie','authorization','proxy-authorization') for k in item['http_headers']):
                raise HubError('YOUTUBE_MATERIAL_AUTHENTICATION_NOT_ALLOWED')
            candidates.append(item)
        if not candidates:raise HubError('YOUTUBE_PUBLIC_SINGLE_FILE_FORMAT_UNAVAILABLE')
        return max(candidates,key=lambda f:(f.get('height') or 0,f['filesize']))

    def _run_helper(self,job,binding,folder,mode,extra=None):
        ticket=folder/(mode+'-ticket.json');output=folder/(mode+'-result.json')
        atomic_json(ticket,{'root':str(self.settings.root.resolve()),'job_id':job['job_id'],
            'lease_owner':job['lease_owner'],'binding':binding,'output':str(output),**(extra or {})})
        os.chmod(ticket,0o600)
        env={k:os.environ[k] for k in ('PATH','SYSTEMROOT','WINDIR','TEMP','TMP','TMPDIR','LANG','LC_ALL') if k in os.environ}
        env.update(YTDLP_NO_PLUGINS='1',PYTHONNOUSERSITE='1',HUB_ROOT=str(self.settings.root.resolve()))
        try:
            proc=subprocess.run([sys.executable,'-m','adapters.youtube_public_material','--'+mode,str(ticket)],
                cwd=Path(__file__).resolve().parents[1],env=env,stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=150 if mode=='extract' else 7200)
        except subprocess.TimeoutExpired:raise HubError('YOUTUBE_MATERIAL_'+mode.upper()+'_TIMEOUT',retryable=True) from None
        except OSError:raise HubError('YOUTUBE_EXTRACTOR_RUNTIME_UNAVAILABLE') from None
        if not output.is_file() or output.is_symlink() or output.stat().st_size>1_048_576:
            raise HubError('YOUTUBE_EXTRACTOR_RUNTIME_UNAVAILABLE')
        packet=json.loads(output.read_text('utf-8'))
        if proc.returncode or packet.get('error_code'):raise HubError(packet.get('error_code','YOUTUBE_EXTRACTOR_FAILED'),retryable=packet.get('retryable') is True)
        return packet['info']

    def _extract(self,job,binding,folder):return self._run_helper(job,binding,folder,'extract')

    def acquire(self,job):
        current,binding=self._binding(job)
        with resource_guard(self.settings.root/'data/locks/heavy',self.settings.root/('data/locks/youtube-material-'+job['job_id'])):
            current=self._check(job,binding)
            old=current['result'].get(STATE)
            if old and old.get('binding')!=binding:raise HubError('YOUTUBE_MATERIAL_BINDING_CHANGED')
            if old and (type(old.get('acquisition_attempt_index')) is not int or not 1<=old['acquisition_attempt_index']<=4):
                raise HubError('YOUTUBE_MATERIAL_ATTEMPT_RECORD_INVALID')
            if old and old.get('state')=='verified':
                info=old['material'];file=Path(info['file']);root=(self.settings.root/'media'/job['job_id']).resolve()
                receipt=Path(old['attempt_dir'])/'acquisition-receipt.json'
                if (any(p.is_symlink() for p in (self.settings.root/'media',self.settings.root/'media'/job['job_id'],receipt.parent,receipt,file)) or
                        not root.is_relative_to((self.settings.root/'media').resolve()) or not receipt.resolve().is_relative_to(root) or
                        not receipt.is_file() or receipt.stat().st_size>262144 or json.loads(receipt.read_text('utf-8'))!=old or
                        not file.resolve().is_relative_to(root) or not file.is_file() or
                        file.stat().st_size!=info['bytes'] or sha256_file(file)!=info['sha256']):raise HubError('YOUTUBE_ACQUIRED_FILE_CHANGED')
                return old
            if old and old.get('lease_owner')==job['lease_owner']:raise HubError('YOUTUBE_ACQUISITION_ALREADY_CONSUMED')
            if old and old['acquisition_attempt_index']>=4:raise HubError('YOUTUBE_MATERIAL_RETRY_EXHAUSTED')
            self.maintenance.check_disk(binding['max_bytes']*2+64_000_000)
            folder=self.settings.root/'media'/job['job_id']/'youtube-material-v1'/str(uuid.uuid4())
            if any(p.is_symlink() for p in (self.settings.root/'media',self.settings.root/'media'/job['job_id'],folder.parent)):
                raise HubError('YOUTUBE_MATERIAL_WORKSPACE_INVALID')
            folder.mkdir(parents=True,mode=0o700)
            state={'version':VERSION,'state':'acquiring','binding':binding,'lease_owner':job['lease_owner'],
                'attempt':current['attempts'],'attempt_dir':str(folder),'previous_attempt':old,
                'acquisition_attempt_index':old['acquisition_attempt_index']+1 if old else 1,
                'publication_authorized':False,'human_review':False,'production_source_asset_written':False}
            self.store.checkpoint(job,'youtube_material_acquiring',{**current['result'],STATE:state})
            try:
                value=self.extractor(job,binding,folder);self._check(job,binding)
                chosen=self._select(value,binding);url=chosen['url'];host=validate_url(url,media=True)
                budget=Budget(self.store,WEB_LIMITS);call=budget.reserve(PROVIDER,job['job_id'])
                try:
                    if self.fetch is None:
                        info=self._run_helper(job,binding,folder,'fetch',{'url':url,'destination':str(folder/'material.mp4'),
                            'max_bytes':binding['max_bytes'],'expected_sha256':binding['expected_sha256']})
                    else:
                        info=self.fetch(url,folder/'material.mp4',allowed_hosts=[host],max_bytes=binding['max_bytes'],
                            check_disk=lambda reserve=0:self._check(job,binding,reserve),expected_sha256=binding['expected_sha256'])
                    budget.finish(call,'done')
                except BaseException as exc:
                    budget.finish(call,'unknown' if not isinstance(exc,HubError) or exc.retryable else 'error',getattr(exc,'code','DOWNLOAD_INTERRUPTED'));raise
                self._check(job,binding)
                file=Path(info['file']);expected=folder/'material.mp4'
                if (file.is_symlink() or file.resolve()!=expected.resolve() or not file.is_file() or
                        not 0<file.stat().st_size<=binding['max_bytes'] or file.stat().st_size!=chosen['filesize'] or
                        info.get('bytes')!=file.stat().st_size or sha256_file(file)!=info.get('sha256') or
                        (binding['expected_sha256'] and info['sha256']!=binding['expected_sha256'])):
                    raise HubError('YOUTUBE_ACQUIRED_FILE_INCOMPLETE')
                details=self.inspector(file,check=lambda:self._check(job,binding))
                if (type(details['duration']) not in (int,float) or not math.isfinite(details['duration']) or
                        abs(details['duration']-binding['duration'])>2 or details['bytes']!=info['bytes'] or
                        sorted(details['stream_types'].values())!=['audio','video']):raise HubError('YOUTUBE_ACQUIRED_MEDIA_INCOMPLETE')
                proof=self.packets(file,details['stream_types'],timeout=7200,check=lambda:self._check(job,binding))
                if (set(proof)!={'audio','video'} or any(type(v.get('packets')) is not int or v['packets']<1 or
                        not re.fullmatch('[a-f0-9]{64}',v.get('chain','')) for v in proof.values())):raise HubError('YOUTUBE_ACQUIRED_MEDIA_INCOMPLETE')
                self._check(job,binding)
                receipt={**state,'state':'verified','material':{**info,'duration':details['duration']},
                    'format_id':chosen.get('format_id'),'media_url_sha256':hashlib.sha256(url.encode()).hexdigest(),
                    'packet_evidence':proof,'web_call_id':call,'transport_mode':'custom_owned_test' if self.custom_transport else 'public_tls_guarded',
                    'new_official_data_api_calls':0,'official_data_api_quota_units':0,'web_api_price_usd':0,
                    'network_transfer_invoice_cost':'unknown','expected_hash_verified':binding['expected_sha256'] is not None,
                    'creator_original_upload_identity_verified':False,'yt_dlp_required_version':YTDLP_VERSION,
                    'yt_dlp_runtime_used':not self.custom_extractor,
                    'local_media_validation_mode':'custom_owned_test' if self.custom_media_validation else 'ffprobe_packet_scan'}
                atomic_json(folder/'acquisition-receipt.json',receipt);os.chmod(folder/'acquisition-receipt.json',0o600)
                self.store.checkpoint(job,'youtube_material_verified',{**self.store.get(job['job_id'])['result'],STATE:receipt})
                return receipt
            except BaseException as exc:
                failure={**state,'state':'failed','error_code':getattr(exc,'code','YOUTUBE_MATERIAL_INTERRUPTED'),
                    'partial_files_retained':True,'publication_authorized':False}
                try:
                    atomic_json(folder/'acquisition-failure.json',failure)
                    self._check(job,binding)
                    self.store.checkpoint(job,'youtube_material_failed',{**self.store.get(job['job_id'])['result'],STATE:failure})
                except (HubError,RuntimeError):pass
                raise


def _child_extract(file):
    """Isolated dependency process; every extractor request uses public TLS pins."""
    from services.hub.config import Settings
    ticket=json.loads(Path(file).read_text('utf-8'));output=Path(ticket['output']);root=Path(ticket['root'])
    if not output.resolve().is_relative_to((root/'media'/ticket['job_id']).resolve()):raise HubError('YOUTUBE_MATERIAL_WORKSPACE_INVALID')
    try:
        import yt_dlp
        from yt_dlp.version import __version__
        from yt_dlp.networking.common import Request,Response
        if __version__!=YTDLP_VERSION:raise HubError('YOUTUBE_EXTRACTOR_VERSION_NOT_VALIDATED')
        settings=Settings.load();store=Store(settings.db_path);service=YouTubePublicMaterial(settings,store)
        job=store.get(ticket['job_id']);job['lease_owner']=ticket['lease_owner'];service._check(job,ticket['binding'])
        budget=Budget(store,WEB_LIMITS);deadline=time.monotonic()+120;calls=0
        class Logger:
            def debug(self,*a):pass
            def warning(self,message):
                low=message.lower()
                if any(x in low for x in ('po token','sign in','captcha','drm','geo-restrict','not available in your country')):
                    raise HubError('YOUTUBE_MATERIAL_ACCESS_RESTRICTED')
            def error(self,*a):pass
        class Guarded(yt_dlp.YoutubeDL):
            def urlopen(self,request):
                nonlocal calls
                request=Request(request) if isinstance(request,str) else request
                if not isinstance(request,Request) or any(request.proxies.values()) or request.extensions.get('impersonate'):
                    raise HubError('YOUTUBE_MATERIAL_REQUEST_NOT_SUPPORTED')
                if request.method not in ('GET','HEAD','POST'):raise HubError('YOUTUBE_MATERIAL_REQUEST_NOT_SUPPORTED')
                headers={k:v for k,v in request.headers.items() if k.lower()!='accept-encoding'};headers['Accept-Encoding']='identity'
                if any(k.lower() in ('cookie','authorization','proxy-authorization','host','forwarded','x-forwarded-for','x-real-ip') for k in headers):
                    raise HubError('YOUTUBE_MATERIAL_AUTHENTICATION_NOT_ALLOWED')
                body=request.data
                if body is not None and (not isinstance(body,bytes) or len(body)>1_048_576):raise HubError('YOUTUBE_MATERIAL_REQUEST_NOT_SUPPORTED')
                url=request.url
                for hop in range(4):
                    service._check(job,ticket['binding']);host=validate_url(url)
                    if calls>=20 or time.monotonic()>deadline:raise HubError('YOUTUBE_EXTRACTOR_REQUEST_BOUND')
                    calls+=1;call=budget.reserve(PROVIDER,job['job_id']);connection=None
                    try:
                        connection,response=open_request(url,[host],method=request.method,headers=headers,body=body)
                        status=response.status
                        if status in (301,302,303,307,308):
                            if request.method not in ('GET','HEAD') or hop==3:raise HubError('YOUTUBE_MATERIAL_REDIRECT_NOT_ALLOWED')
                            next_url=urljoin(url,response.getheader('Location',''));validate_url(next_url);budget.finish(call,'done');url=next_url;continue
                        if status in (401,403):raise HubError('YOUTUBE_MATERIAL_ACCESS_RESTRICTED')
                        if status==429:raise HubError('YOUTUBE_MATERIAL_RATE_LIMITED')
                        if status>=500:raise HubError('YOUTUBE_MATERIAL_NETWORK_ERROR',retryable=True)
                        if status!=200 or response.getheader('Content-Encoding','identity') not in ('','identity'):raise HubError('YOUTUBE_MATERIAL_HTTP_REJECTED')
                        raw=response.read(8_388_609)
                        if len(raw)>8_388_608:raise HubError('YOUTUBE_EXTRACTOR_RESPONSE_BOUND')
                        budget.finish(call,'done');return Response(io.BytesIO(raw),url,dict(response.getheaders()),status=status)
                    except BaseException as exc:
                        budget.finish(call,'unknown' if isinstance(exc,OSError) else 'error',getattr(exc,'code','YOUTUBE_EXTRACTOR_NETWORK_ERROR'));raise
                    finally:
                        if connection:connection.close()
                raise HubError('YOUTUBE_MATERIAL_REDIRECT_NOT_ALLOWED')
        options={'quiet':True,'no_warnings':False,'logger':Logger(),'cachedir':False,'skip_download':True,
            'noplaylist':True,'playlistend':1,'proxy':'','usenetrc':False,'cookiefile':None,'cookiesfrombrowser':None,
            'username':None,'password':None,'nocheckcertificate':False,'geo_bypass':False,'xff':'never',
            'extractor_retries':0,'retries':0,'fragment_retries':0,'socket_timeout':20,'concurrent_fragment_downloads':1,
            'allow_unplayable_formats':False,'js_runtimes':{},'remote_components':[],'postprocessors':[],
            'mark_watched':False,'impersonate':None,'writeinfojson':False,'writesubtitles':False,'writethumbnail':False,'getcomments':False,
            'allowed_extractors':['youtube'],'extractor_args':{'youtube':{'player_client':['web']}}}
        with Guarded(options) as ydl:
            info=ydl.extract_info('https://www.youtube.com/watch?v='+ticket['binding']['video_id'],download=False)
        keys=('id','channel_id','duration','is_live','availability','age_limit','has_drm','_type')
        clean={k:info[k] for k in keys if k in info}
        clean['formats']=[{k:v for k,v in item.items() if k in ('format_id','protocol','url','ext','vcodec','acodec','filesize','height','has_drm','fragments','manifest_url','http_headers')}
                          for item in info.get('formats',[])[:200]]
        atomic_json(output,{'info':clean});os.chmod(output,0o600);return 0
    except ImportError:code='YOUTUBE_EXTRACTOR_RUNTIME_UNAVAILABLE';retry=False
    except HubError as exc:code=exc.code;retry=exc.retryable
    except OSError:code='YOUTUBE_MATERIAL_NETWORK_ERROR';retry=True
    except Exception:code='YOUTUBE_PUBLIC_EXTRACTION_UNAVAILABLE';retry=False
    atomic_json(output,{'error_code':code,'retryable':retry});os.chmod(output,0o600);return 1


def _child_fetch(file):
    from adapters import assets
    from services.hub.config import Settings
    ticket=json.loads(Path(file).read_text('utf-8'));output=Path(ticket['output'])
    root=Path(ticket['root']);folder=Path(file).resolve().parent
    if (not folder.is_relative_to((root/'media'/ticket['job_id']).resolve()) or output.resolve().parent!=folder or
            Path(ticket['destination']).resolve()!=folder/'material.mp4'):raise HubError('YOUTUBE_MATERIAL_WORKSPACE_INVALID')
    try:
        settings=Settings.load();store=Store(settings.db_path);service=YouTubePublicMaterial(settings,store)
        job=store.get(ticket['job_id']);job['lease_owner']=ticket['lease_owner'];binding=ticket['binding']
        service._check(job,binding)
        if ticket['max_bytes']!=binding['max_bytes'] or ticket['expected_sha256']!=binding['expected_sha256']:
            raise HubError('YOUTUBE_MATERIAL_BINDING_CHANGED')
        host=validate_url(ticket['url'],media=True)
        original=assets.open_request
        try:
            assets.open_request=open_media_request
            info=download(ticket['url'],ticket['destination'],allowed_hosts=[host],max_bytes=binding['max_bytes'],
                check_disk=lambda reserve=0:service._check(job,binding,reserve),expected_sha256=binding['expected_sha256'])
        finally:assets.open_request=original
        atomic_json(output,{'info':info});os.chmod(output,0o600);return 0
    except HubError as exc:code=exc.code;retry=exc.retryable
    except OSError:code='YOUTUBE_MATERIAL_NETWORK_ERROR';retry=True
    except Exception:code='YOUTUBE_MATERIAL_DOWNLOAD_FAILED';retry=False
    atomic_json(output,{'error_code':code,'retryable':retry});os.chmod(output,0o600);return 1


if __name__=='__main__':
    if len(sys.argv)!=3 or sys.argv[1] not in ('--extract','--fetch'):raise SystemExit('YOUTUBE_MATERIAL_INTERNAL_HELPER_ONLY')
    raise SystemExit((_child_extract if sys.argv[1]=='--extract' else _child_fetch)(sys.argv[2]))
