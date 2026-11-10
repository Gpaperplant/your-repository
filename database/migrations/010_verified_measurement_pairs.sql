
BEGIN;

-- Keep manual before/after matching separate from the original ISO observations.
CREATE TABLE IF NOT EXISTS verified_measurement_pairs (
    pair_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    case_id BIGINT NOT NULL REFERENCES modernisation_cases(case_id),
    document_id BIGINT NOT NULL REFERENCES source_documents(document_id),
    before_observation_id BIGINT NOT NULL REFERENCES energy_observations(observation_id),
    after_observation_id BIGINT NOT NULL REFERENCES energy_observations(observation_id),
    before_days INTEGER NOT NULL CHECK (before_days > 0),
    after_days INTEGER NOT NULL CHECK (after_days > 0),
    conditions_verified BOOLEAN NOT NULL DEFAULT FALSE,
    period_energy_verified BOOLEAN NOT NULL DEFAULT FALSE,
    specifications_verified BOOLEAN NOT NULL DEFAULT FALSE,
    review_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (review_status IN ('pending', 'approved', 'rejected')),
    review_note TEXT,
    reviewed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (case_id),
    CHECK (before_observation_id <> after_observation_id),
    CHECK (
        review_status <> 'approved'
        OR (
            conditions_verified
            AND period_energy_verified
            AND reviewed_at IS NOT NULL
            AND NULLIF(BTRIM(review_note), '') IS NOT NULL
        )
    )
);

-- Expose only independently reviewed, same-elevator, actual period measurements.
CREATE OR REPLACE VIEW ai_verified_measurement_pairs_view AS
SELECT
    p.pair_id,
    p.document_id,
    p.case_id,
    m.elevator_id,
    p.review_status,
    'metered'::TEXT AS before_basis,
    'metered'::TEXT AS after_basis,
    'kWh'::TEXT AS before_unit,
    'kWh'::TEXT AS after_unit,
    p.conditions_verified,
    p.specifications_verified,
    before_value.source_id AS before_source_id,
    after_value.source_id AS after_source_id,
    before_value.numeric_value AS before_kwh,
    after_value.numeric_value AS after_kwh,
    p.before_days,
    p.after_days
FROM verified_measurement_pairs p
JOIN modernisation_cases m ON m.case_id = p.case_id
JOIN source_documents d ON d.document_id = p.document_id
JOIN energy_observations before_value
    ON before_value.observation_id = p.before_observation_id
JOIN energy_observations after_value
    ON after_value.observation_id = p.after_observation_id
JOIN measurement_sources before_source
    ON before_source.source_id = before_value.source_id
JOIN measurement_sources after_source
    ON after_source.source_id = after_value.source_id
WHERE p.review_status = 'approved'
  AND p.conditions_verified
  AND p.period_energy_verified
  AND m.status = 'completed'
  AND d.case_id = p.case_id
  AND before_source.review_status = 'approved'
  AND after_source.review_status = 'approved'
  AND before_source.verified_elevator_id = m.elevator_id
  AND after_source.verified_elevator_id = m.elevator_id
  AND before_value.source_id <> after_value.source_id
  AND LOWER(TRIM(before_value.value_kind)) IN ('measured', 'metered')
  AND LOWER(TRIM(after_value.value_kind)) IN ('measured', 'metered')
  AND LOWER(TRIM(before_value.unit)) = 'kwh'
  AND LOWER(TRIM(after_value.unit)) = 'kwh'
  AND before_value.metric_name = after_value.metric_name
  AND before_value.numeric_value > 0
  AND after_value.numeric_value >= 0
  AND before_value.period_start IS NOT NULL
  AND before_value.period_end IS NOT NULL
  AND after_value.period_start IS NOT NULL
  AND after_value.period_end IS NOT NULL
  AND before_value.period_start <= before_value.period_end
  AND after_value.period_start <= after_value.period_end
  AND before_value.period_end < after_value.period_start;

COMMIT;
