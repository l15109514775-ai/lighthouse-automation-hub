-- 051: owner whitelist listing for the web console.
BEGIN;
SET LOCAL lock_timeout='5s';
CREATE FUNCTION public.st_subtitle_auto_policy_list()
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE result jsonb;
BEGIN
 IF NOT public.st_owner() THEN RAISE EXCEPTION '仅组长可查看自动审批白名单' USING ERRCODE='42501'; END IF;
 SELECT jsonb_build_object(
  'limits',(SELECT to_jsonb(l) FROM public.st_subtitle_auto_limits l WHERE id=1),
  'members',coalesce((
   SELECT jsonb_agg(jsonb_build_object(
    'member_id',m.id,'name',m.display_name,
    'enabled',coalesce(p.enabled,false),
    'daily_jobs',coalesce(p.daily_jobs,2),
    'daily_seconds',coalesce(p.daily_seconds,2400)) ORDER BY m.display_name)
   FROM public.st_members m
   LEFT JOIN public.st_subtitle_auto_policy p ON p.member_id=m.id
   WHERE m.state='active'
    AND EXISTS(SELECT 1 FROM public.st_member_roles r WHERE r.member_id=m.id AND r.role_code<>'bot')
  ),'[]'::jsonb)) INTO result;
 RETURN result;
END; $$;
REVOKE ALL ON FUNCTION public.st_subtitle_auto_policy_list() FROM PUBLIC,anon,service_role;
GRANT EXECUTE ON FUNCTION public.st_subtitle_auto_policy_list() TO authenticated;
INSERT INTO public.st_schema_migrations(version) VALUES('051_auto_policy_list');
COMMIT;
