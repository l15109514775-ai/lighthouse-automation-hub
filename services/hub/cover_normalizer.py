"""Deterministic FFmpeg-only cover composition. No model, network or text renderer.

The recorded maxres JPEG stays unchanged. The derived JPEG is not official
original bytes. Determinism is tied to the recorded FFmpeg version and filter;
existing verified output is reused without executing FFmpeg.
"""
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import uuid

from .errors import HubError
from .locks import resource_guard

VERSION = 'lighthouse-derived-cover-v3'
POLICY = 'blur-safe-foreground-v1'
MAX_BYTES = 1_000_000
FOREGROUND = {'x': 192, 'y': 108, 'width': 896, 'height': 504}
SAFE_CROP = {'x': 160, 'y': 0, 'width': 960, 'height': 720}
FILTER_COMPLEX = ('[0:v]split=2[bg0][fg0];'
    '[bg0]scale=1280:720:force_original_aspect_ratio=increase:flags=lanczos,crop=1280:720,gblur=sigma=35:steps=3[bg];'
    '[fg0]scale=896:504:force_original_aspect_ratio=decrease:flags=lanczos,pad=896:504:(ow-iw)/2:(oh-ih)/2:color=black[fg];'
    '[bg][fg]overlay=192:108:format=auto,setsar=1,format=yuvj420p[out]')


def _fail(code):
    raise HubError(code, review=True)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def jpeg_dimensions(data):
    if len(data) < 4 or data[:2] != b'\xff\xd8':
        return None
    offset = 2
    sof = {192, 193, 194, 195, 197, 198, 199, 201, 202, 203, 205, 206, 207}
    while offset < len(data):
        if data[offset] != 255:
            return None
        offset += 1
        while offset < len(data) and data[offset] == 255:
            offset += 1
        if offset >= len(data):
            return None
        marker = data[offset]; offset += 1
        if marker in (217, 218):
            return None
        if marker == 1 or 208 <= marker <= 215:
            continue
        if offset + 2 > len(data):
            return None
        size = int.from_bytes(data[offset:offset+2], 'big')
        if size < 2 or offset + size > len(data):
            return None
        if marker in sof:
            if size < 8:
                return None
            return (int.from_bytes(data[offset+5:offset+7], 'big'),
                    int.from_bytes(data[offset+3:offset+5], 'big'))
        offset += size
    return None


def _safe(path, base):
    path = Path(path)
    if (not path.is_absolute() or path.resolve() != path or not path.is_relative_to(base) or
            any(parent.is_symlink() for parent in (path, *path.parents))):
        _fail('COVER_NORMALIZE_PATH_INVALID')
    return path


def _private_folder(path, base):
    _safe(path, base)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    _safe(path, base)
    if not path.is_dir() or os.name != 'nt' and path.stat().st_mode & 0o077:
        _fail('COVER_NORMALIZE_PATH_INVALID')


