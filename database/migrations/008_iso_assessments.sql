-- Keep ISO assessments separate from EnerCal report revisions.
CREATE TABLE IF NOT EXISTS iso_assessments (
    assessment_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id BIGINT NOT NULL REFERENCES modernisation_cases(case_id),
    lift_id TEXT NOT NULL,
    pdf_sha256 CHAR(64) NOT NULL,
    excel_sha256 CHAR(64) NOT NULL,
    comparisons JSONB NOT NULL DEFAULT '[]',
    imported_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (case_id, pdf_sha256, excel_sha256)
);

-- Preserve the original bytes of both supporting documents.
CREATE TABLE IF NOT EXISTS iso_documents (
    document_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    assessment_id BIGINT NOT NULL
        REFERENCES iso_assessments(assessment_id) ON DELETE CASCADE,
    document_type TEXT NOT NULL
        CHECK (document_type IN ('ee_pdf', 'iso_excel')),
    filename TEXT NOT NULL,
    file_sha256 CHAR(64) NOT NULL,
    content BYTEA NOT NULL,
    UNIQUE (assessment_id, document_type)
);

-- Keep even unique or conflicting fields with exact source locations.
CREATE TABLE IF NOT EXISTS iso_extracted_values (
    value_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id BIGINT NOT NULL
        REFERENCES iso_documents(document_id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    value_text TEXT,
    numeric_value NUMERIC,
    unit TEXT,
    data_kind TEXT NOT NULL,
    pdf_page INTEGER,
    excel_sheet TEXT,
    excel_cell TEXT,
    excel_formula TEXT,
    source_label TEXT,
    source_note TEXT,
    UNIQUE (document_id, field_name)
);

CREATE INDEX IF NOT EXISTS idx_iso_assessments_case
    ON iso_assessments(case_id);

CREATE INDEX IF NOT EXISTS idx_iso_values_document
    ON iso_extracted_values(document_id);