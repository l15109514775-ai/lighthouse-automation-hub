"""API-only three-way ASR for 20-minute subtitle jobs.

Primary   whisper-1 (verbose_json)  -> sentence segments with start/end (timeline source)
Secondary qwen3-asr-flash           -> 60-second windows, cheap always-on second opinion
Arbiter   gpt-4o-transcribe         -> 60-second windows, only when primary and secondary disagree

No local model is loaded here: the module only shells out to ffmpeg/ffprobe and
calls the configured HTTPS endpoints. Keys are read from environment variables
(or ``*_FILE`` indirection) and never logged.
"""
import hashlib
import json
import math
import os
import re
import ssl
import subprocess
import unicodedata
import urllib.error
import urllib.request
import uuid
from pathlib import Path

VERSION = 'asr-three-way-v1'
MAX_AUDIO_BYTES = 25 * 1024 * 1024
MAX_QWEN_INLINE = 10_000_000
DEFAULT_WINDOW_SECONDS = 60


class AsrError(RuntimeError):
    """Fixed diagnostic codes only; response bodies are never echoed."""


def _require_https(url):
    from urllib.parse import urlsplit
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.query or parsed.fragment:
        raise AsrError('ASR_ENDPOINT_INVALID')
    return url


def resolve_key(cfg, env_name, provided=None):
    if provided:
        key = provided
    else:
        key = os.environ.get(env_name, '')
        if not key:
            path = os.environ.get(env_name + '_FILE', '')
            if path:
                key = Path(path).read_text('utf-8').strip()
    if not key or any(ch.isspace() for ch in key):
        raise AsrError('ASR_CREDENTIAL_MISSING:' + env_name)
    return key


def preflight(cfg):
    """Fail fast on missing endpoints or credentials without printing secrets."""
    _require_https(cfg.get('asr_primary_base_url', 'https://api.openai.com/v1'))
    _require_https(cfg.get('asr_qwen_base_url', 'https://dashscope.aliyuncs.com/compatible-mode/v1'))
    _require_https(cfg.get('asr_arbiter_base_url', cfg.get('asr_primary_base_url', 'https://api.openai.com/v1')))
    resolve_key(cfg, cfg.get('asr_primary_key_env', 'OPENAI_API_KEY'))
    resolve_key(cfg, cfg.get('asr_qwen_key_env', 'DASHSCOPE_API_KEY'))
    if int(cfg.get('asr_max_arbiter_requests', 10)) > 0:
        resolve_key(cfg, cfg.get('asr_arbiter_key_env', 'OPENAI_API_KEY'))
    return True


def fingerprint(key):
    return hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]


def normalize(text):
    text = unicodedata.normalize('NFKC', text or '').lower()
    return ''.join(ch for ch in text if ch.isalnum())


def cer(reference, hypothesis):
    """Character error rate of hypothesis against reference (0..1)."""
    a, b = normalize(reference), normalize(hypothesis)
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1] / max(len(a), len(b))


def window_text(segments, start, end):
    parts = [row['text'] for row in segments if start <= (row['start'] + row['end']) / 2 < end]
    return ''.join(parts)


def decide_window(primary, secondary, arbiter=None, *, agree=0.20, conflict=0.40):
    result = {'status': 'agree', 'cer_primary_secondary': cer(primary, secondary),
              'cer_primary_arbiter': None, 'cer_secondary_arbiter': None, 'arbiter_used': False}
    if result['cer_primary_secondary'] <= agree:
        return result
    result['status'] = 'conflict' if result['cer_primary_secondary'] > conflict else 'minor'
    if arbiter is None:
        return result
    result['arbiter_used'] = True
    result['cer_primary_arbiter'] = cer(primary, arbiter)
    result['cer_secondary_arbiter'] = cer(secondary, arbiter)
    if result['cer_primary_arbiter'] <= agree:
        result['status'] = 'resolved_primary'
    elif result['cer_secondary_arbiter'] <= agree:
        result['status'] = 'resolved_secondary'
    else:
        result['status'] = 'conflict'
    return result


