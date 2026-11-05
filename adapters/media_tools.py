"""Bounded local FFmpeg/ffprobe operations; no network protocols or shell execution."""
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time
from services.hub.errors import HubError


def process_lines(command,folder,timeout,check=lambda:None):
    messages=queue.Queue(maxsize=128)
    stop=threading.Event()
    with tempfile.TemporaryFile(dir=folder) as diagnostic:
        try:
            proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=diagnostic)
        except OSError:
            raise HubError('MEDIA_TOOL_RUNTIME_UNAVAILABLE',retryable=True) from None
        def read():
            try:
                while not stop.is_set():
                    line=proc.stdout.readline(4097)
                    while not stop.is_set():
                        try: messages.put(line,timeout=.2);break
                        except queue.Full: pass
                    if not line: return
            except (OSError,ValueError):
                while not stop.is_set():
                    try: messages.put(None,timeout=.2);break
                    except queue.Full: pass
            finally:
                proc.stdout.close()
        reader=threading.Thread(target=read,daemon=True);reader.start()
        deadline=time.monotonic()+timeout
        next_check=0
        try:
            while True:
                now=time.monotonic()
                if now>deadline: raise HubError('MEDIA_TOOL_TIMEOUT',retryable=True)
                if now>=next_check:
                    check()
                    if os.fstat(diagnostic.fileno()).st_size>1_048_576: raise HubError('MEDIA_TOOL_DIAGNOSTIC_LIMIT',review=True)
                    next_check=now+.25
                try: line=messages.get(timeout=.2)
                except queue.Empty: continue
                if line is None: raise HubError('MEDIA_TOOL_PIPE_FAILED',retryable=True)
                if not line: break
                if len(line)>4096: raise HubError('MEDIA_TOOL_OUTPUT_LIMIT',review=True)
                yield line
            try: status=proc.wait(timeout=max(.1,min(5,deadline-time.monotonic())))
            except subprocess.TimeoutExpired: raise HubError('MEDIA_TOOL_TIMEOUT',retryable=True) from None
            if status: raise HubError('MEDIA_TOOL_FAILED',review=True)
            check()
        finally:
            stop.set()
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired: proc.kill();proc.wait(timeout=5)
            reader.join(timeout=1)


def inspect_media(file,*,check=lambda:None,include_frame_rates=False):
    file=Path(file)
    if type(include_frame_rates) is not bool:
        raise HubError('MEDIA_PROBE_OPTIONS_INVALID',review=True)
    fields='index,codec_type,codec_name,extradata_hash,width,height,sample_rate,channels'
    if include_frame_rates:
        fields+=',avg_frame_rate,r_frame_rate'
    command=['ffprobe','-v','error','-protocol_whitelist','file,pipe','-show_data_hash','sha256','-show_entries',
        'stream='+fields+':format=duration,size,start_time,format_name',
        '-of','json',str(file)]
    data=bytearray()
    for line in process_lines(command,file.parent,30,check):
        data.extend(line)
        if len(data)>65536: raise HubError('MEDIA_TOOL_OUTPUT_LIMIT',review=True)
    try:
        value=json.loads(data)
        duration=float(value['format']['duration'])
        streams=value['streams']
        kinds=[stream['codec_type'] for stream in streams]
        if not math.isfinite(duration) or duration<=0 or int(value['format']['size'])!=file.stat().st_size:
            raise ValueError()
        # Never silently drop additional languages, video tracks or embedded subtitles.
        if sorted(kinds)!=['audio','video']:
            raise HubError('MEDIA_STREAM_SELECTION_REQUIRES_REVIEW',review=True)
        mapping={int(stream['index']):stream['codec_type'] for stream in streams}
        return {'duration':duration,'bytes':file.stat().st_size,'stream_types':mapping,
                'stream_config':{stream['codec_type']:{key:val for key,val in stream.items()
                    if key not in ('index','codec_type')} for stream in streams},'format':value['format']['format_name']}
    except (ValueError,TypeError,KeyError):
        raise HubError('MEDIA_PROBE_RESULT_INVALID',review=True) from None


def empty_evidence():
    return {kind:{'packets':0,'chain':'0'*64} for kind in ('video','audio')}


def packet_evidence(file,streams,*,initial=None,timeout=7200,check=lambda:None,max_packets=10_000_000):
    """Order-sensitive SHA256 chain of each packet payload hash, bounded memory."""
    file=Path(file)
    if type(max_packets) is not int or not 1<=max_packets<=64_000_000:
        raise HubError('MEDIA_PACKET_LIMIT_INVALID',review=True)
    states={kind:dict(value) for kind,value in (initial or empty_evidence()).items()}
    mapping={int(index):kind for index,kind in streams.items()}
    pending={}
    packet_id=None
    command=['ffprobe','-v','error','-protocol_whitelist','file,pipe','-show_packets','-show_data_hash','sha256',
        '-show_entries','packet=stream_index,data_hash','-of','flat',str(file)]
    for line in process_lines(command,file.parent,timeout,check):
        match=re.fullmatch(rb'packets\.packet\.([0-9]+)\.(stream_index|data_hash)=(.+)\s*',line.strip())
        if not match: continue
        ident=int(match[1])
        if packet_id is not None and ident!=packet_id:
            if pending: raise HubError('MEDIA_PACKET_EVIDENCE_INVALID',review=True)
        packet_id=ident
        field=match[2].decode()
        if field in pending: raise HubError('MEDIA_PACKET_EVIDENCE_INVALID',review=True)
        pending[field]=match[3].strip()
        if set(pending)=={'stream_index','data_hash'}:
            try:
                kind=mapping[int(pending['stream_index'])]
                digest=re.fullmatch(rb'"SHA256:([0-9a-fA-F]{64})"',pending['data_hash'])[1]
                state=states[kind]
                state['chain']=hashlib.sha256(bytes.fromhex(state['chain'])+bytes.fromhex(digest.decode())).hexdigest()
                state['packets']+=1
                if sum(v['packets'] for v in states.values())>max_packets: raise ValueError()
            except (ValueError,KeyError,TypeError):
                raise HubError('MEDIA_PACKET_EVIDENCE_INVALID',review=True) from None
            pending={}
    if pending or any(value['packets']<1 for value in states.values()):
        raise HubError('MEDIA_PACKET_EVIDENCE_INVALID',review=True)
    return states


def split_media(source,folder,seconds,*,timeout,check):
    command=['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-i',str(source),
        '-map','0:v:0','-map','0:a:0','-c','copy','-threads','1','-avoid_negative_ts','make_zero',
        '-f','segment','-segment_time',str(seconds),'-reset_timestamps','1','-segment_format','mp4',
        '-segment_format_options','movflags=+faststart','-segment_list_type','csv',
        '-segment_list',str(folder/'segments.csv'),str(folder/'part-%03d.partial.mp4')]
    for _ in process_lines(command,folder,timeout,check): pass
