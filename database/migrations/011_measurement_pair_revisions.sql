
BEGIN;

-- Keep each submitted measurement pair as a separate revision.
ALTER TABLE verified_measurement_pairs
    ADD COLUMN IF NOT EXISTS revision_no INTEGER NOT NULL DEFAULT 1
        CHECK (revision_no >= 1),
    ADD COLUMN IF NOT EXISTS submission_note TEXT;

-- Preserve the original submission note for any still-pending legacy pair.
UPDATE verified_measurement_pairs
SET submission_note = review_note
WHERE review_status = 'pending'
  AND submission_note IS NULL;

-- Replace the one-pair-per-case constraint with revision-aware uniqueness.
ALTER TABLE verified_measurement_pairs
    DROP CONSTRAINT IF EXISTS verified_measurement_pairs_case_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_measurement_pair_case_revision
    ON verified_measurement_pairs (case_id, revision_no);

-- A case may have rejected history but only one pending or approved pair.
CREATE UNIQUE INDEX IF NOT EXISTS uq_measurement_pair_active_case
    ON verified_measurement_pairs (case_id)
    WHERE review_status IN ('pending', 'approved');

COMMIT;
