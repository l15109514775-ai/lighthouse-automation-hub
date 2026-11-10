#!/usr/bin/env python3
"""Shanghai poller: every 30 minutes approve queued jobs and run the audio fetcher."""
import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.request

API = 'https://example-env.api.tcloudbasegateway.com/v1/rdb/rest/rpc/st_subtitle_auto_decide'
SECRETS = '/root/subtitle/secrets.env'
FETCH = '/root/subtitle/fetch.py'
LOCK = '/root/subtitle/poll.lock'
INTERVAL = int(os.environ.get('SUBTITLE_POLL_SECONDS', '1800'))


def load_env():
    env = {}
    for line in open(SECRETS, encoding='utf-8'):
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


def log(**fields):
    print(json.dumps({'at': time.strftime('%F %T'), **fields}, ensure_ascii=False), flush=True)


def main():
    global ENV
    ENV = load_env()
    lock = open(LOCK, 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log(event='already_running')
        return
    while True:
        try:
            data = rpc({'p_key': ENV['SUBTITLE_PLATFORM_TOKEN'], 'p_limit': 3})
            queued = data.get('queued') or []
            log(event='cycle', state=data.get('state'), approved=len(data.get('approved') or []),
                queued=len(queued))
            for job in queued:
                log(event='fetch_start', job=job)
                started = time.time()
                result = subprocess.run([sys.executable, FETCH, job], capture_output=True, text=True, timeout=1800)
                log(event='fetch_done', job=job, code=result.returncode,
                    seconds=round(time.time() - started, 1),
                    tail=(result.stdout or result.stderr or '')[-400:])
        except Exception as error:
            log(event='cycle_failed', error=type(error).__name__, detail=str(error)[:200])
        time.sleep(INTERVAL)


if __name__ == '__main__':
    ENV = {}
    main()
