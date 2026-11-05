#!/usr/bin/env python3
"""Print a read-only health summary of recent machine tweet publications.

Exit code 0 means: no failed publish job in the window and no publication left
in dispatching/unknown. Exit code 1 means an operator should look.

Run inside the tweet worker (it owns the database):

    docker exec -u 10001 -e HUB_ROOT=/opt/lighthouse-hub \
      -e HUB_CONFIG=/opt/lighthouse-hub/config/tweet-runtime-v4.toml \
      -w /opt/lighthouse-hub lighthouse-hub-tweet-worker-1 \
      python scripts/ops/tweet_publish_health_v1.py --hours 24
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.hub.config import Settings  # noqa: E402
from services.hub.db import Store  # noqa: E402

VERSION = 'tweet-publish-health-v1'


def caption_digest(parent):
    try:
        entry = parent['result']['machine_web']
        manifest = json.loads(Path(entry['manifest_file']).read_text('utf-8'))
        return manifest.get('caption_sha256')
    except (KeyError, TypeError, ValueError, OSError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--hours', type=float, default=24.0)
    parser.add_argument('--report', type=Path, help='also write the JSON report to this path')
    parser.add_argument('--include-resolved', action='store_true',
                        help='include failures whose parent or publication already completed')
    args = parser.parse_args()
    settings = Settings.load()
    store = Store(settings.db_path)
    since = time.time() - args.hours * 3600
    failures = []
    for child in store.query(
            "SELECT job_id,source_id,state,attempts,error_code,stage,updated_at FROM jobs "
            "WHERE job_type='tweet_machine_publish' AND state IN ('failed','publish_unknown') AND updated_at>=? "
            "ORDER BY updated_at DESC", (since,)):
        parent = store.get(child['source_id'])
        entry = {'job_id': child['job_id'], 'parent_job_id': child['source_id'],
                 'state': child['state'], 'attempts': child['attempts'],
                 'error_code': child['error_code'], 'stage': child['stage'],
                 'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(child['updated_at']))}
        if parent and not args.include_resolved:
            published = store.query("SELECT 1 FROM publications WHERE source_id=? AND state='completed'",
                                    (parent['source_id'],))
            cleared = parent['state'] == 'failed' and parent['error_code'] == 'OWNER_BACKLOG_CLEARED'
            if parent['state'] == 'completed' or published or cleared:
                continue
        if parent:
            entry['source_url'] = parent['payload'].get('url')
            entry['caption_sha256'] = caption_digest(parent)
            entry['parent_state'] = parent['state']
        failures.append(entry)
    uncertain = []
    for pub in store.query("SELECT source_id,state,external_id,updated_at FROM publications "
                           "WHERE state IN ('dispatching','unknown') ORDER BY updated_at DESC"):
        uncertain.append({'source_id': pub['source_id'], 'state': pub['state'],
                          'external_id': pub['external_id'],
                          'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(pub['updated_at']))})
    report = {'version': VERSION, 'window_hours': args.hours, 'failed': failures,
              'uncertain_publications': uncertain,
              'healthy': not failures and not uncertain}
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + '\n', 'utf-8')
    print(text)
    return 0 if report['healthy'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
