-- 056: manual approval must include a decision note (auto approval already writes one).
BEGIN;
SET LOCAL lock_timeout='5s';
CREATE OR REPLACE FUNCTION public.st_request_approval_note_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.state='approved' AND OLD.state IS DISTINCT FROM 'approved'
  AND (NEW.review_note IS NULL OR btrim(NEW.review_note)='') THEN
  RAISE EXCEPTION '请填写审批意见（批准或驳回都必须填写）';
 END IF;
 RETURN NEW;
END; $$;
DROP TRIGGER IF EXISTS st_request_approval_note ON public.st_subtitle_requests;
CREATE TRIGGER st_request_approval_note BEFORE UPDATE OF state ON public.st_subtitle_requests
FOR EACH ROW EXECUTE FUNCTION public.st_request_approval_note_guard();
INSERT INTO public.st_schema_migrations(version) VALUES('056_approval_note_required') ON CONFLICT DO NOTHING;
COMMIT;
