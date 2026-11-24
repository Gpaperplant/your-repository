BEGIN;

CREATE TABLE project_documents (
    document_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id BIGINT NOT NULL REFERENCES modernisation_cases(case_id),
    document_type TEXT NOT NULL CHECK (
        document_type IN ('mod_pdf', 'iso_excel', 'iso_test_pdf', 'meter_csv')
    ),
    source_classification TEXT NOT NULL DEFAULT 'unknown' CHECK (
        source_classification IN ('real', 'synthetic', 'test_fixture', 'unknown')
    ),
    reported_elevator_ref TEXT,
    filename TEXT NOT NULL CHECK (BTRIM(filename) <> ''),
    file_sha256 TEXT NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    file_content BYTEA NOT NULL CHECK (OCTET_LENGTH(file_content) > 0),
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (case_id, document_type, file_sha256),
    CHECK (file_sha256 = ENCODE(SHA256(file_content), 'hex')),
    CHECK (
        document_type <> 'iso_excel'
        OR NULLIF(BTRIM(COALESCE(reported_elevator_ref, '')), '') IS NOT NULL
    )
);

CREATE INDEX idx_project_documents_case_latest
    ON project_documents(case_id, document_type, document_id DESC);

CREATE TABLE document_revisions (
    revision_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id BIGINT NOT NULL REFERENCES project_documents(document_id),
    revision_no INTEGER NOT NULL CHECK (revision_no >= 1),
    review_scope TEXT NOT NULL DEFAULT 'full_values' CHECK (
        review_scope IN ('full_values', 'comparison_only')
    ),
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        review_status IN (
            'pending', 'approved', 'rejected',
            'changes_requested', 'further_verification_required'
        )
    ),
    review_note TEXT,
    reviewed_by TEXT,
    reviewer_authenticated BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    UNIQUE (document_id, revision_no),
    CHECK (review_status = 'pending' OR reviewed_at IS NOT NULL),
    CHECK (
        NOT reviewer_authenticated
        OR NULLIF(BTRIM(COALESCE(reviewed_by, '')), '') IS NOT NULL
    )
);

CREATE INDEX idx_document_revisions_document_latest
    ON document_revisions(document_id, revision_no DESC);

CREATE TABLE engineering_values (
    value_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    revision_id BIGINT NOT NULL REFERENCES document_revisions(revision_id),
    field_name TEXT NOT NULL CHECK (BTRIM(field_name) <> ''),
    phase TEXT NOT NULL CHECK (phase IN ('existing', 'proposed', 'common')),
    value_kind TEXT NOT NULL CHECK (
        value_kind IN (
            'identity', 'engineering_specification', 'test_measurement',
            'meter_measurement', 'assumption', 'synthetic_input',
            'calculated_estimate', 'reported_estimate', 'metadata'
        )
    ),
    raw_source_value TEXT,
    numeric_value NUMERIC,
    text_value TEXT,
    unit TEXT,
    source_page INTEGER CHECK (source_page > 0),
    excel_sheet TEXT,
    excel_cell TEXT,
    excel_formula TEXT,
    source_label TEXT,
    source_note TEXT,
    measured_on DATE,
    period_start DATE,
    period_end DATE,
    source_verified BOOLEAN NOT NULL DEFAULT FALSE,
    is_corrected BOOLEAN NOT NULL DEFAULT FALSE,
    correction_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (revision_id, phase, field_name),
    CHECK (numeric_value IS NULL OR text_value IS NULL),
    CHECK (
        numeric_value IS NOT NULL OR text_value IS NOT NULL
        OR raw_source_value IS NOT NULL
    ),
    CHECK (
        NOT is_corrected
        OR NULLIF(BTRIM(COALESCE(correction_reason, '')), '') IS NOT NULL
    ),
    CHECK (
        (period_start IS NULL AND period_end IS NULL)
        OR (period_start IS NOT NULL AND period_end IS NOT NULL AND period_end >= period_start)
    ),
    CHECK (value_kind <> 'meter_measurement' OR period_start IS NOT NULL)
);

CREATE INDEX idx_engineering_values_revision
    ON engineering_values(revision_id, phase, field_name);

CREATE TABLE document_reviews (
    review_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    revision_id BIGINT NOT NULL REFERENCES document_revisions(revision_id),
    action TEXT NOT NULL CHECK (
        action IN (
            'revision_submitted', 'approved', 'rejected',
            'changes_requested', 'further_verification_required'
        )
    ),
    previous_status TEXT,
    new_status TEXT NOT NULL,
    reason TEXT,
    reviewer_id TEXT,
    reviewer_authenticated BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        NOT reviewer_authenticated
        OR NULLIF(BTRIM(COALESCE(reviewer_id, '')), '') IS NOT NULL
    )
);