def cross_check(segments, duration, cfg, qwen_window, arbiter_window=None):
    """Compare the primary segments with secondary windows, arbitrating conflicts.

    ``qwen_window(start, end)`` and ``arbiter_window(start, end)`` return text.
    Arbiter calls are capped by ``asr_max_arbiter_requests``.
    """
    window = int(cfg.get('asr_window_seconds', DEFAULT_WINDOW_SECONDS))
    if not 30 <= window <= 300:
        raise AsrError('ASR_WINDOW_INVALID')
    agree = float(cfg.get('asr_agree_cer', 0.20))
    conflict = float(cfg.get('asr_conflict_cer', 0.40))
    budget = int(cfg.get('asr_max_arbiter_requests', 10))
    rows = []
    used = 0
    for start in range(0, max(1, math.ceil(duration)), window):
        end = min(duration, start + window)
        primary = window_text(segments, start, end)
        secondary = qwen_window(start, end)
        decision = decide_window(primary, secondary, None, agree=agree, conflict=conflict)
        arbiter_text = None
        if decision['status'] in ('minor', 'conflict') and arbiter_window is not None and used < budget:
            arbiter_text = arbiter_window(start, end)
            used += 1
            decision = decide_window(primary, secondary, arbiter_text, agree=agree, conflict=conflict)
        rows.append({'start': start, 'end': end, 'primary_chars': len(primary),
                     'secondary_chars': len(secondary), 'primary': primary,
                     'secondary': secondary, 'arbiter': arbiter_text, **decision})
    flagged = []
    for index, segment in enumerate(segments):
        mid = (segment['start'] + segment['end']) / 2
        row = next((item for item in rows if item['start'] <= mid < item['end']), None)
        if row and row['status'] in ('conflict', 'resolved_secondary'):
            flagged.append({'index': index, 'start': segment['start'], 'end': segment['end'],
                            'text': segment['text'], 'window_status': row['status']})
    conflicts = sum(1 for item in rows if item['status'] == 'conflict')
    return {
        'version': VERSION,
        'window_seconds': window,
        'windows': rows,
        'flagged_segments': flagged,
        'summary': {
            'windows': len(rows),
            'agree': sum(1 for item in rows if item['status'] == 'agree'),
            'resolved_primary': sum(1 for item in rows if item['status'] == 'resolved_primary'),
            'resolved_secondary': sum(1 for item in rows if item['status'] == 'resolved_secondary'),
            'conflicts': conflicts,
            'arbiter_requests': used,
            'cer_mean': round(sum(item['cer_primary_secondary'] for item in rows) / max(1, len(rows)), 4),
            'agreement_rate': round(sum(1 for item in rows if item['status'] == 'agree') / max(1, len(rows)), 4),
        },
    }


def _multipart(fields, files):
    boundary = '----lighthouse' + uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, (filename, content, content_type) in files.items():
        body.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                    f'Content-Type: {content_type}\r\n\r\n'.encode())
        body.extend(content)
        body.extend(b'\r\n')
    body.extend(f'--{boundary}--\r\n'.encode())
    return bytes(body), 'multipart/form-data; boundary=' + boundary


