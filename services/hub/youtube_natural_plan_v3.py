"""Pure next-phase per-video planning. No admission, network, or publication.

The frozen v2 pilot remains separate. A future natural producer/child must
bind this plan to fresh official observations and an actual activation record;
this module alone never grants either capability.
"""
from datetime import datetime,timezone
import hashlib,math,re,time

from .db import encode
from .errors import HubError
from .youtube_download import CHANNELS,duration_seconds
from .youtube_highest_plan_v2 import (OPERATING_NOT_BEFORE,MAX_BYTES,MIN_RANGE_BYTES,
    OUTPUT_ALLOWANCE_BYTES,WORKSPACE_OVERHEAD_BYTES,require_operating_date)

VERSION='youtube-highest-natural-source-plan-v3'
QUALITY='highest-height-fps-h264-aac-stream-copy-v1'
QUALITY_BASIS='maximum_available_height_then_fps;highest_reported_h264_bitrate_at_that_tier;highest_reported_aac_bitrate'
IDENT=re.compile('[A-Za-z0-9_-]{1,64}')


def fail(code):raise HubError(code,review=True)


def stamp(value):
    try:
        if not isinstance(value,str):raise ValueError()
        item=datetime.fromisoformat(value.replace('Z','+00:00'))
        if item.tzinfo is None:raise ValueError()
        return item.astimezone(timezone.utc)
    except (ValueError,TypeError,OverflowError):fail('YOUTUBE_NATURAL_OFFICIAL_DATE_REQUIRED')


def positive(value):return type(value) in (int,float) and math.isfinite(value) and value>0


def family(value,kind):
    if not isinstance(value,str):return None
    if value=='none':return 'none'
    if kind=='video' and value.startswith(('avc1','h264')):return 'h264'
    if kind=='audio' and value.startswith(('mp4a','aac')):return 'aac'
    label=value.split('.',1)[0]
    return label if re.fullmatch('[A-Za-z0-9_-]{1,32}',label) else None


def _standard_aac_tie(matches,formats):
    """Only resolve one standard/DRC pair; all other equal-rate ties stop."""
    if len(matches)!=2:return matches
    standard=[f for f in matches if re.fullmatch('[0-9]+',f['format_id'])]
    if len(standard)!=1:return matches
    plain=standard[0];drc=next(f for f in matches if f is not plain)
    if drc['format_id']!=plain['format_id']+'-drc':return matches
    same=('ext','vcodec','acodec','width','height','fps','bitrate_kbps','protocol','fragmented','has_drm','dynamic_range')
    if (plain['ext']!='m4a' or plain['protocol']!='https' or plain['fragmented'] or plain['has_drm'] or
            any(plain[k]!=drc[k] for k in same)):return matches
    # Compare the supplied codec strings too, not just the normalized family.
    originals={f['format_id']:f for f in formats}
    if originals[plain['format_id']].get('acodec')!=originals[drc['format_id']].get('acodec'):return matches
    return [plain]


def natural_source_plan(raw,info,*,activated_at,observed_at,now=None):
    return _source_plan(raw,info,activated_at=activated_at,observed_at=observed_at,now=now,bounds=None)


