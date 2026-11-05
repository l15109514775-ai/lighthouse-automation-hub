-- 054: allow request hard delete to remove immutable notification rows inside the owner purge.
BEGIN;
SET LOCAL lock_timeout='5s';
CREATE OR REPLACE FUNCTION public.st_reject_mutation()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE j public.st_purge_jobs; oldj jsonb:=to_jsonb(OLD);newj jsonb;
BEGIN
 IF TG_OP='DELETE' AND TG_TABLE_NAME='st_subtitle_notifications'
  AND current_setting('subtitle.allow_request_purge',true)='on' THEN
  RETURN OLD;
 END IF;
 IF TG_OP='UPDATE' AND TG_TABLE_NAME IN ('st_deliveries','st_reviews','st_agent_runs') THEN
  SELECT * INTO j FROM public.st_purge_jobs WHERE id=nullif(current_setting('subtitle.purge_job',true),'')::uuid AND state='deleting';
  IF j.id IS NOT NULL AND public.st_purge_matches(j.kind,j.target_id,(oldj->>'project_id')::uuid,(oldj->>'task_id')::uuid) THEN
   newj:=to_jsonb(NEW);
   IF TG_TABLE_NAME='st_deliveries' AND (newj-ARRAY['content_text','notes'])=(oldj-ARRAY['content_text','notes']) AND newj->>'content_text'='' AND newj->>'notes'='' THEN RETURN NEW;
   ELSIF TG_TABLE_NAME='st_reviews' AND (newj-'feedback')=(oldj-'feedback') AND newj->>'feedback'='' THEN RETURN NEW;
   ELSIF TG_TABLE_NAME='st_agent_runs' AND (newj-'summary')=(oldj-'summary') AND newj->>'summary'='' THEN RETURN NEW; END IF;
  END IF;
 END IF;
 RAISE EXCEPTION 'Immutable record: create a new version, revocation or reversal instead';
END; $$;

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
 BEGIN
  PERFORM set_config('subtitle.allow_request_purge','on',true);
  DELETE FROM public.st_subtitle_notifications WHERE request_id=r.id;
  DELETE FROM public.st_subtitle_auto_runs WHERE request_id=r.id;
  DELETE FROM public.st_source_retention WHERE source_kind='request' AND source_id=r.id;
  DELETE FROM public.st_subtitle_requests WHERE id=r.id;
  PERFORM set_config('subtitle.allow_request_purge','off',true);
 EXCEPTION WHEN OTHERS THEN
  PERFORM set_config('subtitle.allow_request_purge','off',true);
  RAISE;
 END;
 RETURN jsonb_build_object('state','purged','request_id',p_request);
END; $$;
INSERT INTO public.st_schema_migrations(version) VALUES('054_purge_immutable') ON CONFLICT DO NOTHING;
COMMIT;
