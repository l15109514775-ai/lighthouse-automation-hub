#!/usr/bin/env python3
"""Requeue one failed machine tweet publish without touching attempts or history.

Dry-run by default. ``--apply`` requires the owner to name the parent job and to
provide a reason; the tool refuses when a publication row exists, when the child
is in an uncertain state, when the source is tombstoned without an explicit
owner-backfill authorization, or when the child has exhausted its attempts.

Run inside the tweet worker (it owns the SQLite file):

    docker exec -u 10001 -e HUB_ROOT=/opt/lighthouse-hub \
      -e HUB_CONFIG=/opt/lighthouse-hub/config/tweet-runtime-v4.toml \
      -w /opt/lighthouse-hub lighthouse-hub-tweet-worker-1 \
      python scripts/ops/requeue_machine_tweet_publish_v1.py --list

    ... --parent <tweet_process_job_id> --apply --reason "owner chat ..."
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
from services.hub.machine_tweets import owner_backfill_authorization  # noqa: E402

VERSION = 'requeue-machine-tweet-publish-v1'
PARENT_STATES = ('upload_pending', 'failed')
CHILD_STATES = ('failed',)


def candidate(store, parent):
    children = store.query(
        "SELECT * FROM jobs WHERE source_platform='machine_web' AND source_id=? AND job_type='tweet_machine_publish' "
        "ORDER BY created_at DESC", (parent['job_id'],))
    return children[0] if children else None


def assess(settings, store, parent, reset_attempts=False):
    now = time.time()
    result = {'parent_job_id': parent['job_id'], 'source_id': parent['source_id'],
              'parent_state': parent['state'], 'problems': [], 'actions': []}
    if parent['state'] not in PARENT_STATES:
        result['problems'].append('PARENT_NOT_REQUEUEABLE')
        return result
    child = candidate(store, parent)
    if not child:
        result['problems'].append('PUBLISH_CHILD_MISSING')
        return result
    result['child_job_id'] = child['job_id']
    result['child_state'] = child['state']
    result['child_attempts'] = child['attempts']
    result['child_error'] = child['error_code']
    if child['state'] not in CHILD_STATES:
        result['problems'].append('CHILD_NOT_IN_FAILED_STATE')
    if child['attempts'] >= settings.config['max_attempts']:
        if reset_attempts:
            result['actions'].append('reset_child_attempts')
        else:
            result['problems'].append('CHILD_ATTEMPTS_EXHAUSTED')
    pubs = store.query("SELECT state,external_id FROM publications WHERE source_id=? AND state<>'rejected'",
                       (parent['source_id'],))
    if pubs:
        result['problems'].append('PUBLICATION_EXISTS')
        result['publication'] = [dict(row) for row in pubs]
    auth = owner_backfill_authorization(settings, parent['source_id'])
    result['owner_backfill'] = bool(auth)
    if reset_attempts and not auth:
        result['problems'].append('ATTEMPTS_RESET_REQUIRES_AUTHORIZATION')
    if auth:
        result['owner_backfill_reference'] = auth['reference']
        result['owner_backfill_not_after'] = auth['not_after']
    tombstoned = bool(store.query("SELECT 1 FROM source_tombstones WHERE source_platform='x' AND source_id=?",
                                  (parent['source_id'],)))
    result['tombstoned'] = tombstoned
    window_ok = True
    try:
        from datetime import datetime
        posted = datetime.fromisoformat(parent['payload']['post']['created_at'].replace('Z', '+00:00')).timestamp()
        window_ok = 0 <= now - posted <= 7200
        result['age_seconds'] = round(now - posted)
    except (KeyError, ValueError, TypeError):
        result['problems'].append('SOURCE_TIMESTAMP_INVALID')
    if not window_ok and not auth:
        result['problems'].append('WINDOW_EXPIRED_WITHOUT_AUTHORIZATION')
    if tombstoned and not auth:
        result['problems'].append('TOMBSTONED_WITHOUT_AUTHORIZATION')
    if parent['state'] == 'failed':
        if parent['error_code'] != 'OWNER_BACKLOG_CLEARED':
            result['problems'].append('PARENT_FAILED_FOR_OTHER_REASON')
        if not auth:
            result['problems'].append('PARENT_RESTORE_REQUIRES_AUTHORIZATION')
        else:
            result['actions'].append('restore_parent_upload_pending')
    result['actions'].append('rebind_child_manifest_sha256')
    result['actions'].append('reset_child_pending')
    return result


def apply(settings, store, parent, child, reason, now, reset_attempts=False):
    with store.transaction() as db:
        if parent['state'] == 'failed':
            changed = db.execute(
                "UPDATE jobs SET state='upload_pending',stage='published_pending',error_code=NULL,updated_at=? "
                "WHERE job_id=? AND state='failed'", (now, parent['job_id'])).rowcount
            if changed != 1:
                raise SystemExit('REQUEUE_PARENT_UPDATE_CONFLICT')
        result = parent['result']
        if isinstance(result, str):
            result = json.loads(result)
        manifest_sha = result['machine_web']['manifest_sha256']
        payload = child['payload']
        if isinstance(payload, str):
            payload = json.loads(payload)
        else:
            payload = dict(payload)
        payload['manifest_sha256'] = manifest_sha
        new_attempts = 0 if reset_attempts else child['attempts']
        changed = db.execute(
            "UPDATE jobs SET payload=?,state='pending',error_code=NULL,stage='discovered',next_run=?,updated_at=?,"
            "lease_owner=NULL,lease_until=NULL,attempts=? WHERE job_id=? AND state='failed' AND attempts=?",
            (json.dumps(payload, ensure_ascii=False), now, now, new_attempts,
             child['job_id'], child['attempts'])).rowcount
        if changed != 1:
            raise SystemExit('REQUEUE_CHILD_UPDATE_CONFLICT')
        audit = {'reason': reason, 'parent': parent['job_id'], 'source_id': parent['source_id'],
                 'manifest_sha256': manifest_sha}
        if reset_attempts:
            audit['attempts_reset_from'] = child['attempts']
        else:
            audit['attempts_preserved'] = child['attempts']
        db.execute("INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)",
                   (now, 'owner', VERSION, child['job_id'], json.dumps(audit, ensure_ascii=False)))
    return {'requeued': child['job_id'], 'parent': parent['job_id'], 'attempts': new_attempts}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--parent', action='append', default=[], help='tweet_process job id (repeatable)')
    parser.add_argument('--list', action='store_true', help='list requeueable parents and exit')
    parser.add_argument('--apply', action='store_true', help='perform the requeue (default is dry-run)')
    parser.add_argument('--reason', default='', help='required with --apply')
    parser.add_argument('--reset-attempts', action='store_true',
                        help='owner-authorized reset of exhausted child attempts (requires authorization)')
    args = parser.parse_args()
    settings = Settings.load()
    store = Store(settings.db_path)
    if args.list:
        rows = store.query("SELECT * FROM jobs WHERE job_type='tweet_process' "
                           "AND state IN ('upload_pending','failed') ORDER BY updated_at DESC")
        plans = [assess(settings, store, row, args.reset_attempts) for row in rows]
        print(json.dumps({'version': VERSION, 'plans': plans}, ensure_ascii=False, indent=1))
        return 0
    if args.apply and not args.reason.strip():
        raise SystemExit('REASON_REQUIRED')
    if not args.parent:
        raise SystemExit('PARENT_REQUIRED')
    now = time.time()
    results = []
    for parent_id in args.parent:
        parent = store.get(parent_id)
        if not parent or parent['job_type'] != 'tweet_process':
            raise SystemExit('PARENT_NOT_FOUND:' + parent_id)
        plan = assess(settings, store, parent, args.reset_attempts)
        if plan['problems']:
            raise SystemExit('REFUSED:' + parent_id + ':' + ','.join(plan['problems']))
        if not args.apply:
            results.append({'dry_run': True, **plan})
            continue
        child = candidate(store, parent)
        results.append(apply(settings, store, parent, child, args.reason.strip(), now, args.reset_attempts))
    print(json.dumps({'version': VERSION, 'applied': args.apply, 'results': results}, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
