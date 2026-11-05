"""Finite, exact-track source planning for the single highest-quality pilot."""
import math
from datetime import datetime,timezone

from .errors import HubError

VIDEO_ID='9T5tn1WGlJg'
QUALITY='h264-1080p60-itag299-aac140-v1'
VERSION='youtube-highest-source-plan-v2'
MAX_BYTES=14_000_000_000
MIN_RANGE_BYTES=int((10<<20)*0.95)
OUTPUT_ALLOWANCE_BYTES=64_000_000
WORKSPACE_OVERHEAD_BYTES=32_000_000
OPERATING_NOT_BEFORE='2026-10-31T16:00:00Z'


def require_operating_date(value):
    try:
        if not isinstance(value,str):raise ValueError()
        actual=datetime.fromisoformat(value.replace('Z','+00:00'))
        cutoff=datetime.fromisoformat(OPERATING_NOT_BEFORE.replace('Z','+00:00'))
        if actual.tzinfo is None:raise ValueError()
        if actual<cutoff:raise HubError('YOUTUBE_HIGHEST_BEFORE_OPERATING_START',review=True)
        if actual.timestamp()>datetime.now(timezone.utc).timestamp()+30:raise ValueError()
        return actual.astimezone(timezone.utc).isoformat().replace('+00:00','Z')
    except HubError:raise
    except (ValueError,TypeError,OverflowError):raise HubError('YOUTUBE_HIGHEST_ACTUAL_START_REQUIRED',review=True) from None


def source_plan(video_bytes,audio_bytes,*,max_source_bytes,max_output_bytes,request_cap,actual_start_time):
    started=require_operating_date(actual_start_time)
    if (any(type(v) is not int or v<=0 for v in (video_bytes,audio_bytes,max_source_bytes,max_output_bytes,request_cap)) or
            max_source_bytes>MAX_BYTES or max_output_bytes>MAX_BYTES or not 1<=request_cap<=2048):
        raise HubError('YOUTUBE_HIGHEST_SOURCE_PLAN_INVALID',review=True)
    total=video_bytes+audio_bytes;output=total+OUTPUT_ALLOWANCE_BYTES
    media=math.ceil(video_bytes/MIN_RANGE_BYTES)+math.ceil(audio_bytes/MIN_RANGE_BYTES)
    overhead=max(64,math.ceil(media/4));requests=media+64+overhead
    if total>max_source_bytes or output>max_output_bytes or requests>request_cap:
        raise HubError('YOUTUBE_HIGHEST_SOURCE_PLAN_EXCEEDS_LIMIT',review=True)
    selected=[{'format_id':'299','ext':'mp4','vcodec':'h264','acodec':'none','width':1920,'height':1080,
               'fps':60.0,'filesize':video_bytes},
              {'format_id':'140','ext':'m4a','vcodec':'none','acodec':'aac','width':None,'height':None,
               'fps':None,'filesize':audio_bytes}]
    return {'version':VERSION,'quality_profile':QUALITY,'selected_formats':selected,'source_bytes':total,
        'operating_not_before':OPERATING_NOT_BEFORE,'source_actual_start_time':started,
        'planned_output_max_bytes':output,'workspace_overhead_bytes':WORKSPACE_OVERHEAD_BYTES,
        'required_working_bytes':total+output+WORKSPACE_OVERHEAD_BYTES,'minimum_range_bytes':MIN_RANGE_BYTES,
        'media_requests_plan':media,'metadata_request_allowance':64,'redirect_resume_request_allowance':overhead,
        'max_http_requests':requests}


def validate_source_plan(value):
    try:
        tracks=value['selected_formats']
        if not isinstance(tracks,list) or len(tracks)!=2:raise ValueError()
        expected=source_plan(tracks[0]['filesize'],tracks[1]['filesize'],max_source_bytes=MAX_BYTES,
            max_output_bytes=MAX_BYTES,request_cap=2048,actual_start_time=value['source_actual_start_time'])
        if value!=expected:raise ValueError()
        return value
    except HubError:raise
    except (KeyError,ValueError,TypeError,AttributeError):raise HubError('YOUTUBE_HIGHEST_SOURCE_PLAN_INVALID',review=True) from None
