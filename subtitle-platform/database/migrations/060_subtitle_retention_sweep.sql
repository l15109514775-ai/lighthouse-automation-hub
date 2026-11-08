-- 060: 30-day hard-delete sweep for soft-deleted subtitle items.
-- Invoked by the existing QQ cleanup worker; never runs from an active request.
begin;
set local lock_timeout='5s';

create or replace function public.st_subtitle_retention_sweep(p_days integer default 30)
returns jsonb language plpgsql security definer set search_path=pg_catalog as $$
declare
  cutoff timestamptz;
  r record;
  j record;
  request_count integer:=0;
  job_count integer:=0;
  policy_count integer:=0;
begin
  if p_days is null or p_days not between 1 and 365 then
    raise exception '保留天数无效';
  end if;
  cutoff:=now()-make_interval(days=>p_days);

  delete from public.st_subtitle_auto_policy
   where deleted_at is not null and deleted_at<=cutoff;
  get diagnostics policy_count=row_count;

  perform set_config('subtitle.allow_request_purge','on',true);
  for r in select id,job_id from public.st_subtitle_requests
           where deleted_at is not null and deleted_at<=cutoff loop
    if r.job_id is not null and exists(
      select 1 from public.st_cloud_jobs j where j.id=r.job_id and j.deleted_at is null
    ) then
      continue;
    end if;
    delete from public.st_subtitle_notifications where request_id=r.id;
    delete from public.st_subtitle_auto_runs where request_id=r.id;
    delete from public.st_source_retention where source_kind='request' and source_id=r.id;
    delete from public.st_subtitle_requests where id=r.id;
    request_count:=request_count+1;
  end loop;
  perform set_config('subtitle.allow_request_purge','off',true);

  for j in select id from public.st_cloud_jobs
           where deleted_at is not null and deleted_at<=cutoff loop
    if exists(select 1 from public.st_subtitle_requests r where r.job_id=j.id and r.deleted_at is null) then
      continue;
    end if;
    delete from public.st_subtitle_auto_runs where job_id=j.id;
    delete from public.st_source_retention where source_kind='job' and source_id=j.id;
    delete from public.st_cloud_jobs where id=j.id;
    job_count:=job_count+1;
  end loop;

  insert into public.st_audit_events(actor_subject,database_role,entity_table,entity_id,operation,metadata)
  values('system:retention',session_user,'st_subtitle_retention','bulk','DELETE',
    jsonb_build_object('days',p_days,'requests',request_count,'jobs',job_count,'policies',policy_count));
  return jsonb_build_object('days',p_days,'requests',request_count,'jobs',job_count,'policies',policy_count);
end; $$;

create or replace function public.st_subtitle_retention_worker(p_key text,p_sender text,p_days integer default 30)
returns jsonb language plpgsql security definer set search_path=pg_catalog as $$
declare g uuid;
begin
  g:=public.st_qq_notification_access(p_key,p_sender);
  if g is null then raise exception 'Retention worker denied' using errcode='42501'; end if;
  return public.st_subtitle_retention_sweep(p_days);
end; $$;

revoke all on function public.st_subtitle_retention_sweep(integer),public.st_subtitle_retention_worker(text,text,integer) from public,anon,authenticated,service_role;
grant execute on function public.st_subtitle_retention_worker(text,text,integer) to anon,authenticated;
insert into public.st_schema_migrations(version) values('060_subtitle_retention_sweep') on conflict do nothing;
commit;
