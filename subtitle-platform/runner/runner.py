#!/usr/bin/env python3
"""Self-hosted subtitle runner: auto-approve requests, then execute queued jobs.

Every SUBTITLE_POLL_SECONDS (default 300):
  1. call st_subtitle_auto_decide with the worker key (auto-approves whitelisted
     requests under the daily limits and returns queued job ids);
  2. run platform_worker.py --job-id <id> --execute for each queued job, one at
     a time, so the 20-minute slice limits and the platform lease stay valid.

Secrets come from the environment (systemd EnvironmentFile or docker --env-file);
nothing is written to disk. Logs are single-line JSON on stdout.
"""
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API_BASE = os.environ['SUBTITLE_API_BASE'].rstrip('/')
GATEWAY_KEY = os.environ['SUBTITLE_PLATFORM_GATEWAY_KEY']
WORKER_TOKEN = os.environ['SUBTITLE_PLATFORM_TOKEN']
CONFIG = os.environ['SUBTITLE_WORKER_CONFIG']
WORK_ROOT = Path(os.environ.get('SUBTITLE_WORK_ROOT', str(HERE / 'work')))
POLL_SECONDS = int(os.environ.get('SUBTITLE_POLL_SECONDS', '300'))
JOB_TIMEOUT = int(os.environ.get('SUBTITLE_JOB_TIMEOUT', '2700'))
PYTHON = os.environ.get('SUBTITLE_PYTHON', sys.executable or 'python3')
RUNNING = True


def log(**fields):
    print(json.dumps(fields, ensure_ascii=False), flush=True)


def rpc(name, payload):
    request = urllib.request.Request(
        API_BASE + '/' + name,
        data=json.dumps(payload).encode('utf-8'),
        method='POST',
        headers={'Authorization': 'Bearer ' + GATEWAY_KEY, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=60, context=ssl.create_default_context()) as response:
        raw = response.read(1_000_001)
    return json.loads(raw.decode('utf-8'))


def run_job(job_id):
    command = [PYTHON, str(HERE / 'platform_worker.py'), '--job-id', job_id,
               '--work-root', str(WORK_ROOT), '--config', CONFIG, '--execute']
    started = time.time()
    try:
        result = subprocess.run(command, cwd=str(HERE), timeout=JOB_TIMEOUT,
                                capture_output=True, text=True)
        log(event='job_finished', job_id=job_id, code=result.returncode,
            seconds=round(time.time() - started, 1),
            tail=(result.stdout or result.stderr or '')[-400:])
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        log(event='job_timeout', job_id=job_id, seconds=round(time.time() - started, 1))
        return False


def stop(_signum, _frame):
    global RUNNING
    RUNNING = False


def main():
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    log(event='runner_started', api=API_BASE, poll_seconds=POLL_SECONDS,
        work_root=str(WORK_ROOT), config=CONFIG)
    while RUNNING:
        queued = []
        try:
            data = rpc('st_subtitle_auto_decide', {'p_key': WORKER_TOKEN, 'p_limit': 3})
            queued = data.get('queued') or []
            log(event='cycle', state=data.get('state'), approved=len(data.get('approved') or []),
                queued=len(queued), limits=data.get('limits'))
        except urllib.error.HTTPError as error:
            log(event='rpc_failed', status=error.code)
        except Exception as error:  # network, JSON, TLS: retry next cycle
            log(event='rpc_failed', error=type(error).__name__, detail=str(error)[:200])
        for job_id in queued:
            if not RUNNING:
                break
            log(event='job_started', job_id=job_id)
            run_job(job_id)
        deadline = time.time() + (10 if queued else POLL_SECONDS)
        while RUNNING and time.time() < deadline:
            time.sleep(1)
    log(event='runner_stopped')


if __name__ == '__main__':
    main()
