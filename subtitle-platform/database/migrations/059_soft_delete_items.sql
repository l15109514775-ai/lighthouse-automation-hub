-- 059: per-item soft delete for subtitle requests, cloud jobs and auto-approval policy.
-- Phase 1 only: no rows are hard-deleted. The 30-day sweep is added after UI verification.
begin;
set local lock_timeout='5s';

alter table public.st_cloud_jobs
  add column if not exists deleted_at timestamptz,
  add column if not exists deleted_by uuid references public.st_members(id);
alter table public.st_subtitle_auto_policy
  add column if not exists deleted_at timestamptz,
  add column if not exists deleted_by uuid references public.st_members(id);
create index if not exists st_cloud_jobs_deleted_at_idx on public.st_cloud_jobs(deleted_at) where deleted_at is not null;
create index if not exists st_subtitle_auto_policy_deleted_at_idx on public.st_subtitle_auto_policy(deleted_at) where deleted_at is not null;

create or replace function public.st_subtitle_auto_policy_set_deleted(p_member uuid,p_deleted boolean)
returns jsonb language plpgsql security definer set search_path=pg_catalog as $$
declare p public.st_subtitle_auto_policy;
begin
 if not public.st_owner() then raise exception '只有组长可以删除或恢复自动审批白名单' using errcode='42501'; end if;
 if p_member is null or p_deleted is null then raise exception '白名单参数无效'; end if;
 select * into p from public.st_subtitle_auto_policy where member_id=p_member for update;
 if p.member_id is null then raise exception '白名单记录不存在'; end if;
 if (p.deleted_at is not null)=p_deleted then
  return jsonb_build_object('member_id',p_member,'deleted',p_deleted);
 end if;
 update public.st_subtitle_auto_policy
  set deleted_at=case when p_deleted then clock_timestamp() end,
      deleted_by=case when p_deleted then public.st_actor() end,
      updated_at=clock_timestamp()
  where member_id=p_member;
 insert into public.st_audit_events(actor_subject,database_role,entity_table,entity_id,operation,metadata)
 values(auth.uid(),session_user,'st_subtitle_auto_policy',p_member::text,'UPDATE',
  jsonb_build_object('action',case when p_deleted then 'soft_delete' else 'restore' end));
 return jsonb_build_object('member_id',p_member,'deleted',p_deleted);
end; $$;

create or replace function public.st_subtitle_auto_policy_list()
returns jsonb language plpgsql stable security definer set search_path=pg_catalog as $$
declare result jsonb;
begin
 if not public.st_owner() then raise exception '只有组长可以查看自动审批白名单' using errcode='42501'; end if;
 select jsonb_build_object(
  'limits',(select to_jsonb(l) from public.st_subtitle_auto_limits l where id=1),
  'members',coalesce((
   select jsonb_agg(jsonb_build_object(
    'member_id',m.id,'name',m.display_name,
    'enabled',coalesce(p.enabled,false),
    'daily_jobs',coalesce(p.daily_jobs,2),
    'daily_seconds',coalesce(p.daily_seconds,2400)) order by m.display_name)
   from public.st_members m
   left join public.st_subtitle_auto_policy p on p.member_id=m.id and p.deleted_at is null
   where m.state='active'
    and exists(select 1 from public.st_member_roles r where r.member_id=m.id and r.role_code<>'bot')
  ),'[]'::jsonb),
  'deleted',coalesce((
   select jsonb_agg(jsonb_build_object(
    'member_id',p.member_id,'name',m.display_name,
    'enabled',p.enabled,'deleted_at',p.deleted_at) order by p.deleted_at desc)
   from public.st_subtitle_auto_policy p
   join public.st_members m on m.id=p.member_id
   where p.deleted_at is not null
  ),'[]'::jsonb)
 ) into result;
 return result;
end; $$;

