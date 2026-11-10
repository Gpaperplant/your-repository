
-- 007: Additional EnerCal specifications.

BEGIN;

-- Store unique extracted fields from each PDF.
CREATE TABLE IF NOT EXISTS enercal_specifications (
    document_id BIGINT NOT NULL
        REFERENCES source_documents(document_id) ON DELETE CASCADE,

    field_name TEXT NOT NULL,
    field_value TEXT NOT NULL,
    unit TEXT,
    source_page INTEGER NOT NULL CHECK (source_page >= 1),

    PRIMARY KEY (document_id, field_name)
);

-- Speed up document field lookups.
CREATE INDEX IF NOT EXISTS idx_enercal_specs_document_page
    ON enercal_specifications (document_id, source_page);

-- Show extracted specifications.
CREATE OR REPLACE VIEW enercal_observations_view AS
SELECT
    document_id,
    field_name,
    field_value,
    unit,
    source_page
FROM enercal_specifications;

COMMIT;
