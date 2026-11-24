-- Step 35: reversible retirement of OLD MOD-only tables from the public schema.
-- This is an archival schema move, NOT permanent deletion. Data and FK/view
-- object dependencies stay intact. Active app modules use five-table storage.
-- Default: perform a full rehearsal then ROLLBACK.
-- Apply: psql -v apply=1 -v ON_ERROR_STOP=1 -f scripts/035_archive_legacy_mod.sql
-- Must use a fresh, validated pg_dump backup before applying.
\set ON_ERROR_STOP on
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

DO $guard$
DECLARE
    p RECORD;
    v_kept_case_id BIGINT;
    v_mod_docs BIGINT;
    v_revisions BIGINT;
    v_values BIGINT;
    v_reviews BIGINT;
    v_observations BIGINT;
    v_meter_pairs BIGINT;
BEGIN
    FOR p IN SELECT unnest(ARRAY[
        'modernisation_cases', 'project_documents', 'document_revisions',
        'engineering_values', 'document_reviews', 'elevators',
        'source_documents', 'document_pages', 'extraction_revisions',
        'extraction_candidates', 'enercal_specifications',
        'enercal_review_events', 'energy_observations',
        'verified_measurement_pairs'
    ]) AS name
    LOOP
        IF to_regclass(format('public.%I', p.name)) IS NULL THEN
            RAISE EXCEPTION 'Expected public table missing: %', p.name;
        END IF;
    END LOOP;

    IF EXISTS (
        SELECT 1 FROM pg_namespace WHERE nspname = 'legacy_archive'
    ) THEN
        RAISE EXCEPTION 'legacy_archive schema already exists; inspect it before proceeding';
    END IF;

    SELECT case_id INTO v_kept_case_id
    FROM public.modernisation_cases WHERE case_ref = 'TEST-MOD-REV-001';
    IF v_kept_case_id IS NULL THEN
        RAISE EXCEPTION 'Retained case TEST-MOD-REV-001 is missing';
    END IF;

    SELECT COUNT(DISTINCT d.document_id),
           COUNT(DISTINCT r.revision_id),
           COUNT(DISTINCT v.value_id)
    INTO v_mod_docs, v_revisions, v_values
    FROM public.project_documents d
    LEFT JOIN public.document_revisions r ON r.document_id = d.document_id
    LEFT JOIN public.engineering_values v ON v.revision_id = r.revision_id
    WHERE d.case_id = v_kept_case_id AND d.document_type = 'mod_pdf';

    SELECT COUNT(*) INTO v_reviews
    FROM public.document_reviews h
    JOIN public.document_revisions r ON r.revision_id = h.revision_id
    JOIN public.project_documents d ON d.document_id = r.document_id
    WHERE d.case_id = v_kept_case_id AND d.document_type = 'mod_pdf';

    IF v_mod_docs <> 1 OR v_revisions <> 2 OR v_values <> 126 OR v_reviews < 2 THEN
        RAISE EXCEPTION 'Retained case guard failed: docs %, revisions %, values %, review events %',
            v_mod_docs, v_revisions, v_values, v_reviews;
    END IF;

    SELECT COUNT(*) INTO v_observations FROM public.energy_observations;
    SELECT COUNT(*) INTO v_meter_pairs FROM public.verified_measurement_pairs;
    IF v_observations < 47 OR v_meter_pairs <> 0 THEN
        RAISE EXCEPTION 'Measurement safety guard failed: observations %, verified pairs %',
            v_observations, v_meter_pairs;
    END IF;

    RAISE NOTICE 'Guard passed: 1 retained MOD document, 2 revisions, 126 values, % review events; % observations; 0 meter pairs',
        v_reviews, v_observations;
END
$guard$;

-- Keep FK-backed metered workflow objects (and its empty pairing table).
-- Moving source_documents across schemas preserves FK references and view
-- references via PostgreSQL object IDs. The old diagnostic checker that
-- hard-codes public.source_documents must be retired or adapted separately.
CREATE SCHEMA legacy_archive;
ALTER TABLE public.document_pages SET SCHEMA legacy_archive;
ALTER TABLE public.extraction_candidates SET SCHEMA legacy_archive;
ALTER TABLE public.enercal_review_events SET SCHEMA legacy_archive;
ALTER TABLE public.enercal_specifications SET SCHEMA legacy_archive;
ALTER TABLE public.extraction_revisions SET SCHEMA legacy_archive;
ALTER TABLE public.source_documents SET SCHEMA legacy_archive;

-- Fail closed if expected relations aren't in the target schema.
DO $verify$
DECLARE
    p RECORD;
BEGIN
    FOR p IN SELECT unnest(ARRAY[
        'source_documents', 'document_pages', 'extraction_revisions',
        'extraction_candidates', 'enercal_specifications', 'enercal_review_events'
    ]) AS name
    LOOP
        IF to_regclass(format('public.%I', p.name)) IS NOT NULL OR
           to_regclass(format('legacy_archive.%I', p.name)) IS NULL THEN
            RAISE EXCEPTION 'Legacy schema move incomplete: %', p.name;
        END IF;
    END LOOP;
    IF to_regclass('public.project_documents') IS NULL OR
       to_regclass('public.energy_observations') IS NULL OR
       to_regclass('public.verified_measurement_pairs') IS NULL THEN
        RAISE EXCEPTION 'A required public workflow table is missing';
    END IF;
END
$verify$;

\if :{?apply}
  \if :apply
    COMMIT;
    \echo 'APPLIED: old MOD tables moved to legacy_archive (no data deleted).'
  \else
    ROLLBACK;
    \echo 'REHEARSAL ONLY: rollback complete; no schema changes saved.'
  \endif
\else
  ROLLBACK;
  \echo 'REHEARSAL ONLY: rollback complete; no schema changes saved.'
\endif
