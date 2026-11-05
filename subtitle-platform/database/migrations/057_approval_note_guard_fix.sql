-- 057: approval note guard must not depend on the previous state.
BEGIN;
SET LOCAL lock_timeout='5s';
CREATE OR REPLACE FUNCTION public.st_request_approval_note_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.state='approved' AND (NEW.review_note IS NULL OR btrim(NEW.review_note)='') THEN
  RAISE EXCEPTION '请填写审批意见（批准或驳回都必须填写）';
 END IF;
 RETURN NEW;
END; $$;
INSERT INTO public.st_schema_migrations(version) VALUES('057_approval_note_guard_fix') ON CONFLICT DO NOTHING;
COMMIT;