CREATE INDEX idx_document_reviews_revision
    ON document_reviews(revision_id, review_id);

CREATE FUNCTION fyp_check_document_upload() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    registered_ref TEXT;
BEGIN
    SELECT e.elevator_ref INTO registered_ref
    FROM modernisation_cases m
    JOIN elevators e ON e.elevator_id = m.elevator_id
    WHERE m.case_id = NEW.case_id
    FOR UPDATE OF m;

    IF NEW.document_type = 'iso_excel'
       AND NOT (
           NEW.source_classification = 'test_fixture'
           AND registered_ref LIKE 'TEST-LIFT-%'
       )
       AND REGEXP_REPLACE(UPPER(registered_ref), '[^A-Z0-9]', '', 'g') <>
           REGEXP_REPLACE(UPPER(NEW.reported_elevator_ref), '[^A-Z0-9]', '', 'g')
    THEN
        RAISE EXCEPTION 'ISO lift ID does not match the case elevator';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_fyp_document_upload
BEFORE INSERT ON project_documents
FOR EACH ROW EXECUTE FUNCTION fyp_check_document_upload();

CREATE FUNCTION fyp_block_source_changes() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Original evidence cannot be changed or deleted';
END;
$$;

CREATE TRIGGER trg_fyp_document_immutable
BEFORE UPDATE OR DELETE ON project_documents
FOR EACH ROW EXECUTE FUNCTION fyp_block_source_changes();

CREATE FUNCTION fyp_check_revision_insert() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    last_number INTEGER;
    last_status TEXT;
    v_case_id BIGINT;
BEGIN
    IF NEW.review_status <> 'pending' OR NEW.reviewed_at IS NOT NULL
       OR NEW.reviewed_by IS NOT NULL THEN
        RAISE EXCEPTION 'New revisions must start pending';
    END IF;

    SELECT case_id INTO v_case_id
    FROM project_documents WHERE document_id = NEW.document_id;

    PERFORM 1 FROM modernisation_cases
    WHERE case_id = v_case_id FOR UPDATE;

    SELECT revision_no, review_status INTO last_number, last_status
    FROM document_revisions
    WHERE document_id = NEW.document_id
    ORDER BY revision_no DESC LIMIT 1;

    IF NEW.revision_no <> COALESCE(last_number, 0) + 1 THEN
        RAISE EXCEPTION 'Revision numbers must be consecutive';
    END IF;

    IF last_status = 'pending' THEN
        RAISE EXCEPTION 'Review the previous revision before creating another';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_fyp_revision_insert
BEFORE INSERT ON document_revisions
FOR EACH ROW EXECUTE FUNCTION fyp_check_revision_insert();

CREATE FUNCTION fyp_log_revision_submitted() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO document_reviews (revision_id, action, new_status)
    VALUES (NEW.revision_id, 'revision_submitted', 'pending');
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_fyp_revision_created
AFTER INSERT ON document_revisions
FOR EACH ROW EXECUTE FUNCTION fyp_log_revision_submitted();

CREATE FUNCTION fyp_check_value_insert() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_document_id BIGINT;
    v_status TEXT;
    v_latest_id BIGINT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Engineering values are immutable; create a new revision';
    END IF;

    SELECT document_id, review_status INTO v_document_id, v_status
    FROM document_revisions
    WHERE revision_id = NEW.revision_id
    FOR UPDATE;

    SELECT revision_id INTO v_latest_id
    FROM document_revisions
    WHERE document_id = v_document_id
    ORDER BY revision_no DESC LIMIT 1;

    IF v_status <> 'pending' OR v_latest_id <> NEW.revision_id THEN
        RAISE EXCEPTION 'Only the latest pending revision accepts values';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_fyp_value_immutable
BEFORE INSERT OR UPDATE OR DELETE ON engineering_values
FOR EACH ROW EXECUTE FUNCTION fyp_check_value_insert();

