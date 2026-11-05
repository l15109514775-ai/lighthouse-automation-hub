"""Local SSH/Docker operator CLI; policy permits at most one acceptance ID."""
import argparse,json,os,re,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from services.hub.config import Settings
from services.hub.db import Store
from services.hub.errors import HubError
from services.hub.youtube_download import YouTubeDownload

parser=argparse.ArgumentParser()
parser.add_argument('video_id')
parser.add_argument('--acceptance',action='store_true')
args=parser.parse_args()
try:
    if os.name!='posix' or os.getuid() not in (0,10001):raise HubError('LOCAL_OPERATOR_RUNTIME_REQUIRED')
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}',args.video_id):raise HubError('YOUTUBE_DOWNLOAD_IDENTITY_INVALID')
    s=Settings.load();db=Store(s.db_path)
    rows=db.query('SELECT * FROM youtube_videos WHERE video_id=?',(args.video_id,))
    if len(rows)!=1:raise HubError('YOUTUBE_DOWNLOAD_OFFICIAL_BINDING_REQUIRED')
    row=rows[0]
    payload={k:row[k] for k in ('video_id','channel_id','title','published_at','duration','url')}
    payload.update(source_api=row['source_api'],raw=json.loads(row['metadata']))
    service=YouTubeDownload(s,db)
    job_id=service.enqueue_acceptance(payload) if args.acceptance else service.enqueue_if_eligible(payload)
    if job_id:db.audit('operator:local-ssh','youtube.download.queue',job_id,'queued_or_existing')
    print(json.dumps({'job_id':job_id,'video_id':args.video_id,'acceptance':args.acceptance,
                      'download_only':True,'publication_authorized':False}))
except HubError as exc:
    print(json.dumps({'error_code':exc.code,'download_started':False,'publication_authorized':False}))
    raise SystemExit(2) from None
