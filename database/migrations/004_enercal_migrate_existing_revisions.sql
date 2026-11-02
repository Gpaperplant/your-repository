
-- =====================================================
-- KONE FYP DATABASE
-- 004: MIGRATE EXISTING ENERCAL REVISIONS
-- =====================================================

-- PURPOSE
-- Connect existing extraction candidates to the
-- normalised extraction_revisions table.
--
-- Preserve all existing PDF files, extracted values,
-- review notes and historical statuses.


-- =====================================================
-- 1. ADD REVISION REFERENCE
-- =====================================================

ALTER TABLE extraction_candidates
ADD COLUMN IF NOT EXISTS extraction_revision_id BIGINT;


-- Add the foreign key only if it does not exist.

DO $$
BEGIN

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_candidate_revision'
          AND conrelid =
              'extraction_candidates'::regclass
    ) THEN

        ALTER TABLE extraction_candidates
        ADD CONSTRAINT fk_candidate_revision
        FOREIGN KEY (extraction_revision_id)
        REFERENCES extraction_revisions(revision_id);

    END IF;

END;
$$;


-- =====================================================
-- 2. CHECK EXISTING REVIEW STATUSES
-- =====================================================

-- One revision should not have conflicting statuses
-- across its extracted fields.

DO $$
BEGIN

    IF EXISTS (
        SELECT 1
        FROM extraction_candidates c

        JOIN document_pages p
            ON c.page_id = p.page_id

        GROUP BY
            p.document_id,
            c.revision_no

        HAVING COUNT(
            DISTINCT c.review_status
        ) > 1
    ) THEN

        RAISE EXCEPTION
            'Mixed review statuses found. '
            'Inspect existing revisions first.';

    END IF;

END;
$$;


-- =====================================================
-- 3. IMPORT EXISTING REVISIONS
-- =====================================================

-- Previous rejected revisions were correctable.
-- Map them to changes_requested.
--
-- The original rejected status remains unchanged
-- in the old extraction_candidates records.

INSERT INTO extraction_revisions (

    document_id,
    revision_no,
    review_status,
    review_note,
    reviewed_at,
    created_at

)

SELECT

    p.document_id,

    c.revision_no,

    CASE
        WHEN MAX(c.review_status) = 'rejected'
        THEN 'changes_requested'

        ELSE MAX(c.review_status)
    END,

    MAX(c.review_note),

    MAX(c.reviewed_at),

    MIN(c.created_at)

FROM extraction_candidates c

JOIN document_pages p
    ON c.page_id = p.page_id

GROUP BY
    p.document_id,
    c.revision_no

ON CONFLICT (document_id, revision_no)
DO NOTHING;


-- =====================================================
-- 4. LINK CANDIDATES TO REVISIONS
-- =====================================================

UPDATE extraction_candidates c

SET extraction_revision_id = r.revision_id

FROM document_pages p

JOIN extraction_revisions r
    ON r.document_id = p.document_id

WHERE c.page_id = p.page_id

AND c.revision_no = r.revision_no

AND c.extraction_revision_id IS NULL;


-- =====================================================
-- 5. CHECK ALL CANDIDATES HAVE A REVISION
-- =====================================================

DO $$
BEGIN

    IF EXISTS (
        SELECT 1
        FROM extraction_candidates
        WHERE extraction_revision_id IS NULL
    ) THEN

        RAISE EXCEPTION
            'Some extracted values have no revision.';
    END IF;

END;
$$;


-- Require every extraction candidate to reference
-- its corresponding revision.

ALTER TABLE extraction_candidates
ALTER COLUMN extraction_revision_id SET NOT NULL;


-- =====================================================
-- 6. CREATE LOOKUP INDEX
-- =====================================================

CREATE INDEX IF NOT EXISTS
    idx_candidates_extraction_revision

ON extraction_candidates (
    extraction_revision_id
);


-- =====================================================
-- 7. PREVENT DUPLICATE LEGACY HISTORY EVENTS
-- =====================================================

CREATE UNIQUE INDEX IF NOT EXISTS
    uq_review_legacy_import

ON enercal_review_events (revision_id)

WHERE event_type = 'legacy_import';


-- =====================================================
-- 8. IMPORT EXISTING REVIEW HISTORY
-- =====================================================

-- One migration event is stored for each revision.
-- The original legacy review status and notes
-- remain available in the explanation.

INSERT INTO enercal_review_events (

    revision_id,
    event_type,
    previous_status,
    new_status,
    reason,
    actor_id

)

SELECT

    r.revision_id,

    'legacy_import',

    NULL,

    r.review_status,

    'Imported from previous review system. '
    || 'Original status: '
    || MAX(c.review_status)
    || '. '
    || COALESCE(MAX(c.review_note), ''),

    NULL

FROM extraction_revisions r

JOIN document_pages p
    ON p.document_id = r.document_id

JOIN extraction_candidates c
    ON c.page_id = p.page_id
    AND c.revision_no = r.revision_no

GROUP BY
    r.revision_id,
    r.review_status

ON CONFLICT DO NOTHING;
