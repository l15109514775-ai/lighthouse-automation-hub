"""Default read-only plan; explicitly queue the genuine highest-v2 pilot.

Run with app UID10001 inside the already deployed highest-v2 worker, whose
tested source mounts are retained. No generic-v3 service is imported here.
"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import argparse,json,os,re,stat

PILOT_JOB='d28685f1-03fc-4a12-9d85-5dbf14107f82'
PILOT_VIDEO='9T5tn1WGlJg'
PRINCIPAL='operator:ssh-sudo'


def metadata(settings,source):
    p=source['payload'];raw=p['raw'];snippet=raw['snippet'];channel=p['channel_id']
    matches=[item for item in settings.config['sources'] if item['youtube_channel_id']==channel]
    if len(matches)!=1 or snippet['channelId']!=channel or snippet['title']!=p['title']:raise ValueError('PILOT_SOURCE_METADATA_CHANGED')
    name=matches[0]['name'];handle=matches[0]['youtube_handle'];title=snippet['title'];url=p['url']
    if (source['source_id']!=PILOT_VIDEO or raw['id']!=PILOT_VIDEO or url!='https://www.youtube.com/watch?v='+PILOT_VIDEO or
            not isinstance(title,str) or not 0<len(title)<=200 or not isinstance(name,str) or not 0<len(name)<=20 or
            not isinstance(handle,str) or not re.fullmatch('@[A-Za-z0-9_.-]{1,100}',handle)):raise ValueError('PILOT_SOURCE_METADATA_CHANGED')
    start=datetime.fromisoformat(raw['liveStreamingDetails']['actualStartTime'].replace('Z','+00:00'))
    if start.tzinfo is None or start<datetime.fromisoformat('2026-10-31T16:00:00+00:00'):raise ValueError('PILOT_ACTUAL_START_REQUIRED')
    start=start.astimezone(ZoneInfo('Asia/Shanghai'));prefix='【'+name+'】'+start.strftime('%Y-%m-%d')+'「';suffix='」直播回放'
    units=lambda value:len(value.encode('utf-16-le'))//2
    room=80-units(prefix)-units(suffix)
    if room<2:raise ValueError('PILOT_TITLE_CANNOT_FIT')
    display=title
    if units(title)>room:
        display=''
        for character in title:
            if units(display+character)>room-1:break
            display+=character
        display+='…'
    return {'title':prefix+display+suffix,'description':name+' '+str(start.year)+'年'+str(start.month)+'月'+str(start.day)+
        '日直播回放，日语原声。\n原题：'+title+'\n原视频：'+url+'\n原频道：https://www.youtube.com/'+handle,
        'source':url,'category_label':'娱乐','copyright':2,'tags':[name,'ぶいすぽっ','虚拟UP主','直播回放']}


def plan(service,job_id):
    policy=service.policy();source,asset,receipt=service._source(job_id);service._source_scope(source,policy)
    if source['state']!='completed' or source['source_id']!=PILOT_VIDEO or policy['source_scope']!='pilot_single':raise ValueError('PILOT_NOT_VERIFIED')
    meta=metadata(service.settings,source)
    rows=service.store.query("SELECT job_id,state,attempts,external_publish_id FROM jobs WHERE source_platform='youtube' AND source_id=? AND job_type=? AND policy_version=?",
        (PILOT_VIDEO,service.JOB_TYPE,service.POLICY))
    pubs=service.store.query("SELECT job_id,kind,state,external_id FROM publications WHERE platform='bilibili' AND source_platform='youtube' AND source_id=?",(PILOT_VIDEO,))
    return {'version':'highest-v2-pilot-queue-plan-v1','download_job_id':job_id,'source_id':PILOT_VIDEO,'target_site_uid':policy['target_site_uid'],
        'policy_sha256':policy['_sha256'],'artifact_sha256':asset['sha256'],'file_bytes':asset['bytes'],'metadata':meta,
        'existing_publish_jobs':rows,'existing_publications':pubs,'queue_mutated':False,'external_calls':0,'human_review':False}


def apply(service,job_id,authorization):
    if set(authorization)!={'actual_owner_workflow_response','owner_response_reference'} or any(not isinstance(v,str) or not 0<len(v.strip())<=2048 for v in authorization.values()):
        raise ValueError('ACTUAL_WORKFLOW_AUTHORIZATION_REQUIRED')
    prepared=plan(service,job_id)
    before=service.store.get(prepared['existing_publish_jobs'][0]['job_id']) if prepared['existing_publish_jobs'] else None
    ident=service.queue_pilot(PRINCIPAL,job_id,prepared['metadata'],**authorization)
    current=service.store.get(ident)
    return {**prepared,'publish_job_id':ident,'state':current['state'],'attempts':current['attempts'],
        'queue_mutated':before!=current,'external_calls':0,'publication_performed':False,'attempts_reset':False}


def main():
    from services.hub.config import Settings
    from services.hub.db import Store
    from services.hub.errors import HubError
    from services.hub.locks import resource_guard
    from services.hub.replay_highest_video_v2 import HighestReplayVideoWeb
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--download-job',default=PILOT_JOB)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--authorization-file')
    args=parser.parse_args()
    if os.name!='posix' or os.getuid()!=10001:raise ValueError('APP_UID_10001_REQUIRED')
    if args.download_job!=PILOT_JOB:raise ValueError('ONLY_ACTUAL_D286_PILOT_ALLOWED')
    settings=Settings.load();store=Store(settings.db_path);service=HighestReplayVideoWeb(settings,store)
    try:
        if not args.apply:result=plan(service,args.download_job)
        else:
            file=Path(args.authorization_file or '')
            if not file.is_absolute() or file.resolve()!=file or not file.is_file() or not 0<file.stat().st_size<=8192:raise ValueError('ACTUAL_WORKFLOW_AUTHORIZATION_FILE_REQUIRED')
            for entry in (file,*file.parents):
                st=entry.lstat()
                if stat.S_ISLNK(st.st_mode) or st.st_uid!=0 or st.st_mode&0o022:raise ValueError('ROOT_PROTECTED_AUTHORIZATION_REQUIRED')
            authorization=json.loads(file.read_bytes())
            with resource_guard(settings.root/'data/locks/video'):result=apply(service,args.download_job,authorization)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    except HubError as error:
        print(json.dumps({'error_code':error.code,'queue_mutated':False,'publication_performed':False}));raise SystemExit(2) from None


if __name__=='__main__':main()
