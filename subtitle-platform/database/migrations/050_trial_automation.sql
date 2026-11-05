-- 050: trial automation (auto approval, job events, runner feed)
-- Applies on top of 049. Safe to run inside one transaction.
BEGIN;
SET LOCAL lock_timeout='5s';

-- 1. Sources captured at submit time so auto approval does not depend on an owner browser.
ALTER TABLE public.st_subtitle_requests ADD COLUMN sources jsonb;

-- 2. Arbiter allowance on the job (gpt-4o-transcribe windows).
ALTER TABLE public.st_cloud_jobs ADD COLUMN max_openai_requests integer NOT NULL DEFAULT 10
  CHECK (max_openai_requests BETWEEN 0 AND 40);

-- 3. Job lifecycle notifications.
ALTER TABLE public.st_subtitle_notifications DROP CONSTRAINT st_subtitle_notifications_event_check;
ALTER TABLE public.st_subtitle_notifications ADD CONSTRAINT st_subtitle_notifications_event_check
  CHECK (event = ANY (ARRAY['submitted','approved','rejected','withdrawn','source_refreshed','started','completed','failed']));

-- 4. Dedicated bot member for automatic decisions.
INSERT INTO public.st_members(auth_subject,display_name,state)
VALUES('bot:subtitle-auto','字幕自动执行','active')
ON CONFLICT (auth_subject) DO NOTHING;
INSERT INTO public.st_member_roles(member_id,role_code)
SELECT id,'bot' FROM public.st_members WHERE auth_subject='bot:subtitle-auto'
ON CONFLICT DO NOTHING;

