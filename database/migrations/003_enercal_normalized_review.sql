
-- =====================================================
-- KONE FYP DATABASE
-- 003: NORMALISED ENERCAL ENGINEERING REVIEW
-- =====================================================

-- Existing documents and extraction candidates
-- will remain unchanged during this first step.


-- =====================================================
-- TABLE 1: EXTRACTION REVISIONS
-- =====================================================
-- One row represents one extraction revision.
-- Review status is stored once per revision,
-- instead of being repeated across 16 fields.

CREATE TABLE IF NOT EXISTS extraction_revisions (

    revision_id BIGINT
        GENERATED ALWAYS AS IDENTITY
        PRIMARY KEY,

    document_id BIGINT NOT NULL
        REFERENCES source_documents(document_id),

    revision_no INTEGER NOT NULL
        CHECK (revision_no >= 1),

    review_status VARCHAR(40)
        NOT NULL DEFAULT 'pending'
        CHECK (
            review_status IN (
                'pending',
                'changes_requested',
                'further_verification_required',
                'approved',
                'rejected'
            )
        ),

    review_note TEXT,

    reviewed_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ
        NOT NULL DEFAULT NOW(),

    UNIQUE (document_id, revision_no)

);


-- =====================================================
-- TABLE 2: ENERCAL REVIEW EVENTS
-- =====================================================
-- Stores the history of engineering decisions.
--
-- Examples:
-- Changes requested
-- Further verification requested
-- Verification completed
-- Corrected revision submitted
-- Extraction approved
-- Final rejection

CREATE TABLE IF NOT EXISTS enercal_review_events (

    event_id BIGINT
        GENERATED ALWAYS AS IDENTITY
        PRIMARY KEY,

    revision_id BIGINT NOT NULL
        REFERENCES extraction_revisions(revision_id),

    event_type VARCHAR(50) NOT NULL,

    previous_status VARCHAR(40),

    new_status VARCHAR(40) NOT NULL,

    reason TEXT,

    actor_id TEXT,

    created_at TIMESTAMPTZ
        NOT NULL DEFAULT NOW()

);


-- =====================================================
-- INDEX FOR REVIEW HISTORY
-- =====================================================
-- Allows efficient loading of events associated
-- with a particular revision.

CREATE INDEX IF NOT EXISTS
    idx_enercal_review_events_revision

ON enercal_review_events (
    revision_id,
    created_at
);
