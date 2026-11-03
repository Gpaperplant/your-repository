
-- 007: Additional EnerCal observations and cross-page checks.
-- Leave the existing 16-field revision and approval tables unchanged.
BEGIN;

-- One row per additional field in an original EnerCal PDF.
CREATE TABLE IF NOT EXISTS enercal_specifications (
    document_id BIGINT NOT NULL
        REFERENCES source_documents(document_id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    field_value TEXT NOT NULL,
    unit TEXT,
    source_page INTEGER NOT NULL CHECK (source_page >= 1),
    PRIMARY KEY (document_id, field_name)
);

-- Speed up viewing a document's fields grouped by source page.
CREATE INDEX IF NOT EXISTS idx_enercal_specs_document_page
    ON enercal_specifications (document_id, source_page);

-- Extracted project information, engineering inputs and report figures.
-- Cross-checks are displayed in the separate validation view.
CREATE OR REPLACE VIEW enercal_observations_view AS
SELECT
    document_id,
    field_name,
    field_value,
    unit,
    source_page
FROM enercal_specifications
WHERE LEFT(field_name, 6) <> 'check_';

-- Cross-page checks, using page 4 as the PDF reference.
CREATE OR REPLACE VIEW enercal_crosschecks_view AS
SELECT
    document_id,
    SUBSTRING(field_name FROM 7) AS check_name,
    field_value AS check_status,
    source_page
FROM enercal_specifications
WHERE LEFT(field_name, 6) = 'check_';

-- Summarise validation results for each document.
CREATE OR REPLACE VIEW enercal_crosscheck_summary_view AS
SELECT
    document_id,
    COUNT(*) AS total_checks,
    COUNT(*) FILTER (WHERE check_status = 'PASS') AS passed,
    COUNT(*) FILTER (WHERE check_status = 'REVIEW') AS needs_review,
    COUNT(*) FILTER (WHERE check_status = 'MISSING') AS missing
FROM enercal_crosschecks_view
GROUP BY document_id;

COMMIT;