create or replace function public.st_cloud_job_set_deleted(p_job uuid,p_deleted boolean)
returns jsonb language plpgsql security definer set search_path=pg_catalog as $$
declare j public.st_cloud_jobs;
begin
 if p_job is null or p_deleted is null then raise exception '云任务参数无效'; end if;
 select * into j from public.st_cloud_jobs where id=p_job for update;
 if j.id is null then raise exception '云任务不存在'; end if;
 if not (public.st_owner() or public.st_project_manage(j.project_id)) then
  raise exception '只有组长或项目负责人可以删除或恢复云任务' using errcode='42501';
 end if;
 if p_deleted and (j.state not in ('succeeded','failed','cancelled')
  or (j.lease_until is not null and j.lease_until>now())) then
  raise exception '只能删除已结束且未在执行的云任务';
 end if;
 if (j.deleted_at is not null)=p_deleted then
  return jsonb_build_object('job_id',p_job,'deleted',p_deleted);
 end if;
 update public.st_cloud_jobs
  set deleted_at=case when p_deleted then clock_timestamp() end,
      deleted_by=case when p_deleted then public.st_actor() end,
      updated_at=clock_timestamp()
  where id=p_job;
 insert into public.st_audit_events(actor_subject,database_role,entity_table,entity_id,operation,metadata)
 values(auth.uid(),session_user,'st_cloud_jobs',p_job::text,'UPDATE',
  jsonb_build_object('action',case when p_deleted then 'soft_delete' else 'restore' end));
 return jsonb_build_object('job_id',p_job,'deleted',p_deleted);
end; $$;

create or replace function public.st_cloud_jobs_view(p_project uuid default null)
returns jsonb language plpgsql stable security definer set search_path=pg_catalog as $$
begin
 if public.st_actor() is null then raise exception '请先登录正式成员账号'; end if;
 return jsonb_build_object(
  'workers',case when public.st_owner() then coalesce((select jsonb_agg(jsonb_build_object('id',id,'label',label,'active',active,'expires_at',expires_at,'max_jobs',max_jobs,'claimed_jobs',claimed_jobs,'available',public.st_worker_valid(id) and claimed_jobs<max_jobs) order by created_at desc) from public.st_cloud_workers where deleted_at is null),'[]'::jsonb) else '[]'::jsonb end,
  'deleted_workers',case when public.st_owner() then coalesce((select jsonb_agg(jsonb_build_object('id',id,'label',label,'active',active,'deleted_at',deleted_at) order by deleted_at desc,id) from public.st_cloud_workers where deleted_at is not null),'[]'::jsonb) else '[]'::jsonb end,
  'deleted_jobs',coalesce((select jsonb_agg(jsonb_build_object('id',j.id,'project_id',j.project_id,'project_title',p.title,'state',j.state,'deleted_at',j.deleted_at) order by j.deleted_at desc) from public.st_cloud_jobs j join public.st_projects p on p.id=j.project_id where j.deleted_at is not null and (public.st_owner() or public.st_project_manage(j.project_id))),'[]'::jsonb),
  'jobs',coalesce((select jsonb_agg(to_jsonb(v) order by v.created_at desc) from (
   select j.id,j.project_id,j.task_id,j.state,j.stage,j.progress,j.file_name,j.byte_size,j.sha256,j.max_video_seconds,j.max_text_requests,j.max_secondary_requests,j.source_expires_at,j.delivery_id,j.created_at,j.updated_at,p.title as project_title
   from public.st_cloud_jobs j join public.st_projects p on p.id=j.project_id
   where j.deleted_at is null and public.st_project_access(j.project_id) and (p_project is null or j.project_id=p_project)
   order by j.created_at desc limit 100
  ) v),'[]'::jsonb));
end; $$;

revoke all on function public.st_subtitle_auto_policy_set_deleted(uuid,boolean),public.st_cloud_job_set_deleted(uuid,boolean) from public,anon,authenticated,service_role;
grant execute on function public.st_subtitle_auto_policy_set_deleted(uuid,boolean),public.st_cloud_job_set_deleted(uuid,boolean) to authenticated;
insert into public.st_schema_migrations(version) values('059_soft_delete_items') on conflict do nothing;
commit;
