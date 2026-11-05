"""Local operator: plan first, then explicitly admit one independent v2 pilot."""
import argparse,json,os,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.errors import HubError
from services.hub.maintenance import Maintenance
from services.hub.youtube_download_v2 import YouTubeHighestDownload
from services.hub.youtube_highest_plan_v2 import VIDEO_ID,OPERATING_NOT_BEFORE
from adapters.ytdlp_highest_v2 import preflight_budget

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');parser.add_argument('--policy-sha256');args=parser.parse_args()
    if os.name!='posix' or os.getuid() not in (0,10001):raise HubError('LOCAL_OPERATOR_RUNTIME_REQUIRED')
    s=Settings.load()
    if not s.db_path.is_file():raise HubError('EXISTING_HUB_DATABASE_REQUIRED')
    db=Store(s.db_path);service=YouTubeHighestDownload(s,db);policy=service.policy()
    rows=db.query('SELECT * FROM youtube_videos WHERE video_id=?',(VIDEO_ID,))
    if len(rows)!=1:raise HubError('YOUTUBE_HIGHEST_OFFICIAL_SOURCE_REQUIRED',review=True)
    row=rows[0];payload={k:row[k] for k in ('video_id','channel_id','title','published_at','duration','url')}
    payload.update(source_api=row['source_api'],raw=json.loads(row['metadata']));service.metadata(payload,policy)
    free=Maintenance(s,db).check_disk(policy['source_plan']['required_working_bytes'])
    binding={'transport_budget':{'provider':'youtube_download_http','daily_requests':2500,'monthly_requests':30000,'history_reset':False}}
    preflight_budget(db,binding,policy['source_plan']['max_http_requests'])
    job=None
    if args.apply:
        if args.policy_sha256!=policy['_sha256']:raise HubError('YOUTUBE_HIGHEST_POLICY_PLAN_CHANGED',review=True)
        job=service.enqueue_pilot(payload);db.audit('operator:local-ssh','youtube.highest.queue',job,'new_version_no_legacy_reset')
    print(json.dumps({'mode':'apply' if args.apply else 'plan','job_id':job,'video_id':VIDEO_ID,'policy_sha256':policy['_sha256'],
        'source_plan':policy['source_plan'],'free_bytes':free,'disk_floor_bytes':15_000_000_000,
        'operating_not_before':OPERATING_NOT_BEFORE,'external_requests':0,'legacy_job_reset':False,'publication_authorized':False}))

if __name__=='__main__':
    try:main()
    except HubError as exc:
        print(json.dumps({'error_code':exc.code,'new_download_started':False,'legacy_job_reset':False}));raise SystemExit(2) from None
