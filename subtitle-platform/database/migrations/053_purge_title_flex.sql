-- 053: accept either the stream title or the displayed title+timestamp on hard delete.
BEGIN;
SET LOCAL lock_timeout='5s';
CREATE OR REPLACE FUNCTION public.st_subtitle_request_purge(p_request uuid,p_confirmation text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r public.st_subtitle_requests; j public.st_cloud_jobs; given text; combined text;
BEGIN
 IF NOT public.st_owner() THEN RAISE EXCEPTION '仅组长可永久删除申请' USING ERRCODE='42501'; END IF;
 SELECT * INTO r FROM public.st_subtitle_requests WHERE id=p_request FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION '申请不存在'; END IF;
 given:=btrim(replace(replace(coalesce(p_confirmation,''),'|','｜'),' ',''));
 combined:=btrim(replace(replace(r.stream_title||'｜'||r.timestamp_label,'|','｜'),' ',''));
 IF given IS DISTINCT FROM btrim(replace(replace(r.stream_title,'|','｜'),' ','')) AND given IS DISTINCT FROM combined THEN
  RAISE EXCEPTION '确认标题不匹配';
 END IF;
 IF r.state NOT IN ('withdrawn','rejected') AND r.deleted_at IS NULL THEN
  RAISE EXCEPTION '仅已撤回、已拒绝或已删除的申请可永久删除';
 END IF;
 IF r.job_id IS NOT NULL THEN
  SELECT * INTO j FROM public.st_cloud_jobs WHERE id=r.job_id;
  IF j.id IS NULL OR j.state IN ('uploading','queued','running','waiting_configuration','waiting_reconciliation') THEN
   RAISE EXCEPTION '云任务仍活动，不能永久删除';
  END IF;
  RAISE EXCEPTION '该申请已关联云任务，请在项目/任务的永久删除中处理';
 END IF;
 DELETE FROM public.st_subtitle_notifications WHERE request_id=r.id;
 DELETE FROM public.st_subtitle_auto_runs WHERE request_id=r.id;
 DELETE FROM public.st_source_retention WHERE source_kind='request' AND source_id=r.id;
 DELETE FROM public.st_subtitle_requests WHERE id=r.id;
 RETURN jsonb_build_object('state','purged','request_id',p_request);
END; $$;
INSERT INTO public.st_schema_migrations(version) VALUES('053_purge_title_flex') ON CONFLICT DO NOTHING;
COMMIT;
