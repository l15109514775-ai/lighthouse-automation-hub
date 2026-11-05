"""Read real native observations before preserving them in durable state.

Local streaming hash + causal filename/upload completion + known public BV
and playable full-duration HTMLVideoElement are evidence, never B storage hash.
"""
from datetime import datetime,timezone
import hashlib,json,math,os
from pathlib import Path
from .errors import HubError
from .youtube_download import digest


def fail():raise HubError('REPLAY_HIGHEST_COVERAGE_EVIDENCE_REQUIRED',review=True)
def finite(v):return type(v) in (int,float) and math.isfinite(v)
def recent(value,seconds):
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    return parsed.tzinfo is not None and 0<=datetime.now(timezone.utc).timestamp()-parsed.timestamp()<=seconds


def read_evidence(settings,job,state,reference,name,*,directory='replay-browser-highest-v2'):
    try:
        if directory not in ('replay-browser-highest-v2','replay-browser-natural-v3'):fail()
        file=settings.root/'media'/job['job_id']/directory/state['attempt_id']/name
        if (not isinstance(reference,dict) or set(reference)!={'file','bytes','sha256'} or reference['file']!=str(file) or
                type(reference['bytes']) is not int or not 0<reference['bytes']<=65536 or file.resolve()!=file or
                any(p.is_symlink() for p in (file,*file.parents))):fail()
        with file.open('rb') as stream:
            before=os.fstat(stream.fileno());raw=stream.read(65537);after=os.fstat(stream.fileno())
        if (not len(raw)==reference['bytes']==before.st_size or
                any(getattr(before,k)!=getattr(after,k) for k in ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')) or
                hashlib.sha256(raw).hexdigest()!=reference['sha256']):fail()
        return json.loads(raw)
    except HubError:raise
    except (OSError,ValueError,TypeError,KeyError,AttributeError):fail()


def expected_binding(job,binding,state):
    return {'job_id':job['job_id'],'attempt_id':state['attempt_id'],'site_uid':binding['site_uid'],
        'source_id':binding['source_id'],'source_url':binding['source_url'],'artifact_sha256':binding['artifact_sha256'],
        'file_bytes':binding['file_bytes'],'metadata_sha256':binding['metadata_sha256'],'binding_sha256':digest(binding)}


def upload_coverage(settings,job,binding,state,reference,*,fresh=False,directory='replay-browser-highest-v2'):
    try:
        observation=read_evidence(settings,job,state,reference,'upload-coverage.json',directory=directory);expected=expected_binding(job,binding,state)
        expected.update(version='replay-highest-native-upload-coverage-v2',file=binding['file'],selected_filename=Path(binding['file']).name,
            native_upload_complete=True,whole_file_streamed=True,streamed_bytes=binding['file_bytes'],streamed_sha256=binding['artifact_sha256'],
            server_file_hash_verified=False,human_review=False)
        if (set(observation)!=set(expected)|{'native_filename','observed_at'} or
                any(observation[k]!=v for k,v in expected.items()) or
                any(observation[k] is not v for k,v in expected.items() if type(v) is bool)):fail()
        native=observation['native_filename'];filename=Path(binding['file']).name
        allowed=(filename,Path(filename).stem) if native.get('kind')=='native_title_input_after_upload' else (filename,)
        if (set(native)!={'text','kind','observed'} or native['kind'] not in ('file_input','native_title_input_after_upload','visible_filename') or
                native['observed'] is not True or native['text'] not in allowed or fresh and not recent(observation['observed_at'],300)):fail()
        return {**observation,'evidence':reference}
    except HubError:raise
    except (OSError,KeyError,ValueError,TypeError,AttributeError,OverflowError):fail()


def public_coverage(settings,store,job,binding,state,reference,observation,*,directory='replay-browser-highest-v2',download_state='youtube_download_v2'):
    try:
        if (directory,download_state) not in (('replay-browser-highest-v2','youtube_download_v2'),('replay-browser-natural-v3','youtube_download_v3')):fail()
        value=read_evidence(settings,job,state,reference,'public-coverage.json',directory=directory);expected=expected_binding(job,binding,state)
        expected.update(version='replay-highest-public-coverage-v2',external_id=observation['external_id'],url=observation['url'],
            server_file_hash_verified=False,human_review=False,source_to_merged_packet_coverage_verified=True)
        if (set(value)!=set(expected)|{'source_duration_seconds','upload_coverage_ref','public_player'} or
                any(value[k]!=v for k,v in expected.items()) or
                any(value[k] is not v for k,v in expected.items() if type(v) is bool)):fail()
        recorded=state['upload_coverage'];uploaded=upload_coverage(settings,job,binding,state,value['upload_coverage_ref'],directory=directory)
        if recorded!=uploaded:fail()
        source=store.get(binding['download_job_id']);download=source['result'][download_state]
        duration=download['binding']['duration'];player=value['public_player'];ranges=player['seekable_ranges']
        if (not finite(value['source_duration_seconds']) or value['source_duration_seconds']!=duration or download['receipt']['source_to_merged_packet_coverage_verified'] is not True or
                set(player)!={'duration_seconds','current_time','ready_state','seekable_ranges','observed_at','error_code','near_end_playability_observed','near_end_seek_performed'} or
                not finite(player['duration_seconds']) or abs(player['duration_seconds']-duration)>2 or
                not finite(player['current_time']) or not 0<=player['current_time']<=player['duration_seconds']+1 or
                type(player['ready_state']) is not int or not 2<=player['ready_state']<=4 or player['error_code'] is not None or
                not recent(player['observed_at'],300) or not isinstance(ranges,list) or not 1<=len(ranges)<=100 or
                any(set(r)!={'start','end'} or not finite(r['start']) or not finite(r['end']) or
                    not 0<=r['start']<r['end']<=player['duration_seconds']+1 for r in ranges) or
                type(player['near_end_playability_observed']) is not bool or player['near_end_seek_performed'] is not False or
                player['near_end_playability_observed']!=(player['current_time']>0 and player['current_time']>=max(0,player['duration_seconds']-5))):fail()
        return {**value,'evidence':reference}
    except HubError:raise
    except (OSError,KeyError,ValueError,TypeError,AttributeError,OverflowError):fail()
