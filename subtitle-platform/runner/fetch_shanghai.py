#!/usr/bin/env python3
"""Shanghai fetcher: download a claimed job's video, extract audio, send to SV, clean up."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = 'https://subtitledev-d2gwhqslfee550692.api.tcloudbasegateway.com/v1/rdb/rest/rpc/st_cloud_worker_call'
INGEST = 'http://43.153.83.217:18081/ingest'
ROOT = '/root/subtitle-work'


def load_env(path='/root/subtitle/secrets.env'):
    env = {}
    for line in open(path, encoding='utf-8'):
        line = line.strip()
        if line and '=' in line:
            k, v = line.split('=', 1)
            env[k] = v
    return env


def rpc(payload, timeout=60):
    req = urllib.request.Request(API, data=json.dumps(payload).encode(), method='POST',
                                 headers={'Authorization': 'Bearer ' + ENV['SUBTITLE_PLATFORM_GATEWAY_KEY'],
                                          'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def main():
    global ENV
    job = sys.argv[1]
    ENV = load_env()
    claim = rpc({'p_key': ENV['SUBTITLE_PLATFORM_TOKEN'], 'p_action': 'claim', 'p_job': job,
                 'p_payload': {'workflow_version': 'per-job-v1'}})
    print(json.dumps({'event': 'claimed', 'state': claim.get('state'), 'sources': len(claim.get('sources') or [])}), flush=True)
    if claim.get('state') != 'running':
        return 1
    work = os.path.join(ROOT, job)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    source = os.path.join(work, 'source.media')
    digest = hashlib.sha256()
    total = 0
    for index, item in enumerate(claim['sources']):
        req = urllib.request.Request(item['url'], headers={'Accept-Encoding': 'identity'}, method='GET')
        with urllib.request.urlopen(req, timeout=300) as r, open(source, 'ab') as out:
            count = 0
            while True:
                block = r.read(1024 ** 2)
                if not block:
                    break
                out.write(block)
                digest.update(block)
                count += len(block)
                total += len(block)
            if count != item['byte_size']:
                raise SystemExit('chunk_mismatch %d %d %d' % (index, count, item['byte_size']))
    if total != claim['byte_size'] or digest.hexdigest() != claim['sha256']:
        raise SystemExit('source_hash_mismatch')
    audio = os.path.join(work, 'audio.mp3')
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', source, '-vn', '-ac', '1', '-ar', '16000',
                    '-b:a', '64k', audio], check=True)
    audio_sha = hashlib.sha256(open(audio, 'rb').read()).hexdigest()
    audio_bytes = os.path.getsize(audio)
    import base64
    headers = {'Content-Type': 'audio/mpeg', 'X-Token': ENV['SUBTITLE_INGEST_TOKEN'], 'X-Job-Id': job,
               'X-Claim': base64.b64encode(json.dumps(claim).encode()).decode(),
               'X-Source-Sha256': claim['sha256'], 'X-Audio-Sha256': audio_sha}
    with open(audio, 'rb') as fh:
        req = urllib.request.Request(INGEST, data=fh.read(), method='POST', headers=headers)
        with urllib.request.urlopen(req, timeout=3600) as r:
            result = json.loads(r.read().decode())
    print(json.dumps({'event': 'ingested', 'job': job, 'audio_bytes': audio_bytes,
                      'audio_sha256': audio_sha, 'result': result}), flush=True)
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == '__main__':
    ENV = {}
    raise SystemExit(main())