def _source_plan(raw,info,*,activated_at,observed_at,now=None,bounds=None):
    """Validate genuine inputs supplied by a future trusted acquisition path.

    No fallback to a lower geometric/fps tier is allowed. Codec alternatives
    at the same tier are explicit; output remains H264/AAC without reencoding.
    Exact sizes and reported bitrates are required before any media dispatch.
    """
    now=time.time() if now is None else now
    maximum_seconds,maximum_bytes,maximum_requests,version=36000,MAX_BYTES,2048,VERSION
    if bounds is not None:
        if (not isinstance(bounds,dict) or set(bounds)!={'max_seconds','max_bytes','max_requests','version'} or
                bounds['version']!='youtube-whole-replay-source-basis-v4' or any(type(bounds[k]) is not int or
                not 1<=bounds[k]<=limit for k,limit in (('max_seconds',36000*32),('max_bytes',14_000_000_000*32),('max_requests',20000)))):
            fail('YOUTUBE_NATURAL_SOURCE_PLAN_INVALID')
        maximum_seconds,maximum_bytes,maximum_requests,version=(bounds[k] for k in ('max_seconds','max_bytes','max_requests','version'))
    try:
        activation=stamp(activated_at);observed=stamp(observed_at)
        if not math.isfinite(now) or activation.timestamp()>now or not 0<=now-observed.timestamp()<=300:
            fail('YOUTUBE_NATURAL_FRESH_OBSERVATION_REQUIRED')
        if not isinstance(raw,dict) or not isinstance(info,dict):raise ValueError()
        ident=raw['id'];channel=raw['snippet']['channelId'];live=raw['liveStreamingDetails']
        start=require_operating_date(live.get('actualStartTime'));ended=stamp(live.get('actualEndTime'))
        if (not isinstance(ident,str) or not re.fullmatch('[A-Za-z0-9_-]{11}',ident) or channel not in CHANNELS or
                raw.get('status',{}).get('privacyStatus')!='public' or ended<stamp(start) or
                ended.timestamp()>observed.timestamp()+30):
            fail('YOUTUBE_NATURAL_OFFICIAL_SOURCE_REQUIRED')
        if ended<activation:fail('YOUTUBE_NATURAL_HISTORICAL_SOURCE_REJECTED')
        duration=(duration_seconds(raw['contentDetails']['duration']) if bounds is None else
            duration_seconds(raw['contentDetails']['duration'],maximum=maximum_seconds))
        if duration<=0 or duration>maximum_seconds:fail('YOUTUBE_NATURAL_DURATION_UNSUPPORTED')
        if (info.get('id')!=ident or info.get('channel_id')!=channel or info.get('entries') is not None or
                info.get('_type','video')!='video' or info.get('is_live') is True or
                info.get('live_status') in ('is_live','is_upcoming') or info.get('age_limit',0) or
                info.get('availability') not in (None,'public') or info.get('has_drm') is True or
                any(isinstance(f,dict) and f.get('has_drm') for f in info.get('formats',[]) if isinstance(info.get('formats'),list))):
            fail('YOUTUBE_NATURAL_SOURCE_CHANGED')
        if info.get('live_status')=='post_live':fail('YOUTUBE_NATURAL_POST_LIVE_NOT_READY')
        if not positive(info.get('duration')) or abs(info['duration']-duration)>2:fail('YOUTUBE_NATURAL_SOURCE_CHANGED')
        formats=info.get('formats')
        if not isinstance(formats,list) or not 1<=len(formats)<=300:raise ValueError()
        inventory=[];ids=set()
        for source in formats:
            if not isinstance(source,dict):raise ValueError()
            name=source.get('format_id')
            if not isinstance(name,str) or not IDENT.fullmatch(name) or name in ids:raise ValueError()
            ids.add(name);video=family(source.get('vcodec'),'video');audio=family(source.get('acodec'),'audio')
            if video is None or audio is None:raise ValueError()
            if any(v is not None and (not isinstance(v,str) or not re.fullmatch('[A-Za-z0-9_+.-]{1,32}',v))
                    for v in (source.get('ext'),source.get('protocol'),source.get('dynamic_range'))):raise ValueError()
            if video!='none' and (type(source.get('height')) is not int or source['height']<=0 or
                    type(source.get('width')) is not int or source['width']<=0 or not positive(source.get('fps'))):
                fail('YOUTUBE_NATURAL_HIGHEST_TIER_UNKNOWN')
            bitrate=source.get('tbr') if video!='none' else source.get('abr')
            # yt-dlp's sbN/mhtml entries are storyboard images, not an audio
            # option. Normalize their absent-audio abr=0 while retaining them
            # in the bound inventory; they cannot enter either media shortlist.
            if (video==audio=='none' and re.fullmatch('sb[0-9]+',name) and
                    source.get('ext')==source.get('protocol')=='mhtml' and
                    type(bitrate) in (int,float) and bitrate==0):bitrate=None
            inventory.append({'format_id':name,'ext':source.get('ext'),'vcodec':video,'acodec':audio,
                'width':source.get('width') if video!='none' else None,'height':source.get('height') if video!='none' else None,
                'fps':float(source['fps']) if video!='none' else None,
                'filesize':source.get('filesize') if type(source.get('filesize')) is int else None,
                'bitrate_kbps':bitrate,
                'protocol':source.get('protocol'),'fragmented':bool(source.get('fragments') or source.get('manifest_url')),
                'has_drm':bool(source.get('has_drm')),'dynamic_range':source.get('dynamic_range')})
            if inventory[-1]['bitrate_kbps'] is not None and not positive(inventory[-1]['bitrate_kbps']):raise ValueError()
        videos=[f for f in inventory if f['vcodec']!='none']
        if not videos:fail('YOUTUBE_NATURAL_HIGHEST_TIER_UNKNOWN')
        tier=max((f['height'],f['fps']) for f in videos)
        top=[f for f in videos if (f['height'],f['fps'])==tier]
        if (tier[0]>1080 or tier[1]>60.1 or any(f['width']>1920 or
                f['dynamic_range'] not in (None,'SDR') for f in top)):
            fail('YOUTUBE_NATURAL_HIGHEST_TIER_UNSUPPORTED')
        vc=[f for f in top if f['vcodec']=='h264' and f['acodec']=='none']
        ac=[f for f in inventory if f['vcodec']=='none' and f['acodec']=='aac']
        if not vc or not ac:fail('YOUTUBE_NATURAL_HIGHEST_CODEC_UNSUPPORTED')
        if any(not positive(f['bitrate_kbps']) for f in vc+ac):fail('YOUTUBE_NATURAL_QUALITY_ORDER_UNKNOWN')
        # Only an otherwise identical standard/DRC AAC pair has a documented
        # preference for the standard track. Other equal-rate choices stop.
        selected=[]
        for candidates,ext in ((vc,'mp4'),(ac,'m4a')):
            best=max(f['bitrate_kbps'] for f in candidates);matches=[f for f in candidates if f['bitrate_kbps']==best]
            if ext=='m4a':matches=_standard_aac_tie(matches,formats)
            if len(matches)!=1:fail('YOUTUBE_NATURAL_QUALITY_ORDER_AMBIGUOUS')
            candidate=matches[0]
            if (candidate['protocol']!='https' or candidate['fragmented'] or candidate['has_drm'] or candidate['ext']!=ext):
                fail('YOUTUBE_NATURAL_EXACT_TRACK_REQUIRED')
            if candidate['filesize'] is None:fail('YOUTUBE_NATURAL_EXACT_SIZE_NOT_READY')
            if type(candidate['filesize']) is not int or candidate['filesize']<=0:fail('YOUTUBE_NATURAL_EXACT_TRACK_REQUIRED')
            selected.append({k:candidate[k] for k in ('format_id','ext','vcodec','acodec','width','height','fps','filesize')})
        size=sum(f['filesize'] for f in selected);output=size+OUTPUT_ALLOWANCE_BYTES
        media=sum(math.ceil(f['filesize']/MIN_RANGE_BYTES) for f in selected)
        # Whole-replay requests can be redirected through an HTML hop before
        # the media body. The observed 2026-11-04 long replay exceeded the old
        # 25% resume allowance after 461 redirects. A bounded whole-replay plan
        # therefore reserves one possible redirect/resume request per media
        # range; the legacy 2,048-request pilot keeps its original formula.
        resume=media if bounds is not None else max(64,math.ceil(media/4))
        requests=media+64+resume
        if size>maximum_bytes or output>maximum_bytes or requests>maximum_requests:fail('YOUTUBE_NATURAL_SOURCE_PLAN_EXCEEDS_LIMIT')
        return {'version':version,'quality_profile':QUALITY,'quality_basis':QUALITY_BASIS,'video_id':ident,'channel_id':channel,
            'duration':duration,'activated_at':activation.isoformat().replace('+00:00','Z'),
            'observed_at':observed.isoformat().replace('+00:00','Z'),'operating_not_before':OPERATING_NOT_BEFORE,
            'source_actual_start_time':start,'source_actual_end_time':ended.isoformat().replace('+00:00','Z'),
            'official_metadata_sha256':hashlib.sha256(encode(raw).encode()).hexdigest(),'source_inventory':inventory,
            'inventory_sha256':hashlib.sha256(encode(inventory).encode()).hexdigest(),'selected_formats':selected,
            'highest_tier':{'height':tier[0],'fps':tier[1]},'source_bytes':size,'planned_output_max_bytes':output,
            'workspace_overhead_bytes':WORKSPACE_OVERHEAD_BYTES,'required_working_bytes':size+output+WORKSPACE_OVERHEAD_BYTES,
            'minimum_range_bytes':MIN_RANGE_BYTES,'media_requests_plan':media,'metadata_request_allowance':64,
            'redirect_resume_request_allowance':resume,'max_http_requests':requests,
            'human_review':False,'publication_authorized':False,'plan_grants_admission':False}
    except HubError:raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
        fail('YOUTUBE_NATURAL_SOURCE_PLAN_INVALID')


