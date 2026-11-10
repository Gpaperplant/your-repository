-- Safely preview deletion of MOD6767 and its own PDF/document records only.
-- The default ending is ROLLBACK. Do not remove the safety checks.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

-- Exact case reference and document ID guard prevent deleting other projects.
CREATE TEMP TABLE _target_cases ON COMMIT DROP AS
SELECT case_id, case_ref
FROM public.modernisation_cases
WHERE case_ref = 'MOD6767';

CREATE TEMP TABLE _target_docs ON COMMIT DROP AS
SELECT d.document_id
FROM public.source_documents d
JOIN _target_cases t ON t.case_id = d.case_id;

CREATE TEMP TABLE _target_pages ON COMMIT DROP AS
SELECT p.page_id
FROM public.document_pages p
JOIN _target_docs d ON d.document_id = p.document_id;

CREATE TEMP TABLE _target_revisions ON COMMIT DROP AS
SELECT r.revision_id
FROM public.extraction_revisions r
JOIN _target_docs d ON d.document_id = r.document_id;

-- Refuse to proceed if the database, target set, or dependencies differ.
DO $$
DECLARE
    unexpected_fks TEXT;
BEGIN
    IF current_database() <> 'request_app' THEN
        RAISE EXCEPTION 'Refusing cleanup in database %', current_database();
    END IF;

    IF (SELECT COUNT(*) FROM _target_cases) <> 1 THEN
        RAISE EXCEPTION 'Expected exactly one MOD6767 case; found %',
            (SELECT COUNT(*) FROM _target_cases);
    END IF;

    IF (SELECT COUNT(*) FROM _target_docs) <> 1
       OR NOT EXISTS (SELECT 1 FROM _target_docs WHERE document_id = 47) THEN
        RAISE EXCEPTION 'Expected exactly document 47 belonging to MOD6767';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public.modernisation_cases m
        JOIN public.source_documents d ON d.case_id = m.case_id
        WHERE m.case_ref = 'MOD1001' AND d.document_id = 53
    ) OR NOT EXISTS (
        SELECT 1 FROM public.modernisation_cases m
        JOIN public.source_documents d ON d.case_id = m.case_id
        WHERE m.case_ref = 'MOD1002' AND d.document_id = 54
    ) THEN
        RAISE EXCEPTION 'Preservation guard failed: documents 53 or 54 are missing';
    END IF;

    -- Another case must not point to MOD6767 documents through a measurement pair.
    IF EXISTS (
        SELECT 1
        FROM public.verified_measurement_pairs p
        JOIN _target_docs d ON d.document_id = p.document_id
        LEFT JOIN _target_cases t ON t.case_id = p.case_id
        WHERE t.case_id IS NULL
    ) THEN
        RAISE EXCEPTION 'A non-target measurement pair refers to MOD6767 PDF';
    END IF;

    -- Extracted fields must not cross-link target revisions and other pages.
    IF EXISTS (
        SELECT 1
        FROM public.extraction_candidates c
        JOIN _target_revisions r ON r.revision_id = c.extraction_revision_id
        LEFT JOIN _target_pages p ON p.page_id = c.page_id
        WHERE p.page_id IS NULL
    ) THEN
        RAISE EXCEPTION 'Extraction candidate crosses target and non-target documents';
    END IF;

    -- A target page must not contain a candidate from a non-target revision.
    IF EXISTS (
        SELECT 1
        FROM public.extraction_candidates c
        JOIN _target_pages p ON p.page_id = c.page_id
        LEFT JOIN _target_revisions r ON r.revision_id = c.extraction_revision_id
        WHERE r.revision_id IS NULL
    ) THEN
        RAISE EXCEPTION 'Target page contains a non-target extraction revision';
    END IF;

    -- Stop if another table has a foreign key into records being deleted.
    SELECT STRING_AGG(DISTINCT con.conrelid::REGCLASS::TEXT, ', ')
    INTO unexpected_fks
    FROM pg_constraint con
    WHERE con.contype = 'f'
      AND con.confrelid = ANY (ARRAY[
          'public.modernisation_cases'::REGCLASS,
          'public.source_documents'::REGCLASS,
          'public.document_pages'::REGCLASS,
          'public.extraction_revisions'::REGCLASS,
          'public.extraction_candidates'::REGCLASS,
          'public.verified_measurement_pairs'::REGCLASS,
          'public.enercal_review_events'::REGCLASS,
          'public.enercal_specifications'::REGCLASS
      ])
      AND con.conrelid <> ALL (ARRAY[
          'public.source_documents'::REGCLASS,
          'public.document_pages'::REGCLASS,
          'public.extraction_revisions'::REGCLASS,
          'public.extraction_candidates'::REGCLASS,
          'public.enercal_review_events'::REGCLASS,
          'public.enercal_specifications'::REGCLASS,
          'public.verified_measurement_pairs'::REGCLASS
      ]);

    IF unexpected_fks IS NOT NULL THEN
        RAISE EXCEPTION 'Additional child tables require review: %', unexpected_fks;
    END IF;
