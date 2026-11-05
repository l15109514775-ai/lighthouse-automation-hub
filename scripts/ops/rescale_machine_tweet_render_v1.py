#!/usr/bin/env python3
"""Downscale an already-rendered machine tweet image and rebuild its evidence.

Used when a render was created before the byte/size guard existed (or when the
renderer could not fit the image). The original attempt directory stays
untouched: a new attempt directory holds the scaled PNG, an updated report and
an updated manifest. The parent job result and the failed publish child are
re-bound to the new manifest so the normal requeue tools can take over.

Dry-run by default. ``--apply`` requires a reason and refuses when a
publication already exists or the parent/child are in an unexpected state.

Run inside the tweet worker (it owns the media tree, ffmpeg and the database):

    docker exec -u 10001 -e HUB_ROOT=/opt/lighthouse-hub \
      -e HUB_CONFIG=/opt/lighthouse-hub/config/tweet-runtime-v4.toml \
      -e PYTHONPATH=/opt/lighthouse-hub -w /opt/lighthouse-hub \
      lighthouse-hub-tweet-worker-1 \
      python scripts/ops/rescale_machine_tweet_render_v1.py --parent <job> [--apply --reason ...]
"""
import argparse
import hashlib
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.hub.config import Settings  # noqa: E402
from services.hub.db import Store  # noqa: E402
from services.hub.machine_routes import machine_service  # noqa: E402

VERSION = 'rescale-machine-tweet-render-v1'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def png_size(path):
    with open(path, 'rb') as fh:
        head = fh.read(24)
    if head[1:4] != b'PNG':
        raise SystemExit('PNG_REQUIRED')
    return int.from_bytes(head[16:20], 'big'), int.from_bytes(head[20:24], 'big')


def scale_to(source, target, width, *, height=None):
    vf = f'scale={width}:{height}' if height else f'scale={width}:-2'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(source), '-vf', vf,
                    '-frames:v', '1', str(target)], check=True)
    target.chmod(0o644)


def plan(manifest, max_bytes, max_height):
    render = manifest['render']
    png = Path(render['png'])
    report = report_of(manifest)
    width, height = png_size(png)
    bytes_now = png.stat().st_size
    info = {'png': str(png), 'bytes': bytes_now, 'width': width, 'height': height,
            'max_bytes': max_bytes, 'max_height': max_height,
            'needs_scale': bytes_now > max_bytes or height > max_height}
    if info['needs_scale']:
        target_h = min(height, max_height)
        target_w = max(1, round(width * target_h / height))
        if target_w > 1600:
            target_h = max(1, round(target_h * 1600 / target_w))
            target_w = 1600
        info.update(target_width=target_w, target_height=target_h)
    return info, report


def report_of(manifest):
    return json.loads(Path(manifest['render']['report_file']).read_text('utf-8'))


