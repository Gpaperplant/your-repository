
BEGIN;

-- Allow new ISO Excel uploads without an EE PDF.
ALTER TABLE iso_assessments
    ALTER COLUMN pdf_sha256 DROP NOT NULL;

-- Prevent duplicate Excel-only assessments for the same case.
CREATE UNIQUE INDEX IF NOT EXISTS uq_iso_excel_only_case_hash
    ON iso_assessments (case_id, excel_sha256)
    WHERE pdf_sha256 IS NULL;

COMMIT;
