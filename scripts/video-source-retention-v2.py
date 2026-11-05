"""App-UID video-original retention. Explicit instruction, dry-run, then apply."""
import argparse,json,os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.hub.archive_source_retention_v2 import _atomic,_path,_uuid
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.video_source_retention_v2 import VideoSourceRetentionV2,VideoSourceTracksRetentionV2,PRINCIPAL


def main():
    if os.name!='posix' or os.getuid()!=10001:raise RuntimeError('APPLICATION_UID_10001_REQUIRED')
    parser=argparse.ArgumentParser(description=__doc__);mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--authorize-file');mode.add_argument('--plan-job');mode.add_argument('--apply-plan')
    mode.add_argument('--run-completed',action='store_true')
    parser.add_argument('--authorization-id');parser.add_argument('--include-source-tracks',action='store_true')
    parser.add_argument('--natural-format-ids-v3',action='store_true')
    parser.add_argument('--apply',action='store_true');parser.add_argument('--max-jobs',type=int,default=1)
    args=parser.parse_args();settings=Settings.load()
    if args.apply and not args.run_completed:parser.error('--apply requires --run-completed')
    if args.run_completed and not args.include_source_tracks:parser.error('--run-completed requires --include-source-tracks')
    if args.natural_format_ids_v3 and not args.include_source_tracks:parser.error('--natural-format-ids-v3 requires --include-source-tracks')
    service_type=VideoSourceTracksRetentionV2 if args.include_source_tracks else VideoSourceRetentionV2
    if args.natural_format_ids_v3:
        from services.hub.video_source_retention_natural_formats_v3 import NaturalFormatVideoSourceRetentionV3
        service_type=NaturalFormatVideoSourceRetentionV3
    service=service_type(settings,Store(settings.db_path))
    if args.run_completed:
        from services.hub.video_retention_scheduler_v2 import VideoRetentionCompletedSchedulerV2
        result=VideoRetentionCompletedSchedulerV2(settings,service.store,retention=service).run_once(
            args.authorization_id,apply=args.apply,max_jobs=args.max_jobs)
    elif args.authorize_file:
        file=_path(settings.root,args.authorize_file)
        if file.stat().st_size>16384:raise RuntimeError('OWNER_INSTRUCTION_LIMIT')
        result=service.authorize(PRINCIPAL,**json.loads(file.read_bytes()))
    elif args.plan_job:
        result=service.plan(args.plan_job,authorization_id=args.authorization_id)
        directory=service.folder/'plans';_path(settings.root,directory);directory.mkdir(mode=0o700,exist_ok=True)
        file=directory/(result['plan_id']+'.json');_atomic(file,result)
        result={'state':'dry_run_prepared','plan_file':str(file),'plan_sha256':result['plan_sha256'],
            'candidate':result['candidate'],'candidates':result.get('candidates',[result['candidate']]),'scope':result['scope'],'local_files_deleted':0}
    else:
        file=_path(settings.root,args.apply_plan)
        if file.parent!=service.folder/'plans' or not _uuid(file.stem) or file.stat().st_size>1_000_000:
            raise RuntimeError('EXACT_SAVED_PLAN_REQUIRED')
        result=service.apply(json.loads(file.read_bytes()))
    print(json.dumps(result,ensure_ascii=False));return 0


if __name__=='__main__':raise SystemExit(main())
