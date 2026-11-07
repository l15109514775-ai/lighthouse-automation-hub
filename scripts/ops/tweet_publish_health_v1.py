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
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.hub.config import Settings  # noqa: E402
from services.hub.db import Store  # noqa: E402

VERSION = 'tweet-publish-health-v1'


def deferred_ingests(store, now, threshold_seconds=600):
    """Official captures that have not reached a media-ingest attempt.

    Discovery can be ``completed`` while its child waits for the global heavy
    lock. No ``tweet_process`` or publish job exists yet, so publish-failure
    checks alone report this state as healthy.
    """
    rows = store.query(
        "SELECT job_id,source_id,state,error_code,attempts,created_at "
        "FROM jobs WHERE job_type='x_official_media_ingest_v1' "
        "AND state IN ('pending','retry_scheduled') AND created_at<=? "
        "ORDER BY created_at", (now - threshold_seconds,))
    return [{
        'job_id': row['job_id'],
        'source_id': row['source_id'],
        'state': row['state'],
        'error_code': row['error_code'],
        'attempts': row['attempts'],
        'waiting_seconds': round(max(0.0, now - row['created_at'])),
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(row['created_at'])),
    } for row in rows]


def expired_ingests(store, since):
    """Completed media ingests that expired before creating a tweet_process.

    These have no publish child, so publish-failure checks cannot see them.
    """
    rows = store.query(
        "SELECT job_id,source_id,state,result,updated_at "
        "FROM jobs WHERE job_type='x_official_media_ingest_v1' "
        "AND state='completed' AND updated_at>=? ORDER BY updated_at DESC", (since,))
    values = []
    for row in rows:
        try:
            result = json.loads(row['result'] or '{}')
        except (TypeError, ValueError):
            continue
        ingestion = result.get('ingestion') or {}
        if ingestion.get('error_code') != 'OWNER_TWO_HOUR_WINDOW_EXPIRED':
            continue
        values.append({
            'job_id': row['job_id'],
            'source_id': row['source_id'],
            'error_code': ingestion.get('error_code'),
            'disposition': ingestion.get('disposition'),
            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(row['updated_at'])),
        })
    return values