def _post(url, key, *, fields=None, files=None, json_body=None, timeout=180):
    _require_https(url)
    context = ssl.create_default_context()
    if json_body is not None:
        data = json.dumps(json_body).encode('utf-8')
        headers = {'Content-Type': 'application/json'}
    else:
        data, content_type = _multipart(fields or {}, files or {})
        headers = {'Content-Type': content_type}
    request = urllib.request.Request(url, data=data, method='POST',
                                     headers={'Authorization': 'Bearer ' + key, **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            raw = response.read(4_000_001)
            if response.status != 200:
                raise AsrError('ASR_HTTP_' + str(response.status))
    except urllib.error.HTTPError as error:
        raise AsrError('ASR_HTTP_' + str(error.code)) from None
    except urllib.error.URLError:
        raise AsrError('ASR_NETWORK') from None
    try:
        return json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        raise AsrError('ASR_RESPONSE_INVALID') from None


def parse_whisper(payload, duration):
    segments = payload.get('segments') if isinstance(payload, dict) else None
    if not isinstance(segments, list) or not segments:
        raise AsrError('ASR_WHISPER_SEGMENTS_MISSING')
    rows = []
    previous_end = 0.0
    for item in segments:
        try:
            start = float(item['start'])
            end = float(item['end'])
            text = str(item['text']).strip()
        except (KeyError, TypeError, ValueError):
            raise AsrError('ASR_WHISPER_SEGMENT_INVALID') from None
        if not text or not 0 <= start < end:
            raise AsrError('ASR_WHISPER_SEGMENT_INVALID')
        # Whisper can hallucinate repeated segments past the end of a short clip.
        # Ignore segments that start beyond the actual audio and clamp a segment
        # that only overruns the tail; do not fail an otherwise valid transcript.
        if start >= duration + 0.001:
            continue
        end = min(end, duration)
        if end <= start:
            continue
        if start < previous_end - 0.5:
            raise AsrError('ASR_WHISPER_SEGMENT_INVALID')
        rows.append({'start': round(start, 3), 'end': round(end, 3), 'text': text})
        previous_end = end
    if not rows:
        raise AsrError('ASR_WHISPER_SEGMENT_INVALID')
    return rows


def parse_qwen(payload):
    try:
        choice = payload['choices'][0]
        text = choice['message']['content']
    except (KeyError, IndexError, TypeError):
        raise AsrError('ASR_QWEN_RESPONSE_INVALID') from None
    if choice.get('finish_reason') != 'stop' or not isinstance(text, str) or not text.strip():
        raise AsrError('ASR_QWEN_RESPONSE_INCOMPLETE')
    return text.strip()


def parse_openai_text(payload):
    text = payload.get('text') if isinstance(payload, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise AsrError('ASR_ARBITER_TEXT_MISSING')
    return text.strip()


def _run(args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def probe_duration(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                             '-of', 'default=nw=1:nk=1', str(path)],
                            check=True, capture_output=True, text=True)
    try:
        return float(result.stdout.strip())
    except ValueError:
        raise AsrError('ASR_DURATION_INVALID') from None


def prepare_primary_audio(video, out):
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    _run(['ffmpeg', '-v', 'error', '-y', '-i', str(video), '-vn', '-ac', '1', '-ar', '16000',
          '-b:a', '64k', str(out)])
    if out.stat().st_size > MAX_AUDIO_BYTES:
        raise AsrError('ASR_PRIMARY_AUDIO_TOO_LARGE')
    return out


def cut_window(audio, start, end, out):
    _run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{start:.3f}', '-t', f'{max(0.1, end - start):.3f}',
          '-i', str(audio), '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(out)])
    return out


def whisper_primary(audio, cfg, duration, key=None, usage_sink=None):
    key = resolve_key(cfg, cfg.get('asr_primary_key_env', 'OPENAI_API_KEY'), key)
    url = cfg.get('asr_primary_base_url', 'https://api.openai.com/v1').rstrip('/') + '/audio/transcriptions'
    fields = {'model': cfg.get('asr_primary_model', 'whisper-1'), 'response_format': 'verbose_json',
              'temperature': '0'}
    if cfg.get('asr_language'):
        fields['language'] = cfg['asr_language']
    if cfg.get('asr_prompt'):
        fields['prompt'] = cfg['asr_prompt']
    payload = _post(url, key, fields=fields,
                    files={'file': ('audio.mp3', Path(audio).read_bytes(), 'audio/mpeg')})
    if usage_sink is not None:
        usage = payload.get('usage') if isinstance(payload.get('usage'), dict) else {}
        usage = dict(usage)
        usage.setdefault('requests', 1)
        usage.setdefault('seconds', round(float(duration), 3))
        usage_sink('whisper', usage)
    return parse_whisper(payload, duration)


def qwen_window(audio, start, end, cfg, workdir, key=None, usage_sink=None):
    import base64
    key = resolve_key(cfg, cfg.get('asr_qwen_key_env', 'DASHSCOPE_API_KEY'), key)
    part = cut_window(audio, start, end, Path(workdir) / f'qwen-{int(start):05d}.wav')
    encoded = base64.b64encode(part.read_bytes()).decode('ascii')
    if len(encoded) + 23 > MAX_QWEN_INLINE:
        raise AsrError('ASR_QWEN_INLINE_TOO_LARGE')
    payload = {'model': cfg.get('asr_qwen_model', 'qwen3-asr-flash'), 'stream': False,
               'messages': [{'role': 'user', 'content': [{'type': 'input_audio',
                                                           'input_audio': {'data': 'data:audio/wav;base64,' + encoded}}]}],
               'asr_options': {'language': cfg.get('asr_language', 'ja'), 'enable_itn': False}}
    url = cfg.get('asr_qwen_base_url', 'https://dashscope.aliyuncs.com/compatible-mode/v1').rstrip('/') + '/chat/completions'
    response = _post(url, key, json_body=payload)
    if usage_sink is not None:
        usage = response.get('usage') if isinstance(response.get('usage'), dict) else {}
        usage = dict(usage)
        usage.setdefault('requests', 1)
        usage.setdefault('seconds', round(float(end) - float(start), 3))
        usage_sink('qwen', usage)
    return parse_qwen(response)


def arbiter_window(audio, start, end, cfg, workdir, key=None, usage_sink=None):
    key = resolve_key(cfg, cfg.get('asr_arbiter_key_env', 'OPENAI_API_KEY'), key)
    part = cut_window(audio, start, end, Path(workdir) / f'arbiter-{int(start):05d}.wav')
    url = cfg.get('asr_arbiter_base_url', 'https://api.openai.com/v1').rstrip('/') + '/audio/transcriptions'
    fields = {'model': cfg.get('asr_arbiter_model', 'gpt-4o-transcribe'), 'response_format': 'json'}
    response = _post(url, key, fields=fields,
                     files={'file': ('window.wav', part.read_bytes(), 'audio/wav')})
    if usage_sink is not None:
        usage = response.get('usage') if isinstance(response.get('usage'), dict) else {}
        usage = dict(usage)
        usage.setdefault('requests', 1)
        usage.setdefault('seconds', round(float(end) - float(start), 3))
        usage_sink('arbiter', usage)
    return parse_openai_text(response)


def transcribe(video, workdir, cfg, *, primary_key=None, qwen_key=None, arbiter_key=None):
    """Run the full three-way pipeline and return segments, quality and usage."""
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(video)
    limit = cfg.get('max_video_seconds', 1200)
    if duration > limit + 0.001:
        raise AsrError('ASR_DURATION_EXCEEDS_LIMIT')
    audio = prepare_primary_audio(video, workdir / 'primary.mp3')
    segments = whisper_primary(audio, cfg, duration, primary_key)
    usage = {'primary': 1, 'qwen': 0, 'arbiter': 0}

    def qwen_call(start, end):
        usage['qwen'] += 1
        return qwen_window(audio, start, end, cfg, workdir, qwen_key)

    def arbiter_call(start, end):
        usage['arbiter'] += 1
        return arbiter_window(audio, start, end, cfg, workdir, arbiter_key)

    quality = cross_check(segments, duration, cfg, qwen_call, arbiter_call)
    quality['usage'] = usage
    quality['duration'] = round(duration, 3)
    return {'segments': segments, 'quality': quality}