def validate_natural_plan(value,*,official_raw=None):
    return _validate_source_plan(value,official_raw=official_raw,bounds=None)


def _validate_source_plan(value,*,official_raw=None,bounds=None):
    """Rebuild every derived bound, order and selection; no passed flags."""
    try:
        if not isinstance(value,dict) or len(encode(value).encode())>262144:raise ValueError()
        raw=official_raw or {'id':value['video_id'],'snippet':{'channelId':value['channel_id']},
            'status':{'privacyStatus':'public'},'contentDetails':{'duration':'PT'+str(value['duration'])+'S'},
            'liveStreamingDetails':{'actualStartTime':value['source_actual_start_time'],'actualEndTime':value['source_actual_end_time']}}
        observed=stamp(value['observed_at']);formats=[]
        for record in value['source_inventory']:
            source={k:record[k] for k in ('format_id','ext','vcodec','acodec','width','height','fps','filesize','protocol','has_drm','dynamic_range')}
            source['tbr' if record['vcodec']!='none' else 'abr']=record['bitrate_kbps']
            if record['fragmented']:source['fragments']=[{}]
            formats.append(source)
        info={'id':value['video_id'],'channel_id':value['channel_id'],'duration':value['duration'],'formats':formats}
        expected=_source_plan(raw,info,activated_at=value['activated_at'],observed_at=value['observed_at'],now=observed.timestamp(),bounds=bounds)
        if official_raw is None:
            if not isinstance(value['official_metadata_sha256'],str) or not re.fullmatch('[a-f0-9]{64}',value['official_metadata_sha256']):raise ValueError()
            expected['official_metadata_sha256']=value['official_metadata_sha256']
        if value!=expected:raise ValueError()
        return value
    except HubError:raise
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):fail('YOUTUBE_NATURAL_SOURCE_PLAN_INVALID')
