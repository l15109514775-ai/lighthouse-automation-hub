-- 058: look up a linked member by gateway key + sender hash (for member DM mapping).
BEGIN;
SET LOCAL lock_timeout='5s';
CREATE OR REPLACE FUNCTION public.st_qq_member_lookup(p_key text,p_sender text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE g uuid; m uuid;
BEGIN
 IF p_key IS NULL OR p_key !~ '^[0-9a-f]{64}$' OR p_sender IS NULL OR p_sender !~ '^[0-9a-f]{64}$' THEN
  RAISE EXCEPTION 'Notification access denied' USING ERRCODE='42501';
 END IF;
 SELECT q.id INTO g FROM public.st_qq_gateways q
  WHERE q.active AND q.key_hash=encode(sha256(convert_to(p_key,'UTF8')),'hex');
 IF g IS NULL THEN RAISE EXCEPTION 'Notification access denied' USING ERRCODE='42501'; END IF;
 SELECT l.member_id INTO m FROM public.st_qq_links l
  WHERE l.gateway_id=g AND l.sender_hash=p_sender;
 RETURN jsonb_build_object('member_id',m);
END; $$;
REVOKE ALL ON FUNCTION public.st_qq_member_lookup(text,text) FROM PUBLIC,authenticated,service_role;
GRANT EXECUTE ON FUNCTION public.st_qq_member_lookup(text,text) TO anon;
INSERT INTO public.st_schema_migrations(version) VALUES('058_qq_member_lookup') ON CONFLICT DO NOTHING;
COMMIT;