def build(manifest, old_dir, max_bytes, max_height, reason):
    render = dict(manifest['render'])
    old_png = Path(render['png'])
    old_report = report_of(manifest)
    width, height = png_size(old_png)
    base = old_dir.parent
    new_dir = base / str(uuid.uuid4())
    new_dir.mkdir(parents=True, mode=0o700)
    for name in ('input.json', 'ticket.json'):
        (new_dir / name).write_bytes((old_dir / name).read_bytes())

    target_h = min(height, max_height)
    target_w = max(1, round(width * target_h / height))
    if target_w > 1600:
        target_h = max(1, round(target_h * 1600 / target_w))
        target_w = 1600
    new_png = new_dir / (old_png.stem + '.scaled.png')
    scale_to(old_png, new_png, target_w)
    for _ in range(4):
        if new_png.stat().st_size <= max_bytes or target_w <= 900:
            break
        factor = min(0.95, (max_bytes / new_png.stat().st_size) ** 0.5 * 0.95)
        target_w = max(900, round(target_w * factor))
        scale_to(old_png, new_png, target_w)
    if new_png.stat().st_size > max_bytes:
        raise SystemExit('STILL_ABOVE_BYTE_BUDGET')

    new_w, new_h = png_size(new_png)
    report = dict(old_report)
    old_png_info = report.get('png') or {}
    report['png'] = {'file': str(new_png), 'sha256': sha(new_png), 'bytes': new_png.stat().st_size,
                     'width': new_w, 'height': new_h}
    report['normalization'] = {
        'from': {'bytes': old_png_info.get('bytes'), 'width': old_png_info.get('width'),
                 'height': old_png_info.get('height')},
        'to': {'bytes': report['png']['bytes'], 'width': new_w, 'height': new_h},
        'tool': 'ffmpeg',
        'reference': reason,
    }
    report_file = new_dir / 'machine-render-api-v2-result.json'
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n', 'utf-8')

    manifest = dict(manifest)
    render['png'] = str(new_png)
    render['sha256'] = sha(new_png)
    render['report'] = report
    render['report_file'] = str(report_file)
    render['report_sha256'] = sha(report_file)
    render['input_file'] = str(new_dir / 'input.json')
    render['input_sha256'] = sha(new_dir / 'input.json')
    render['ticket_file'] = str(new_dir / 'ticket.json')
    render['ticket_sha256'] = sha(new_dir / 'ticket.json')
    manifest['render'] = render
    manifest_file = new_dir / 'manifest.json'
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + '\n', 'utf-8')
    return new_png, report, manifest, manifest_file, new_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--parent', required=True)
    parser.add_argument('--max-bytes', type=int, default=3145728)
    parser.add_argument('--max-height', type=int, default=4096)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--reason', default='')
    args = parser.parse_args()
    if args.apply and not args.reason.strip():
        raise SystemExit('REASON_REQUIRED')
    settings = Settings.load()
    store = Store(settings.db_path)
    parent = store.get(args.parent)
    if not parent or parent['job_type'] != 'tweet_process':
        raise SystemExit('PARENT_NOT_FOUND')
    entry = parent['result'].get('machine_web') or {}
    if not entry.get('manifest_file'):
        raise SystemExit('MANIFEST_MISSING')
    manifest_path = Path(entry['manifest_file'])
    manifest = json.loads(manifest_path.read_text('utf-8'))
    info, _ = plan(manifest, args.max_bytes, args.max_height)
    if not info['needs_scale']:
        print(json.dumps({'version': VERSION, 'state': 'not_needed', **info}, ensure_ascii=False))
        return 0
    if not args.apply:
        print(json.dumps({'version': VERSION, 'state': 'dry_run', 'parent': args.parent, **info},
                         ensure_ascii=False))
        return 0
    if store.query("SELECT 1 FROM publications WHERE source_id=? AND state<>'rejected'", (parent['source_id'],)):
        raise SystemExit('PUBLICATION_EXISTS')
    children = store.query("SELECT * FROM jobs WHERE source_platform='machine_web' AND source_id=? "
                           "AND job_type='tweet_machine_publish' ORDER BY created_at DESC", (parent['job_id'],))
    if not children:
        raise SystemExit('PUBLISH_CHILD_MISSING')
    child = children[0]
    if child['state'] != 'failed':
        raise SystemExit('CHILD_NOT_FAILED')

    # Keep the immutable attempt layout: copy the evidence the renderer needs.
    old_dir = manifest_path.parent
    new_png, report, new_manifest, manifest_file, new_dir = build(
        manifest, old_dir, args.max_bytes, args.max_height, args.reason)

    previous_result = json.dumps(parent['result'], ensure_ascii=False)
    new_result = json.loads(previous_result)
    new_result['machine_web'] = {**new_result['machine_web'],
                                 'manifest_file': str(manifest_file),
                                 'manifest_sha256': sha(manifest_file),
                                 'artifact_sha256': sha(new_png),
                                 'state': 'ready'}
    now = time.time()
    with store.transaction() as db:
        db.execute('UPDATE jobs SET result=?,updated_at=? WHERE job_id=? AND state=?',
                   (json.dumps(new_result, ensure_ascii=False), now, parent['job_id'], parent['state']))
        payload = dict(child['payload'])
        payload['manifest_sha256'] = new_result['machine_web']['manifest_sha256']
        db.execute("UPDATE jobs SET payload=?,updated_at=? WHERE job_id=? AND state='failed'",
                   (json.dumps(payload, ensure_ascii=False), now, child['job_id']))
        db.execute('INSERT INTO audit(at,principal,action,target,outcome) VALUES(?,?,?,?,?)',
                   (now, 'owner', VERSION, parent['job_id'],
                    json.dumps({'reason': args.reason, 'child': child['job_id'],
                                'from': report['normalization']['from'],
                                'to': report['normalization']['to']}, ensure_ascii=False)))
    svc = machine_service(settings, store, parent['policy_version'])
    try:
        pair = svc._snapshot(parent['job_id'])
        snapshot = pair[-1]
        if isinstance(snapshot, str):
            snapshot = json.loads(snapshot)
        if snapshot['render']['sha256'] != sha(new_png):
            raise ValueError('SNAPSHOT_MISMATCH')
    except Exception as exc:  # restore the previous result on any validation failure
        with store.transaction() as db:
            db.execute('UPDATE jobs SET result=? WHERE job_id=?', (previous_result, parent['job_id']))
        raise SystemExit('VALIDATION_FAILED: ' + type(exc).__name__)
    print(json.dumps({'version': VERSION, 'state': 'applied', 'parent': args.parent,
                      'new_dir': str(new_dir), 'png': report['png'], 'manifest_sha256': sha(manifest_file)},
                     ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
