-- Step 34: retire the obsolete ISO-only legacy storage, once backed up.
-- This is an OPTIONAL destructive operation.  It never touches the five new
-- workflow tables or the measured-energy branch linked to source_documents.
-- Run with: docker exec -i request_app_db psql -U app_user -d request_app -v ON_ERROR_STOP=1 < scripts/034_retire_legacy_iso_only.sql
-- Only run after a VERIFIED, recent pg_dump archive.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $guard$
DECLARE
    kept_case_id BIGINT;
    kept_documents BIGINT;
    kept_revisions BIGINT;
    kept_values BIGINT;
BEGIN
    SELECT case_id INTO kept_case_id FROM public.modernisation_cases
    WHERE case_ref = 'TEST-MOD-REV-001';
    IF kept_case_id IS NULL THEN
        RAISE EXCEPTION 'Safety check failed: TEST-MOD-REV-001 is missing';
    END IF;

    SELECT COUNT(DISTINCT d.document_id), COUNT(DISTINCT r.revision_id),
           COUNT(DISTINCT v.value_id)
    INTO kept_documents, kept_revisions, kept_values
    FROM public.project_documents d
    LEFT JOIN public.document_revisions r ON r.document_id = d.document_id
    LEFT JOIN public.engineering_values v ON v.revision_id = r.revision_id
    WHERE d.case_id = kept_case_id AND d.document_type = 'mod_pdf';

    IF kept_documents < 1 OR kept_revisions < 2 OR kept_values < 2 THEN
        RAISE EXCEPTION
            'Safety check failed: retained case missing MOD document / revisions / values (%, %, %)',
            kept_documents, kept_revisions, kept_values;
    END IF;
    IF NOT EXISTS (
      SELECT 1 FROM public.document_reviews h
      JOIN public.document_revisions r ON r.revision_id = h.revision_id
      JOIN public.project_documents d ON d.document_id = r.document_id
      WHERE d.case_id = kept_case_id
    ) THEN
      RAISE EXCEPTION 'Safety check failed: retained case review history is missing';
    END IF;
    RAISE NOTICE 'Retained TEST-MOD-REV-001: % MOD document(s), % revision(s), % value(s)',
        kept_documents, kept_revisions, kept_values;
END
$guard$;

-- These three tables have only the old ISO assessment data.  No CASCADE is
-- used: any undiscovered view/FK dependency aborts and rolls back the whole
-- transaction, rather than silently removing extra database objects.
DROP TABLE IF EXISTS public.iso_extracted_values RESTRICT;
DROP TABLE IF EXISTS public.iso_documents RESTRICT;
DROP TABLE IF EXISTS public.iso_assessments RESTRICT;

-- Retain source_documents, extraction_revisions, enercal views and measured
-- energy tables: those remain linked by the legacy measured-savings workflow.
-- The application itself now reads new ISO evidence via project_documents.

SELECT m.case_ref, COUNT(DISTINCT d.document_id) AS retained_mod_documents,
       COUNT(DISTINCT r.revision_id) AS retained_revisions
FROM public.modernisation_cases m
JOIN public.project_documents d ON d.case_id = m.case_id
JOIN public.document_revisions r ON r.document_id = d.document_id
WHERE m.case_ref = 'TEST-MOD-REV-001' AND d.document_type = 'mod_pdf'
GROUP BY m.case_ref;
COMMIT;