def _read(path):
    if not path.is_file() or path.is_symlink():
        _fail('COVER_NORMALIZE_SOURCE_INVALID')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_BYTES:
            _fail('COVER_NORMALIZE_SOURCE_INVALID')
        data = b''
        while True:
            part = os.read(fd, 65536)
            if not part:
                break
            data += part
            if len(data) > MAX_BYTES:
                _fail('COVER_NORMALIZE_SOURCE_CHANGED')
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (len(data) != before.st_size or
            any(getattr(before, key) != getattr(after, key) for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns'))):
        _fail('COVER_NORMALIZE_SOURCE_CHANGED')
    return data


def _transform():
    return {'filter_complex': FILTER_COMPLEX, 'foreground_rect': FOREGROUND,
            'safe_crop': SAFE_CROP, 'background': {'width': 1280, 'height': 720, 'blur_sigma': 35},
            'foreground_fit': 'contain', 'foreground_padding': 'black'}


def _sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _cache(output, provenance, input_record):
    if not output.exists() and not provenance.exists():
        return None
    if not output.is_file() or not provenance.is_file() or output.is_symlink() or provenance.is_symlink():
        _fail('COVER_NORMALIZE_EXISTING_OUTPUT_CONFLICT')
    try:
        record = json.loads(_read(provenance))
        data = _read(output)
        if (set(record) != {'version', 'policy_version', 'input', 'output', 'transform', 'ffmpeg_version',
                'image_edited', 'no_text_regenerated', 'official_original_bytes', 'created_at'} or
                record['version'] != VERSION or record['policy_version'] != POLICY or
                record['input'] != input_record or record['transform'] != _transform() or
                record['output'] != {'file': str(output), 'bytes': len(data), 'sha256': _sha(data),
                    'width': 1280, 'height': 720, 'mime_type': 'image/jpeg'} or
                jpeg_dimensions(data) != (1280, 720) or record['image_edited'] is not True or
                record['no_text_regenerated'] is not True or record['official_original_bytes'] is not False or
                not isinstance(record['ffmpeg_version'], str) or not record['ffmpeg_version'].startswith('ffmpeg version ') or
                not isinstance(record['created_at'], str)):
            raise ValueError()
        return record
    except (ValueError, TypeError, KeyError):
        _fail('COVER_NORMALIZE_EXISTING_OUTPUT_CONFLICT')


def normalize_cover(root, job_id, source_video_id, source_file, source_sha256, source_url, *, ffmpeg='ffmpeg'):
    root = Path(root)
    try:
        if str(uuid.UUID(job_id)) != job_id or not re.fullmatch('[A-Za-z0-9_-]{11}', source_video_id):
            raise ValueError()
        if not re.fullmatch('[a-f0-9]{64}', source_sha256):
            raise ValueError()
        if source_url != 'https://i.ytimg.com/vi/' + source_video_id + '/maxresdefault.jpg':
            raise ValueError()
        _safe(root, root)
        job = root/'media'/job_id
        source = _safe(Path(source_file), job)
        attempt = source.parent.name
        if (source.name != 'cover-source-official-maxres.jpg' or
                source.parent.parent != job/'short-video-attempts' or str(uuid.UUID(attempt)) != attempt):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        _fail('COVER_NORMALIZE_INPUT_INVALID')
    original = _read(source)
    if _sha(original) != source_sha256:
        _fail('COVER_NORMALIZE_SOURCE_HASH_MISMATCH')
    if jpeg_dimensions(original) != (1280, 720):
        _fail('COVER_NORMALIZE_SOURCE_INVALID')
    input_record = {'file': str(source), 'bytes': len(original), 'sha256': source_sha256,
                    'source_url': source_url, 'width': 1280, 'height': 720}
    destination = job/'cover-assets-v3'
    _private_folder(destination, root)
    heavy = root/'data/locks/heavy'
    _safe(heavy, root)
    with resource_guard(heavy, destination/'normalizer.lock'):
        output = _safe(destination/'blur-safe-cover.jpg', root)
        provenance = _safe(destination/'provenance.json', root)
        cached = _cache(output, provenance, input_record)
        if cached:
            return {'output_file': str(output), 'provenance_file': str(provenance),
                    'output_sha256': cached['output']['sha256'], 'provenance_sha256': _sha(provenance.read_bytes()),
                    'reused': True, 'provenance': cached}
        binary = shutil.which(ffmpeg)
        if not binary:
            _fail('COVER_NORMALIZE_FFMPEG_MISSING')
        env = {key: os.environ[key] for key in ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP', 'LANG', 'LC_ALL') if key in os.environ}
        partial = destination/('.'+uuid.uuid4().hex+'.partial.jpg')
        document = destination/('.'+uuid.uuid4().hex+'.partial.json')
        published = {}
        try:
            version = subprocess.run([binary, '-version'], stdin=subprocess.DEVNULL, capture_output=True,
                                     text=True, encoding='utf-8', timeout=10, env=env)
            if version.returncode or len(version.stdout) > 65536 or not version.stdout.startswith('ffmpeg version '):
                _fail('COVER_NORMALIZE_FFMPEG_FAILED')
            command = [binary, '-nostdin', '-hide_banner', '-loglevel', 'error', '-threads', '1',
                       '-filter_threads', '1', '-filter_complex_threads', '1', '-f', 'image2pipe', '-vcodec', 'mjpeg', '-i', 'pipe:0',
                       '-filter_complex', FILTER_COMPLEX, '-map', '[out]', '-frames:v', '1',
                       '-c:v', 'mjpeg', '-q:v', '2', '-fflags', '+bitexact', '-flags:v', '+bitexact',
                       '-map_metadata', '-1', '-map_chapters', '-1', '-f', 'image2', '-n', str(partial)]
            # Decode the exact already-hashed bytes, never a racy reread of an
            # input path or any network URL by FFmpeg.
            result = subprocess.run(command, input=original, capture_output=True, timeout=45, env=env)
            if result.returncode:
                _fail('COVER_NORMALIZE_FFMPEG_FAILED')
            if _read(source) != original:
                _fail('COVER_NORMALIZE_SOURCE_CHANGED')
            encoded = _read(partial)
            if jpeg_dimensions(encoded) != (1280, 720):
                _fail('COVER_NORMALIZE_OUTPUT_INVALID')
            record = {'version': VERSION, 'policy_version': POLICY, 'input': input_record,
                      'output': {'file': str(output), 'bytes': len(encoded), 'sha256': _sha(encoded),
                                 'width': 1280, 'height': 720, 'mime_type': 'image/jpeg'},
                      'transform': _transform(), 'ffmpeg_version': version.stdout.splitlines()[0],
                      'image_edited': True, 'no_text_regenerated': True, 'official_original_bytes': False,
                      'created_at': datetime.now(timezone.utc).isoformat()}
            partial.chmod(0o600)
            with partial.open('r+b') as stream:
                stream.flush(); os.fsync(stream.fileno())
            wire = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
            fd = os.open(document, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0), 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(wire); stream.flush(); os.fsync(stream.fileno())
            # Exclusive links cannot replace an existing operator file.
            for source_path, final in ((partial, output), (document, provenance)):
                os.link(source_path, final)
                value = final.stat(); published[final] = (value.st_dev, value.st_ino)
            _sync_directory(destination)
            published.clear()
            return {'output_file': str(output), 'provenance_file': str(provenance),
                    'output_sha256': record['output']['sha256'], 'provenance_sha256': _sha(wire),
                    'reused': False, 'provenance': record}
        except (OSError, subprocess.SubprocessError):
            _fail('COVER_NORMALIZE_FFMPEG_FAILED')
        finally:
            for final, identity in published.items():
                if final.is_file() and not final.is_symlink() and (final.stat().st_dev, final.stat().st_ino) == identity:
                    final.unlink()
            for temporary in (partial, document):
                if temporary.is_file() and not temporary.is_symlink():
                    temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'job-id', 'source-video-id', 'source-file', 'source-sha256', 'source-url'):
        parser.add_argument('--'+name, required=True)
    args = parser.parse_args()
    try:
        result = normalize_cover(args.root, args.job_id, args.source_video_id, args.source_file, args.source_sha256, args.source_url)
        print(json.dumps(result, ensure_ascii=False)); return 0
    except HubError as error:
        print(json.dumps({'state': 'blocked', 'error_code': error.code, 'source_modified': False, 'network_calls': 0}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
