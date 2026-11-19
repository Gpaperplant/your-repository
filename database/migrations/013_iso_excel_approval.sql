BEGIN;

-- Track engineering approval independently for every ISO Excel assessment.
ALTER TABLE iso_assessments
    ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS review_note TEXT NOT NULL DEFAULT '';

-- Enforce the review lifecycle while preserving historical assessments.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'iso_assessments'::regclass
          AND conname = 'iso_assessments_review_status_check'
    ) THEN
        ALTER TABLE iso_assessments
            ADD CONSTRAINT iso_assessments_review_status_check
            CHECK (review_status IN ('pending', 'approved', 'rejected'));
    END IF;
END $$;

COMMIT;