def caption_digest(parent):
    try:
        result = parent['result']
        if isinstance(result, str):
            result = json.loads(result)
        entry = result['machine_web']
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
    parser.add_argument('--discovery-hours', type=float, default=2.0,
                        help='window for failed x_discovery jobs')
    parser.add_argument('--deferred-ingest-seconds', type=float, default=600.0,
                        help='flag official captures waiting this long before ingest')
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
        cleared = False
        if parent and not args.include_resolved:
            published = store.query("SELECT 1 FROM publications WHERE source_id=? AND state='completed'",
                                    (parent['source_id'],))
            if parent['state'] == 'completed' or published:
                continue
            # A cleared backlog with a failed publish child is still a dropped
            # tweet: the 2-hour window expired before anyone recovered it.
            cleared = parent['state'] == 'failed' and parent['error_code'] == 'OWNER_BACKLOG_CLEARED'
        if parent:
            payload = parent['payload']
            if isinstance(payload, str):
                payload = json.loads(payload)
            entry['source_url'] = payload.get('url')
            entry['caption_sha256'] = caption_digest(parent)
            entry['parent_state'] = parent['state']
            entry['parent_error_code'] = parent['error_code']
            if cleared:
                entry['cleared_without_publication'] = True
        failures.append(entry)
    uncertain = []
    for pub in store.query("SELECT source_id,state,external_id,updated_at FROM publications "
                           "WHERE state IN ('dispatching','unknown') ORDER BY updated_at DESC"):
        uncertain.append({'source_id': pub['source_id'], 'state': pub['state'],
                          'external_id': pub['external_id'],
                          'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(pub['updated_at']))})
    source_issues = []
    seen_source = set()
    for poll in store.query("SELECT source_key,checked_at,error_code,metadata FROM source_polls "
                            "WHERE source_key LIKE 'x-api:%' AND checked_at>=? ORDER BY checked_at DESC", (since,)):
        try:
            raw = poll['metadata'] or '{}'
            meta = raw if isinstance(raw, dict) else json.loads(raw)
        except ValueError:
            meta = {}
        rejected = meta.get('rejected_posts') or []
        skipped = [item for item in (meta.get('skipped_posts') or [])
                   if item.get('reason') not in ('OWNER_TWO_HOUR_WINDOW_EXPIRED', 'OWNER_BACKLOG_CLEARED')]
        if poll['error_code'] or rejected or skipped:
            source_issues.append({
                'kind': 'source_poll',
                'source_key': poll['source_key'],
                'checked_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(poll['checked_at'])),
                'error_code': poll['error_code'],
                'rejected_posts': rejected,
                'skipped_posts': skipped,
                'parse_blocked_count': meta.get('parse_blocked_count', 0),
            })
    # Durable parse-blocked events survive later clean polls (the poll row is
    # overwritten each cycle). Retweets and other unsupported references land
    # here and are exactly the "incomplete tweet" signal the owner wants.
    for event in store.query("SELECT occurred_at,data FROM events WHERE name='task.failed' "
                             "AND occurred_at>=? ORDER BY occurred_at DESC", (since,)):
        try:
            data = json.loads(event['data'])
        except (TypeError, ValueError):
            continue
        if data.get('source_platform') != 'x' or data.get('stage') != 'source_parse_blocked':
            continue
        key = (data.get('source_id'), data.get('code'))
        if key in seen_source:
            continue
        seen_source.add(key)
        source_issues.append({
            'kind': 'source_parse_blocked',
            'source_id': data.get('source_id'),
            'error_code': data.get('code'),
            'job_id': data.get('job_id'),
            'occurred_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(event['occurred_at'])),
        })
    discovery_since = time.time() - max(3600.0, args.discovery_hours * 3600)
    discovery_failures = []
    for job in store.query(
            "SELECT job_id,error_code,stage,updated_at FROM jobs WHERE job_type='x_discovery' "
            "AND state='failed' AND updated_at>=? ORDER BY updated_at DESC LIMIT 20", (discovery_since,)):
        discovery_failures.append({
            'job_id': job['job_id'], 'error_code': job['error_code'], 'stage': job['stage'],
            'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(job['updated_at'])),
            '_epoch': job['updated_at'],
        })
    last_success = store.query("SELECT MAX(created_at) AS at FROM jobs "
                               "WHERE job_type='x_discovery' AND state='completed'")[0]['at'] or 0
    discovery_active = any(item['_epoch'] > last_success for item in discovery_failures)
    for item in discovery_failures:
        item.pop('_epoch', None)
    discovery = {
        'last_success_at': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(last_success)) if last_success else None,
        'failures': discovery_failures,
        'active': discovery_active,
    }
    x_budget = {}
    try:
        limits = settings.config['budgets']['x']
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        row = store.query("SELECT COUNT(*) calls,COALESCE(SUM(cost_usd),0) cost FROM api_calls "
                          "WHERE provider='x' AND started_at>=?", (start.timestamp(),))[0]
        call_limit = float(limits['daily_calls'])
        usd_limit = float(limits['daily_usd'])
        calls, cost = int(row['calls']), float(row['cost'])
        x_budget = {
            'daily_calls_used': calls, 'daily_calls_limit': call_limit,
            'daily_calls_remaining': max(0, int(call_limit) - calls),
            'daily_usd_used': round(cost, 6), 'daily_usd_limit': usd_limit,
            'daily_usd_remaining': round(max(0.0, usd_limit - cost), 6),
            'daily_reset_at': (start.timestamp() + 86400),
            'near_call_limit': call_limit > 0 and calls >= call_limit * 0.8,
            'near_usd_limit': usd_limit > 0 and cost >= usd_limit * 0.8,
        }
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        x_budget = {'error': type(exc).__name__}
    deferred = deferred_ingests(store, time.time(), args.deferred_ingest_seconds)
    expired = expired_ingests(store, since)
    report = {'version': VERSION, 'window_hours': args.hours, 'failed': failures,
              'cleared_without_publication': sum(1 for item in failures if item.get('cleared_without_publication')),
              'deferred_ingests': deferred,
              'expired_ingests': expired,
              'source_issues': source_issues,
              'discovery': discovery,
              'x_budget': x_budget,
              'uncertain_publications': uncertain,
              'healthy': not failures and not uncertain and not deferred and not expired and
                         not source_issues and not discovery_active and
                         not x_budget.get('near_call_limit') and not x_budget.get('near_usd_limit')}
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text + '\n', 'utf-8')
    print(text)
    return 0 if report['healthy'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
