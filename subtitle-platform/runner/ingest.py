#!/usr/bin/env python3
"""Receive a pre-segmented audio file from the Shanghai fetcher and run the worker.

The Shanghai server downloads the original video from CloudBase (same region),
extracts a 16 kHz mono mp3 and POSTs it here with the claim it took. This server
holds the OpenAI/DashScope keys, resumes the same lease from the transferred
claim, processes the audio and publishes; the Shanghai side deletes its copy.
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORK = Path(os.environ.get('SUBTITLE_WORK_ROOT', '/work'))
TOKEN = os.environ['SUBTITLE_INGEST_TOKEN']
MAX_BYTES = 60 * 1024 * 1024
LOCK = threading.Lock()
HERE = Path(__file__).resolve().parent


def log(**fields):
    print(json.dumps(fields, ensure_ascii=False), flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = 'subtitle-ingest/1'
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        return

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != '/ingest':
            return self._reply(404, {'error': 'not_found'})
        if self.headers.get('X-Token') != TOKEN:
            return self._reply(403, {'error': 'forbidden'})
        try:
            job_id = self.headers['X-Job-Id']
            claim = json.loads(base64.b64decode(self.headers['X-Claim']).decode())
            source_sha = self.headers['X-Source-Sha256']
            audio_sha = self.headers['X-Audio-Sha256']
        except (KeyError, ValueError, TypeError):
            return self._reply(400, {'error': 'headers'})
        if claim.get('job_id') != job_id or claim.get('sha256') != source_sha:
            return self._reply(409, {'error': 'claim_mismatch'})
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 < length <= MAX_BYTES:
            return self._reply(413, {'error': 'size'})
        data = self.rfile.read(length)
        if hashlib.sha256(data).hexdigest() != audio_sha:
            return self._reply(422, {'error': 'audio_sha256'})
        incoming = WORK / 'incoming' / job_id
        incoming.mkdir(parents=True, exist_ok=True)
        audio = incoming / 'audio.mp3'
        audio.write_bytes(data)
        control = WORK / 'control' / job_id
        control.mkdir(parents=True, exist_ok=True)
        (control / 'session.json').write_text(json.dumps(
            {'claim': claim, 'claimed_at': time.time()}, ensure_ascii=False), 'utf-8')
        log(event='ingest_received', job_id=job_id, bytes=len(data), sha256=audio_sha)
        if not LOCK.acquire(blocking=False):
            return self._reply(429, {'error': 'busy'})
        try:
            command = [sys.executable, str(HERE / 'platform_worker.py'), '--job-id', job_id,
                       '--work-root', str(WORK), '--audio-file', str(audio), '--execute']
            started = time.time()
            result = subprocess.run(command, cwd=str(HERE), capture_output=True, text=True, timeout=3600)
            log(event='ingest_worker_done', job_id=job_id, code=result.returncode,
                seconds=round(time.time() - started, 1), tail=(result.stdout or result.stderr or '')[-400:])
            return self._reply(200, {'state': 'processed' if result.returncode == 0 else 'failed',
                                     'code': result.returncode})
        finally:
            LOCK.release()


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('SUBTITLE_INGEST_PORT', '18081'))), Handler)
    log(event='ingest_started', port=server.server_address[1], work=str(WORK))
    server.serve_forever()


if __name__ == '__main__':
    main()