CREATE FUNCTION fyp_check_revision_review() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
    v_case_id BIGINT;
    v_type TEXT;
    v_latest_document BIGINT;
    v_latest_revision BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Revision history cannot be deleted';
    END IF;

    IF (NEW.document_id, NEW.revision_no, NEW.review_scope, NEW.created_at)
       IS DISTINCT FROM
       (OLD.document_id, OLD.revision_no, OLD.review_scope, OLD.created_at) THEN
        RAISE EXCEPTION 'Revision identity is immutable';
    END IF;

    IF OLD.review_status <> 'pending' OR NEW.review_status NOT IN (
        'approved', 'rejected', 'changes_requested',
        'further_verification_required'
    ) THEN
        RAISE EXCEPTION 'Invalid review transition';
    END IF;

    SELECT case_id, document_type INTO v_case_id, v_type
    FROM project_documents WHERE document_id = NEW.document_id;

    SELECT document_id INTO v_latest_document
    FROM project_documents
    WHERE case_id = v_case_id AND document_type = v_type
    ORDER BY document_id DESC LIMIT 1;

    SELECT revision_id INTO v_latest_revision
    FROM document_revisions
    WHERE document_id = NEW.document_id
    ORDER BY revision_no DESC LIMIT 1;

    IF v_latest_document <> NEW.document_id
       OR v_latest_revision <> NEW.revision_id THEN
        RAISE EXCEPTION 'Only the newest document revision can be reviewed';
    END IF;

    IF NEW.reviewed_at IS NULL THEN
        RAISE EXCEPTION 'Review timestamp is required';
    END IF;

    IF NEW.review_status <> 'approved'
       AND NULLIF(BTRIM(COALESCE(NEW.review_note, '')), '') IS NULL THEN
        RAISE EXCEPTION 'A review reason is required';
    END IF;

    IF NEW.review_status = 'approved' THEN
        IF NOT EXISTS (
            SELECT 1 FROM engineering_values
            WHERE revision_id = NEW.revision_id
        ) THEN
            RAISE EXCEPTION 'An empty revision cannot be approved';
        END IF;

        IF NEW.review_scope = 'full_values' AND EXISTS (
            SELECT 1 FROM engineering_values
            WHERE revision_id = NEW.revision_id AND NOT source_verified
        ) THEN
            RAISE EXCEPTION 'Full approval requires verified source values';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_fyp_revision_review
BEFORE UPDATE OR DELETE ON document_revisions
FOR EACH ROW EXECUTE FUNCTION fyp_check_revision_review();

CREATE FUNCTION fyp_log_review() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO document_reviews (
        revision_id, action, previous_status, new_status,
        reason, reviewer_id, reviewer_authenticated
    ) VALUES (
        NEW.revision_id, NEW.review_status, OLD.review_status, NEW.review_status,
        NEW.review_note, NEW.reviewed_by, NEW.reviewer_authenticated
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_fyp_review_logged
AFTER UPDATE ON document_revisions
FOR EACH ROW EXECUTE FUNCTION fyp_log_review();

CREATE FUNCTION fyp_block_review_changes() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Review events are append-only';
END;
$$;

CREATE TRIGGER trg_fyp_review_immutable
BEFORE UPDATE OR DELETE ON document_reviews
FOR EACH ROW EXECUTE FUNCTION fyp_block_review_changes();

CREATE FUNCTION fyp_block_truncate() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Evidence history cannot be truncated';
END;
$$;

CREATE TRIGGER trg_fyp_documents_no_truncate
BEFORE TRUNCATE ON project_documents
FOR EACH STATEMENT EXECUTE FUNCTION fyp_block_truncate();

CREATE TRIGGER trg_fyp_revisions_no_truncate
BEFORE TRUNCATE ON document_revisions
FOR EACH STATEMENT EXECUTE FUNCTION fyp_block_truncate();

CREATE TRIGGER trg_fyp_values_no_truncate
BEFORE TRUNCATE ON engineering_values
FOR EACH STATEMENT EXECUTE FUNCTION fyp_block_truncate();

CREATE TRIGGER trg_fyp_reviews_no_truncate
BEFORE TRUNCATE ON document_reviews
FOR EACH STATEMENT EXECUTE FUNCTION fyp_block_truncate();

CREATE FUNCTION review_document_revision(
    p_revision_id BIGINT,
    p_status TEXT,
    p_reason TEXT DEFAULT NULL,
    p_reviewer TEXT DEFAULT NULL,
    p_authenticated BOOLEAN DEFAULT FALSE
) RETURNS BIGINT
LANGUAGE plpgsql AS $$
DECLARE
    v_case_id BIGINT;
    v_review_id BIGINT;
BEGIN
    SELECT d.case_id INTO v_case_id
    FROM document_revisions r
    JOIN project_documents d ON d.document_id = r.document_id
    WHERE r.revision_id = p_revision_id;

    IF v_case_id IS NULL THEN
        RAISE EXCEPTION 'Revision not found';
    END IF;

    PERFORM 1 FROM modernisation_cases
    WHERE case_id = v_case_id FOR UPDATE;

    UPDATE document_revisions SET
        review_status = p_status,
        review_note = p_reason,
        reviewed_by = p_reviewer,
        reviewer_authenticated = p_authenticated,
        reviewed_at = NOW()
    WHERE revision_id = p_revision_id;

    SELECT review_id INTO v_review_id
    FROM document_reviews
    WHERE revision_id = p_revision_id
    ORDER BY review_id DESC LIMIT 1;
    RETURN v_review_id;
END;
$$;

COMMIT;
