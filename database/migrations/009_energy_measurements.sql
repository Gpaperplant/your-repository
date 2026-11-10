
-- Store independently sourced energy measurements and calculations
BEGIN;

-- Keep measurement files separate from EnerCal source documents
CREATE TABLE IF NOT EXISTS measurement_sources (
    source_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filename TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (
        source_type IN ('iso_excel', 'iso_test_pdf', 'meter_csv')
    ),
    file_sha256 CHAR(64) NOT NULL UNIQUE CHECK (
        file_sha256 ~ '^[a-f0-9]{64}$'
    ),
    file_size_bytes BIGINT NOT NULL CHECK (file_size_bytes > 0),
    file_bytes BYTEA NOT NULL,
    reported_elevator_ref VARCHAR(100),
    verified_elevator_id BIGINT REFERENCES elevators(elevator_id),
    review_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        review_status IN ('pending', 'approved', 'rejected')
    ),
    review_note TEXT,
    reviewed_at TIMESTAMPTZ,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (verified_elevator_id IS NULL OR review_status = 'approved'),
    CHECK (review_status <> 'approved' OR reviewed_at IS NOT NULL)
);

-- Preserve the raw source value and its measurement classification
CREATE TABLE IF NOT EXISTS energy_observations (
    observation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES measurement_sources(source_id) ON DELETE CASCADE,
    metric_name TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    numeric_value NUMERIC(18,6),
    unit TEXT,
    value_kind TEXT NOT NULL CHECK (
        value_kind IN (
            'engineering_input',
            'test_measurement',
            'iso_estimate',
            'enercal_estimate',
            'unverified_entry',
            'meter_measurement'
        )
    ),
    source_location TEXT NOT NULL,
    observed_on DATE,
    period_start DATE,
    period_end DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source_id, metric_name, source_location),
    CHECK (period_start IS NULL OR period_end IS NULL OR period_end >= period_start)
);

-- Support later queries for approved sources linked to a verified elevator
CREATE INDEX IF NOT EXISTS idx_measurement_sources_elevator
    ON measurement_sources (verified_elevator_id)
    WHERE verified_elevator_id IS NOT NULL;

COMMIT;
