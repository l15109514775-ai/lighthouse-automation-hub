-- 055: archive approved requests after 30 days (soft delete, restorable).
BEGIN;
SET LOCAL lock_timeout='5s';

CREATE OR REPLACE FUNCTION public.st_subtitle_request_set_deleted(p_request uuid,p_deleted boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r public.st_subtitle_requests; j public.st_cloud_jobs;
BEGIN
 SELECT * INTO r FROM public.st_subtitle_requests WHERE id=p_request FOR UPDATE;
 IF NOT public.st_formal_member() OR r.id IS NULL OR (r.requested_by IS DISTINCT FROM public.st_actor() AND NOT public.st_owner()) THEN
  RAISE EXCEPTION '无此申请权限' USING ERRCODE='42501';
 END IF;
 IF p_deleted IS NULL THEN RAISE EXCEPTION '删除参数错误'; END IF;
 IF r.state='approved' AND r.job_id IS NOT NULL THEN
  IF NOT public.st_owner() THEN RAISE EXCEPTION '已批准申请仅组长可删除或恢复' USING ERRCODE='42501'; END IF;
  SELECT * INTO j FROM public.st_cloud_jobs WHERE id=r.job_id FOR UPDATE;
  IF j.id IS NULL OR j.state NOT IN ('cancelled','failed','succeeded')
   OR (j.lease_until IS NOT NULL AND j.lease_until>now())
  THEN RAISE EXCEPTION '请等待云任务结束后再删除或恢复申请'; END IF;
 ELSIF r.state<>'withdrawn' OR r.job_id IS NOT NULL THEN
  RAISE EXCEPTION '仅已撤回申请或已结束的已批准申请可删除';
 END IF;
 IF (r.deleted_at IS NOT NULL)=p_deleted THEN RETURN; END IF;
 UPDATE public.st_subtitle_requests SET deleted_at=CASE WHEN p_deleted THEN clock_timestamp() END,
  deleted_by=CASE WHEN p_deleted THEN public.st_actor() END,updated_at=clock_timestamp() WHERE id=r.id;
END; $$;

CREATE OR REPLACE FUNCTION public.st_subtitle_requests_archive(p_days integer DEFAULT 30)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE bot uuid; changed integer;
BEGIN
 IF p_days IS NULL OR p_days NOT BETWEEN 1 AND 365 THEN RAISE EXCEPTION '归档天数无效'; END IF;
 SELECT id INTO bot FROM public.st_members WHERE auth_subject='bot:subtitle-auto' AND state='active';
 UPDATE public.st_subtitle_requests r SET deleted_at=now(),deleted_by=bot,updated_at=now()
 WHERE r.state='approved' AND r.deleted_at IS NULL AND r.job_id IS NOT NULL
  AND r.created_at < now()-make_interval(days=>p_days)
  AND EXISTS(SELECT 1 FROM public.st_cloud_jobs j WHERE j.id=r.job_id AND j.state IN ('succeeded','failed','cancelled'))
  AND NOT EXISTS(SELECT 1 FROM public.st_cloud_jobs j WHERE j.id=r.job_id AND j.lease_until IS NOT NULL AND j.lease_until>now());
 GET DIAGNOSTICS changed = ROW_COUNT;
 INSERT INTO public.st_audit_events(actor_subject,database_role,entity_table,entity_id,operation,metadata)
 VALUES('system:archive',session_user,'st_subtitle_requests','bulk','UPDATE',
  jsonb_build_object('action','archive_approved_requests','days',p_days,'archived',changed));
 RETURN jsonb_build_object('archived',changed,'days',p_days);
END; $$;

REVOKE ALL ON FUNCTION public.st_subtitle_requests_archive(integer) FROM PUBLIC,anon,authenticated,service_role;
INSERT INTO public.st_schema_migrations(version) VALUES('055_archive_approved_requests') ON CONFLICT DO NOTHING;
COMMIT;