-- 5. Policy and daily limits.
CREATE TABLE public.st_subtitle_auto_policy(
 member_id uuid PRIMARY KEY REFERENCES public.st_members(id),
 enabled boolean NOT NULL DEFAULT false,
 daily_jobs integer NOT NULL DEFAULT 2 CHECK (daily_jobs BETWEEN 0 AND 10),
 daily_seconds integer NOT NULL DEFAULT 2400 CHECK (daily_seconds BETWEEN 0 AND 7200),
 updated_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE public.st_subtitle_auto_policy ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.st_subtitle_auto_policy FROM PUBLIC,anon,authenticated,service_role;

CREATE TABLE public.st_subtitle_auto_limits(
 id integer PRIMARY KEY DEFAULT 1 CHECK (id=1),
 enabled boolean NOT NULL DEFAULT true,
 daily_jobs integer NOT NULL DEFAULT 6 CHECK (daily_jobs BETWEEN 0 AND 50),
 daily_seconds integer NOT NULL DEFAULT 7200 CHECK (daily_seconds BETWEEN 0 AND 86400),
 updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO public.st_subtitle_auto_limits(id) VALUES(1) ON CONFLICT DO NOTHING;
ALTER TABLE public.st_subtitle_auto_limits ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.st_subtitle_auto_limits FROM PUBLIC,anon,authenticated,service_role;

CREATE TABLE public.st_subtitle_auto_runs(
 id bigserial PRIMARY KEY,
 request_id uuid NOT NULL REFERENCES public.st_subtitle_requests(id),
 member_id uuid NOT NULL REFERENCES public.st_members(id),
 job_id uuid REFERENCES public.st_cloud_jobs(id),
 seconds integer NOT NULL,
 decided_at timestamptz NOT NULL DEFAULT now());
ALTER TABLE public.st_subtitle_auto_runs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.st_subtitle_auto_runs FROM PUBLIC,anon,authenticated,service_role;
CREATE INDEX st_subtitle_auto_runs_day ON public.st_subtitle_auto_runs(member_id,decided_at);

-- 6. Owner-managed policy endpoints.
CREATE FUNCTION public.st_subtitle_auto_policy_set(p_member uuid,p_enabled boolean,p_daily_jobs integer DEFAULT 2,p_daily_seconds integer DEFAULT 2400)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE m public.st_members;
BEGIN
 IF NOT public.st_owner() THEN RAISE EXCEPTION '仅组长可设置自动审批策略' USING ERRCODE='42501'; END IF;
 IF p_member IS NULL OR p_enabled IS NULL OR p_daily_jobs IS NULL OR p_daily_seconds IS NULL
 OR p_daily_jobs NOT BETWEEN 0 AND 10 OR p_daily_seconds NOT BETWEEN 0 AND 7200
 THEN RAISE EXCEPTION '自动审批参数无效'; END IF;
 SELECT * INTO m FROM public.st_members WHERE id=p_member AND state='active';
 IF NOT FOUND THEN RAISE EXCEPTION '成员不存在或已停用'; END IF;
 INSERT INTO public.st_subtitle_auto_policy(member_id,enabled,daily_jobs,daily_seconds,updated_at)
 VALUES(p_member,p_enabled,p_daily_jobs,p_daily_seconds,now())
 ON CONFLICT (member_id) DO UPDATE SET enabled=EXCLUDED.enabled,daily_jobs=EXCLUDED.daily_jobs,
  daily_seconds=EXCLUDED.daily_seconds,updated_at=now();
 INSERT INTO public.st_audit_events(actor_subject,database_role,entity_table,entity_id,operation,metadata)
 VALUES(auth.uid(),session_user,'st_subtitle_auto_policy',p_member::text,'UPSERT',
  jsonb_build_object('enabled',p_enabled,'daily_jobs',p_daily_jobs,'daily_seconds',p_daily_seconds));
 RETURN jsonb_build_object('member_id',p_member,'enabled',p_enabled,'daily_jobs',p_daily_jobs,'daily_seconds',p_daily_seconds);
END; $$;

CREATE FUNCTION public.st_subtitle_auto_limits_set(p_enabled boolean,p_daily_jobs integer,p_daily_seconds integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF NOT public.st_owner() THEN RAISE EXCEPTION '仅组长可设置自动审批限额' USING ERRCODE='42501'; END IF;
 IF p_enabled IS NULL OR p_daily_jobs NOT BETWEEN 0 AND 50 OR p_daily_seconds NOT BETWEEN 0 AND 86400
 THEN RAISE EXCEPTION '自动审批限额无效'; END IF;
 UPDATE public.st_subtitle_auto_limits SET enabled=p_enabled,daily_jobs=p_daily_jobs,
  daily_seconds=p_daily_seconds,updated_at=now() WHERE id=1;
 RETURN jsonb_build_object('enabled',p_enabled,'daily_jobs',p_daily_jobs,'daily_seconds',p_daily_seconds);
END; $$;

-- 7. Automatic decision: worker-key authenticated, called by the self-hosted runner.
CREATE FUNCTION public.st_subtitle_auto_decide(p_key text,p_limit integer DEFAULT 5)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE w public.st_cloud_workers; lim public.st_subtitle_auto_limits; r public.st_subtitle_requests;
 bot uuid; proj uuid; task uuid; j uuid; n integer:=0; approved jsonb:='[]'::jsonb; queued jsonb;
 today date:=(now() AT TIME ZONE 'Asia/Shanghai')::date;
 pol public.st_subtitle_auto_policy; used_jobs integer; used_seconds integer; valid boolean; title text;
BEGIN
 IF p_key IS NULL OR p_key !~ '^[0-9a-f]{64}$' THEN RAISE EXCEPTION '云执行身份无效' USING ERRCODE='42501'; END IF;
 SELECT * INTO w FROM public.st_cloud_workers WHERE key_hash=encode(sha256(convert_to(p_key,'UTF8')),'hex') AND public.st_worker_valid(id);
 IF w.id IS NULL THEN RAISE EXCEPTION '云执行身份无效或已过期' USING ERRCODE='42501'; END IF;
 IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 10 THEN RAISE EXCEPTION '自动审批批量无效'; END IF;
 SELECT * INTO lim FROM public.st_subtitle_auto_limits WHERE id=1;
 IF NOT lim.enabled THEN
  RETURN jsonb_build_object('state','disabled','approved','[]'::jsonb,'queued','[]'::jsonb);
 END IF;
 SELECT id INTO bot FROM public.st_members WHERE auth_subject='bot:subtitle-auto' AND state='active';
 IF bot IS NULL THEN RAISE EXCEPTION '自动审批机器人身份缺失'; END IF;
 IF (SELECT count(*) FROM public.st_subtitle_auto_runs WHERE decided_at::date=today) >= lim.daily_jobs THEN
  RETURN jsonb_build_object('state','daily_jobs_exhausted','approved','[]'::jsonb,'queued','[]'::jsonb);
 END IF;
 IF (SELECT coalesce(sum(seconds),0) FROM public.st_subtitle_auto_runs WHERE decided_at::date=today) >= lim.daily_seconds THEN
  RETURN jsonb_build_object('state','daily_seconds_exhausted','approved','[]'::jsonb,'queued','[]'::jsonb);
 END IF;
 FOR r IN SELECT * FROM public.st_subtitle_requests
  WHERE state='pending' AND job_id IS NULL AND deleted_at IS NULL AND sources IS NOT NULL
  ORDER BY created_at LIMIT p_limit LOOP
  IF n>=p_limit THEN EXIT; END IF;
  SELECT * INTO pol FROM public.st_subtitle_auto_policy WHERE member_id=r.requested_by AND enabled;
  IF NOT FOUND THEN CONTINUE; END IF;
  IF EXISTS(SELECT 1 FROM public.st_members m WHERE m.id=r.requested_by AND m.state<>'active') THEN CONTINUE; END IF;
  SELECT count(*),coalesce(sum(seconds),0) INTO used_jobs,used_seconds
   FROM public.st_subtitle_auto_runs WHERE member_id=r.requested_by AND decided_at::date=today;
  IF used_jobs>=pol.daily_jobs OR used_seconds+r.declared_seconds>pol.daily_seconds THEN CONTINUE; END IF;
  valid:=true;
  BEGIN
   PERFORM public.st_subtitle_source_check(r.id,r.sources);
  EXCEPTION WHEN OTHERS THEN
   valid:=false;
  END;
  IF NOT valid THEN CONTINUE; END IF;
  IF (SELECT count(*) FROM public.st_subtitle_auto_runs WHERE decided_at::date=today)>=lim.daily_jobs
   OR (SELECT coalesce(sum(seconds),0) FROM public.st_subtitle_auto_runs WHERE decided_at::date=today)+r.declared_seconds>lim.daily_seconds
  THEN EXIT; END IF;
  title:=btrim(r.stream_title)||'｜'||btrim(r.timestamp_label)||'｜AI字幕初稿';
  INSERT INTO public.st_projects(title,description,state,created_by)
  VALUES(title,'成员登记，自动审批的云端 AI 初稿；需人工听校。','active',bot) RETURNING id INTO proj;
  INSERT INTO public.st_tasks(project_id,title,description,kind,origin,state,created_by)
  VALUES(proj,title,'自动审批创建','translate','bot','open',bot) RETURNING id INTO task;
  INSERT INTO public.st_project_members(project_id,member_id,role)
  VALUES(proj,r.requested_by,'contributor') ON CONFLICT DO NOTHING;
  j:=gen_random_uuid();
  INSERT INTO public.st_cloud_jobs(id,project_id,task_id,requested_by,worker_id,file_name,mime_type,byte_size,sha256,
   max_video_seconds,max_text_requests,max_secondary_requests,max_openai_requests,sources,source_expires_at,state,stage)
  VALUES(j,proj,task,r.requested_by,w.id,r.file_name,r.mime_type,r.byte_size,r.sha256,
   least(r.declared_seconds,1200),400,40,10,r.sources,now()+interval '23 hours','queued','自动审批通过，等待执行器领取');
  UPDATE public.st_subtitle_requests SET state='approved',job_id=j,reviewed_by=bot,reviewed_at=now(),
   review_note='自动审批（策略）',updated_at=now() WHERE id=r.id;
  INSERT INTO public.st_subtitle_notifications(request_id,event) VALUES(r.id,'approved') ON CONFLICT DO NOTHING;
  INSERT INTO public.st_subtitle_auto_runs(request_id,member_id,job_id,seconds)
  VALUES(r.id,r.requested_by,j,r.declared_seconds);
  approved:=approved||jsonb_build_object('request_id',r.id,'job_id',j);
  n:=n+1;
 END LOOP;
 SELECT coalesce(jsonb_agg(id ORDER BY created_at),'[]'::jsonb) INTO queued
  FROM public.st_cloud_jobs WHERE worker_id=w.id AND state='queued' AND source_expires_at>now();
 RETURN jsonb_build_object('state','ok','approved',approved,'queued',queued,
  'limits',jsonb_build_object('daily_jobs',lim.daily_jobs,'daily_seconds',lim.daily_seconds));
END; $$;

-- 8. Request submit now stores the signed source URLs.
DROP FUNCTION public.st_subtitle_request_submit(uuid);
CREATE FUNCTION public.st_subtitle_request_submit(p_request uuid,p_sources jsonb DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE r public.st_subtitle_requests;
BEGIN
 SELECT * INTO r FROM public.st_subtitle_requests WHERE id=p_request FOR UPDATE;
 IF NOT public.st_formal_member() OR r.requested_by IS DISTINCT FROM public.st_actor() THEN RAISE EXCEPTION '无此申请权限' USING ERRCODE='42501'; END IF;
 IF r.state='pending' THEN RETURN jsonb_build_object('state','pending','request_id',r.id); END IF;
 IF r.state<>'uploading' OR r.upload_until<=now() THEN RAISE EXCEPTION '申请已关闭或上传已过期'; END IF;
 PERFORM public.st_subtitle_source_check(r.id,p_sources);
 UPDATE public.st_subtitle_requests SET state='pending',sources=coalesce(p_sources,sources),updated_at=now() WHERE id=r.id;
 INSERT INTO public.st_subtitle_notifications(request_id,event) VALUES(r.id,'submitted');
 RETURN jsonb_build_object('state','pending','request_id',r.id);
END; $$;

-- 9. Job state changes become notifications for the bot.
CREATE FUNCTION public.st_cloud_job_notify() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE ev text;
BEGIN
 IF NEW.state IS DISTINCT FROM OLD.state THEN
  ev:=CASE NEW.state WHEN 'running' THEN 'started' WHEN 'succeeded' THEN 'completed'
       WHEN 'failed' THEN 'failed' ELSE NULL END;
  IF ev IS NOT NULL THEN
   INSERT INTO public.st_subtitle_notifications(request_id,event)
   SELECT r.id,ev FROM public.st_subtitle_requests r WHERE r.job_id=NEW.id
   ON CONFLICT DO NOTHING;
  END IF;
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER st_cloud_job_notify AFTER UPDATE OF state ON public.st_cloud_jobs
FOR EACH ROW EXECUTE FUNCTION public.st_cloud_job_notify();

-- 10. Bot feed: started / completed / failed with applicant and owner identity.
CREATE FUNCTION public.st_qq_job_feed(p_key text,p_sender text,p_after timestamptz DEFAULT NULL,
 p_after_id uuid DEFAULT NULL,p_ids uuid[] DEFAULT NULL)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE g uuid; result jsonb;
BEGIN
 g:=public.st_qq_notification_access(p_key,p_sender);
 IF NOT EXISTS(SELECT 1 FROM public.st_qq_notification_settings WHERE gateway_id=g AND approval_enabled)
 THEN RAISE EXCEPTION 'Job notification access disabled' USING ERRCODE='42501'; END IF;
 IF (p_after IS NULL)<>(p_after_id IS NULL) OR p_after>now()+interval '1 minute' OR cardinality(p_ids)>10
 THEN RAISE EXCEPTION 'Invalid cursor or event ids'; END IF;
 SELECT jsonb_build_object('enabled',true,'events',coalesce(jsonb_agg(to_jsonb(x) ORDER BY x.created_at,x.id),'[]'::jsonb)) INTO result
 FROM (
  SELECT n.id,n.request_id,n.event,n.created_at,r.requested_by AS member_id,m.display_name AS member_name,
   r.stream_title,r.timestamp_label,r.job_id,j.state AS job_state,
   o.id AS owner_id,o.display_name AS owner_name
  FROM public.st_subtitle_notifications n
  JOIN public.st_subtitle_requests r ON r.id=n.request_id
  JOIN public.st_members m ON m.id=r.requested_by
  JOIN public.st_cloud_jobs j ON j.id=r.job_id
  JOIN LATERAL (SELECT mm.id,mm.display_name FROM public.st_members mm
   JOIN public.st_member_roles rr ON rr.member_id=mm.id AND rr.role_code='owner'
   WHERE mm.state='active' LIMIT 1) o ON true
  WHERE n.event IN ('started','completed','failed')
   AND (p_after IS NULL OR (n.created_at,n.id)>(p_after,p_after_id))
   AND (p_ids IS NULL OR n.id=ANY(p_ids))
  ORDER BY n.created_at,n.id LIMIT 50
 ) x;
 RETURN result;
END; $$;

-- 11. Grants: only the two new bot-facing endpoints are reachable with a key.
REVOKE ALL ON FUNCTION public.st_subtitle_auto_decide(text,integer) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.st_subtitle_auto_decide(text,integer) TO anon,authenticated;
REVOKE ALL ON FUNCTION public.st_qq_job_feed(text,text,timestamptz,uuid,uuid[]) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.st_qq_job_feed(text,text,timestamptz,uuid,uuid[]) TO anon;
REVOKE ALL ON FUNCTION public.st_subtitle_auto_policy_set(uuid,boolean,integer,integer),
 public.st_subtitle_auto_limits_set(boolean,integer,integer) FROM PUBLIC,anon,service_role;
GRANT EXECUTE ON FUNCTION public.st_subtitle_auto_policy_set(uuid,boolean,integer,integer),
 public.st_subtitle_auto_limits_set(boolean,integer,integer) TO authenticated;
REVOKE ALL ON FUNCTION public.st_subtitle_request_submit(uuid,jsonb) FROM PUBLIC,anon,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.st_subtitle_request_submit(uuid,jsonb) TO authenticated;

INSERT INTO public.st_schema_migrations(version) VALUES('050_trial_automation');
COMMIT;