END $$;

-- Confirm which cases and how many owned records would be affected.
SELECT case_ref FROM _target_cases ORDER BY case_ref;
SELECT 'target cases' AS record_type, COUNT(*) AS count FROM _target_cases
UNION ALL SELECT 'target PDFs', COUNT(*) FROM _target_docs
UNION ALL SELECT 'target document pages', COUNT(*) FROM _target_pages
UNION ALL SELECT 'target extraction revisions', COUNT(*) FROM _target_revisions
UNION ALL SELECT 'target review events', COUNT(*)
    FROM public.enercal_review_events e
    JOIN _target_revisions r ON r.revision_id = e.revision_id
UNION ALL SELECT 'target extracted values', COUNT(*)
    FROM public.extraction_candidates c
    WHERE c.page_id IN (SELECT page_id FROM _target_pages)
       OR c.extraction_revision_id IN (SELECT revision_id FROM _target_revisions)
UNION ALL SELECT 'target specifications', COUNT(*)
    FROM public.enercal_specifications s
    JOIN _target_docs d ON d.document_id = s.document_id
UNION ALL SELECT 'target measurement pairs', COUNT(*)
    FROM public.verified_measurement_pairs p
    JOIN _target_cases t ON t.case_id = p.case_id;

-- Delete in child-before-parent order. No CASCADE and no TRUNCATE.
DELETE FROM public.verified_measurement_pairs p
USING _target_cases t
WHERE p.case_id = t.case_id;

DELETE FROM public.enercal_review_events e
USING _target_revisions r
WHERE e.revision_id = r.revision_id;

DELETE FROM public.extraction_candidates c
WHERE c.page_id IN (SELECT page_id FROM _target_pages)
   OR c.extraction_revision_id IN (SELECT revision_id FROM _target_revisions);

DELETE FROM public.enercal_specifications s
USING _target_docs d
WHERE s.document_id = d.document_id;

DELETE FROM public.extraction_revisions r
USING _target_revisions t
WHERE r.revision_id = t.revision_id;

DELETE FROM public.document_pages p
USING _target_pages t
WHERE p.page_id = t.page_id;

DELETE FROM public.source_documents d
USING _target_docs t
WHERE d.document_id = t.document_id;

DELETE FROM public.modernisation_cases m
USING _target_cases t
WHERE m.case_id = t.case_id;

-- Verify MOD6767 is gone and both other cases remain before committing.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.modernisation_cases m
        JOIN _target_cases t ON t.case_id = m.case_id
    ) THEN
        RAISE EXCEPTION 'MOD6767 remains after deletion';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM public.modernisation_cases m
        JOIN public.source_documents d ON d.case_id = m.case_id
        WHERE m.case_ref = 'MOD1001' AND d.document_id = 53
    ) OR NOT EXISTS (
        SELECT 1 FROM public.modernisation_cases m
        JOIN public.source_documents d ON d.case_id = m.case_id
        WHERE m.case_ref = 'MOD1002' AND d.document_id = 54
    ) THEN
        RAISE EXCEPTION 'MOD1001 / MOD1002 PDFs were unexpectedly affected';
    END IF;
END $$;

SELECT case_ref, status
FROM public.modernisation_cases
ORDER BY case_ref;

-- Preview mode: all deletions are rolled back.
ROLLBACK;
