"""Bounded completed-video maintenance; publication and evidence stay intact.

No API client, browser, recursive delete or file payload is accepted. The
destructive core independently requires its real isolated-test release.
"""
from datetime import datetime,timezone
import json,sqlite3,uuid

from .archive_source_retention_v2 import _atomic,_path
from .errors import HubError
from .locks import resource_guard
from .video_source_retention_v2 import VideoSourceTracksRetentionV2,PUBLISHED


class VideoRetentionCompletedSchedulerV2:
    def __init__(self,settings,store,*,retention=None):
        self.settings,self.store=settings,store
        self.retention=retention or VideoSourceTracksRetentionV2(settings,store)

    def run_once(self,authorization_id,*,apply=False,max_jobs=1,refresh_public_observation=None,report_accumulator=None):
        if type(apply) is not bool or type(max_jobs) is not int or not 1<=max_jobs<=3:
            raise HubError('VIDEO_RETENTION_SCHEDULER_ARGUMENT_REJECTED',review=True)
        service=self.retention;authorization=service._authorization(authorization_id)
        if authorization['scope']!=PUBLISHED:
            raise HubError('VIDEO_RETENTION_TRACKS_REQUIRE_PUBLIC_COMPLETION',review=True)
        service.initialize();processed=[];retained=[];seen=set()
        # Optional report observer only: no callback, admission or state change.
        # Preserve exact successful/retained entries if a caller's bounded
        # candidate window interrupts before this method returns its report.
        if report_accumulator is not None:
            if type(report_accumulator) is not dict or report_accumulator:
                raise HubError('VIDEO_RETENTION_REPORT_ACCUMULATOR_REJECTED',review=True)
            report_accumulator.update(processed=processed,retained=retained)
        with resource_guard(service.folder/'scheduler.lock'):
            # A crash after an unlink is reconciled before considering new jobs.
            # Existing durable plans keep their exact original UUID and binding.
            with sqlite3.connect(service.path) as db:
                pending=list(db.execute("SELECT record FROM operations WHERE state!='completed' ORDER BY rowid"))
            for (raw,) in pending:
                plan=json.loads(raw).get('plan',{})
                if plan.get('cleanup_mode')!='bound_original_and_source_tracks_v1':continue
                ident=plan.get('job_id');seen.add(ident)
                if plan.get('authorization_id')!=authorization_id:
                    retained.append({'job_id':ident,'error_code':'VIDEO_RETENTION_EXISTING_INTENT_DIFFERENT_AUTHORIZATION'});continue
                if len(processed)>=max_jobs:break
                try:
                    value=service.apply(plan) if apply else {'state':'durable_cleanup_pending','plan_id':plan['plan_id'],'local_files_deleted':0}
                    processed.append({'job_id':ident,'resumed':True,'result':value})
                except HubError as exc:retained.append({'job_id':ident,'error_code':exc.code})
            jobs=self.store.query("""SELECT job_id,result FROM jobs WHERE state='completed' AND queue='video'
                AND job_type IN ('replay_highest_video_publish_v2','replay_natural_video_publish_v3') ORDER BY created_at,job_id""")
            for row in jobs:
                ident=row['job_id']
                if ident in seen or json.loads(row['result']).get('local_original_retention_v2'):continue
                if len(processed)>=max_jobs:break
                try:
                    refresh=(lambda:refresh_public_observation(ident)) if refresh_public_observation is not None else None
                    plan=service.plan(ident,authorization_id=authorization_id,refresh_public_observation=refresh)
                    directory=_path(service.root,service.folder/'plans');directory.mkdir(mode=0o700,exist_ok=True)
                    file=directory/(plan['plan_id']+'.json');_atomic(file,plan)
                    value=service.apply(plan) if apply else {'state':'dry_run_prepared','plan_id':plan['plan_id'],
                        'plan_file':str(file),'candidates':plan['candidates'],'local_files_deleted':0}
                    processed.append({'job_id':ident,'resumed':False,'result':value})
                except HubError as exc:retained.append({'job_id':ident,'error_code':exc.code})
            report={'version':'completed-video-retention-scheduler-v2','operation_id':str(uuid.uuid4()),
                'created_at':datetime.now(timezone.utc).isoformat(),'apply':apply,'max_jobs':max_jobs,
                'authorization_id':authorization_id,'processed':processed,'retained':retained,
                'external_requests':0 if refresh_public_observation is None else 'existing_read_only_lookup_callback',
                'publication_records_removed':False,'source_id_records_removed':False,'remote_video_deleted':False}
            directory=_path(service.root,service.folder/'reports');directory.mkdir(mode=0o700,exist_ok=True)
            file=directory/(report['operation_id']+'.json');_atomic(file,report)
            return {**report,'report_file':str(file)}
