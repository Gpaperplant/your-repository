-- Keep optional specifications separate from the reviewed 16-field comparison.
CREATE TABLE IF NOT EXISTS enercal_specifications (
    document_id BIGINT NOT NULL REFERENCES source_documents(document_id) ON DELETE CASCADE,
    field_name TEXT NOT NULL,
    field_value TEXT NOT NULL,
    unit TEXT,
    source_page INTEGER NOT NULL CHECK (source_page >= 1),
    PRIMARY KEY (document_id, field_name)
);
