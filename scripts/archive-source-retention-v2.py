"""App-UID local archive index/plan/apply. No network, browser or cloud delete."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.hub.archive_source_retention_v2 import ArchiveSourceRetentionV2,_atomic,_path,_uuid
from services.hub.config import Settings
from services.hub.db import Store


def main():
    if os.name!='posix' or os.getuid()!=10001:raise RuntimeError('APPLICATION_UID_10001_REQUIRED')
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--register-before-prune',metavar='RESTORATION_REPORT')
    mode.add_argument('--verify-registration',metavar='RESTORATION_REPORT')
    mode.add_argument('--authorize-file',metavar='OWNER_INSTRUCTION_JSON')
    mode.add_argument('--plan-file',metavar='EXPLICIT_FILES_REQUEST_JSON')
    mode.add_argument('--apply-plan',metavar='SAVED_PLAN_JSON')
    parser.add_argument('--expected-report-sha256')
    args=parser.parse_args();settings=Settings.load();service=ArchiveSourceRetentionV2(settings,Store(settings.db_path))
    if args.register_before_prune:
        path=_path(settings.root,args.register_before_prune)
        if not args.expected_report_sha256 or hashlib.sha256(path.read_bytes()).hexdigest()!=args.expected_report_sha256:
            raise RuntimeError('PINNED_RESTORATION_REPORT_REQUIRED')
        value=service.register_verified_archive(path)
    elif args.verify_registration:
        value=service.verify_registration(args.verify_registration,args.expected_report_sha256)
    elif args.authorize_file:
        file=_path(settings.root,args.authorize_file)
        if file.stat().st_size>16384:raise RuntimeError('OWNER_INSTRUCTION_FILE_LIMIT')
        instruction=json.loads(file.read_bytes())
        value=service.authorize(**instruction)
    elif args.plan_file:
        file=_path(settings.root,args.plan_file)
        if file.stat().st_size>1_000_000:raise RuntimeError('EXPLICIT_PLAN_REQUEST_LIMIT')
        request=json.loads(file.read_bytes());value=service.plan(request['files'],authorization_id=request['authorization_id'])
        directory=service.folder/'plans';_path(settings.root,directory);directory.mkdir(mode=0o700,exist_ok=True)
        plan_file=directory/(value['plan_id']+'.json');_atomic(plan_file,value)
        value={'state':'dry_run_prepared','plan_file':str(plan_file),'plan_sha256':value['plan_sha256'],
            'candidate_files':len(value['candidates']),'rejected':value['rejected'],'source_files_deleted':0,'remote_requests':0}
    else:
        file=_path(settings.root,args.apply_plan)
        if file.parent!=service.folder/'plans' or not _uuid(file.stem) or file.stat().st_size>20_000_000:
            raise RuntimeError('EXACT_SAVED_PLAN_REQUIRED')
        value=service.apply(json.loads(file.read_bytes()))
    print(json.dumps(value,ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())
