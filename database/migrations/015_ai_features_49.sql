BEGIN;

CREATE OR REPLACE VIEW fyp_ai_eligible_revisions AS
WITH latest_documents AS (
    SELECT DISTINCT ON (d.case_id, d.document_type)
        d.case_id, m.case_ref, e.elevator_ref, d.document_id,
        d.document_type, d.source_classification
    FROM project_documents d
    JOIN modernisation_cases m ON m.case_id = d.case_id
    JOIN elevators e ON e.elevator_id = m.elevator_id
    WHERE d.document_type IN ('mod_pdf', 'iso_excel')
    ORDER BY d.case_id, d.document_type, d.document_id DESC
), latest_revisions AS (
    SELECT d.*, r.revision_id, r.review_status, r.review_scope
    FROM latest_documents d
    JOIN LATERAL (
        SELECT revision_id, review_status, review_scope
        FROM document_revisions
        WHERE document_id = d.document_id
        ORDER BY revision_no DESC
        LIMIT 1
    ) r ON TRUE
)
SELECT d.case_id, d.case_ref, d.elevator_ref, d.document_id,
       d.document_type, d.revision_id, d.source_classification
FROM latest_revisions d
WHERE d.source_classification = 'real'
  AND d.review_status = 'approved'
  AND d.review_scope = 'full_values'
  AND EXISTS (
      SELECT 1 FROM engineering_values v
      WHERE v.revision_id = d.revision_id
  )
  AND NOT EXISTS (
      SELECT 1 FROM engineering_values v
      WHERE v.revision_id = d.revision_id
        AND (
            NOT v.source_verified
            OR v.value_kind = 'synthetic_input'
            OR (d.document_type = 'iso_excel'
                AND COALESCE(v.source_note, '') ILIKE '%synthetic before%')
        )
  );

CREATE OR REPLACE VIEW fyp_ai_mod_features_26 AS
WITH p AS (
    SELECT s.case_id, s.case_ref, s.elevator_ref, s.document_id,
           s.revision_id, s.source_classification,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'nominal_load_kg') AS load_existing,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'nominal_speed_m_s') AS speed_existing,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'proposed' AND v.field_name = 'nominal_speed_m_s') AS speed_proposed,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'common' AND v.field_name = 'travel_distance_m') AS travel,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'common' AND v.field_name = 'served_floors') AS floors,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'common' AND v.field_name = 'starts_per_year') AS starts,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'common' AND v.field_name = 'operating_days_per_year') AS days,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'common' AND v.field_name = 'iso_usage_category') AS usage,
    MAX(v.text_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'gear') AS gear_existing,
    MAX(v.text_value) FILTER (WHERE v.phase = 'proposed' AND v.field_name = 'gear') AS gear_proposed,
    MAX(v.text_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'drive_type') AS drive_existing,
    MAX(v.text_value) FILTER (WHERE v.phase = 'proposed' AND v.field_name = 'drive_type') AS drive_proposed,
    MAX(v.text_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'roping_suspension_type') AS roping_existing,
    MAX(v.text_value) FILTER (WHERE v.phase = 'proposed' AND v.field_name = 'roping_suspension_type') AS roping_proposed,
    MAX(v.text_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'regenerative_drive') AS regen_existing,
    MAX(v.text_value) FILTER (WHERE v.phase = 'proposed' AND v.field_name = 'regenerative_drive') AS regen_proposed,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'annual_energy_consumption') AS energy_existing,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'proposed' AND v.field_name = 'annual_energy_consumption') AS energy_proposed
    FROM fyp_ai_eligible_revisions s
    JOIN engineering_values v ON v.revision_id = s.revision_id
    WHERE s.document_type = 'mod_pdf'
    GROUP BY s.case_id, s.case_ref, s.elevator_ref, s.document_id,
             s.revision_id, s.source_classification
), e AS (
    SELECT p.*,
        CASE
            WHEN LOWER(drive_existing) LIKE '%pmsm%'
              OR LOWER(drive_existing) LIKE '%permanent magnet%' THEN 'PMSM'
            WHEN LOWER(drive_existing) LIKE '%induction%' THEN 'Induction'
        END AS motor_existing,
        CASE
            WHEN LOWER(drive_proposed) LIKE '%pmsm%'
              OR LOWER(drive_proposed) LIKE '%permanent magnet%' THEN 'PMSM'
            WHEN LOWER(drive_proposed) LIKE '%induction%' THEN 'Induction'
        END AS motor_proposed,
        CASE WHEN LOWER(BTRIM(regen_existing)) IN ('yes', 'true', '1') THEN 1
             WHEN LOWER(BTRIM(regen_existing)) IN ('no', 'false', '0') THEN 0
        END AS regen_existing_int,
        CASE WHEN LOWER(BTRIM(regen_proposed)) IN ('yes', 'true', '1') THEN 1
             WHEN LOWER(BTRIM(regen_proposed)) IN ('no', 'false', '0') THEN 0
        END AS regen_proposed_int
    FROM p
), f AS (
    SELECT case_id, case_ref, elevator_ref,
           document_id AS mod_document_id, revision_id AS mod_revision_id,
           source_classification,
           load_existing AS "MOD_rated_load_kg",
           speed_existing AS "MOD_speed_existing_mps",
           speed_proposed AS "MOD_speed_proposed_mps",
           travel AS "MOD_travel_distance_m",
           floors AS "MOD_served_floors",
           starts AS "MOD_starts_per_year",
           days AS "MOD_operating_days_per_year",
           usage AS "MOD_usage_category",
           NULLIF(BTRIM(gear_existing), '') AS "MOD_gear_existing",
           NULLIF(BTRIM(gear_proposed), '') AS "MOD_gear_proposed",
           NULLIF(BTRIM(drive_existing), '') AS "MOD_drive_type_existing",
           NULLIF(BTRIM(drive_proposed), '') AS "MOD_drive_type_proposed",
           motor_existing AS "MOD_motor_technology_existing",
           motor_proposed AS "MOD_motor_technology_proposed",
           NULLIF(BTRIM(roping_existing), '') AS "MOD_roping_existing",
           NULLIF(BTRIM(roping_proposed), '') AS "MOD_roping_proposed",
           regen_existing_int AS "MOD_regeneration_existing",
           regen_proposed_int AS "MOD_regeneration_proposed",
           energy_existing AS "MOD_PROXY_energy_existing_kwh_year",
           energy_proposed AS "MOD_PROXY_energy_proposed_kwh_year",
           100 * (energy_existing - energy_proposed)
               / NULLIF(energy_existing, 0) AS "MOD_PROXY_savings_pct",
           speed_proposed - speed_existing AS "DERIVED_speed_increase_mps",
           100 * (speed_proposed - speed_existing)
               / NULLIF(speed_existing, 0) AS "DERIVED_speed_increase_pct",
           CASE WHEN motor_existing = 'Induction' AND motor_proposed = 'PMSM'
                THEN 1 ELSE 0 END AS "DERIVED_induction_to_pmsm",
           CASE WHEN LOWER(BTRIM(gear_existing)) = 'geared'
                 AND LOWER(BTRIM(gear_proposed)) = 'gearless'
                THEN 1 ELSE 0 END AS "DERIVED_geared_to_gearless",
           CASE WHEN regen_existing_int = 0 AND regen_proposed_int = 1
                THEN 1 ELSE 0 END AS "DERIVED_regeneration_added"
    FROM e
)
SELECT * FROM f
WHERE "MOD_rated_load_kg" > 0
  AND "MOD_speed_existing_mps" > 0
  AND "MOD_speed_proposed_mps" > 0
  AND "MOD_PROXY_energy_existing_kwh_year" > 0
  AND "MOD_PROXY_energy_proposed_kwh_year" >= 0
  AND "MOD_travel_distance_m" >= 0
  AND "MOD_served_floors" > 0
  AND "MOD_starts_per_year" >= 0
  AND "MOD_operating_days_per_year" > 0
  AND "MOD_usage_category" > 0
  AND "MOD_rated_load_kg" IS NOT NULL AND
  "MOD_speed_existing_mps" IS NOT NULL AND
  "MOD_speed_proposed_mps" IS NOT NULL AND
  "MOD_travel_distance_m" IS NOT NULL AND
  "MOD_served_floors" IS NOT NULL AND
  "MOD_starts_per_year" IS NOT NULL AND
  "MOD_operating_days_per_year" IS NOT NULL AND
  "MOD_usage_category" IS NOT NULL AND
  "MOD_gear_existing" IS NOT NULL AND
  "MOD_gear_proposed" IS NOT NULL AND
  "MOD_drive_type_existing" IS NOT NULL AND
  "MOD_drive_type_proposed" IS NOT NULL AND
  "MOD_motor_technology_existing" IS NOT NULL AND
  "MOD_motor_technology_proposed" IS NOT NULL AND
  "MOD_roping_existing" IS NOT NULL AND
  "MOD_roping_proposed" IS NOT NULL AND
  "MOD_regeneration_existing" IS NOT NULL AND
  "MOD_regeneration_proposed" IS NOT NULL AND
  "MOD_PROXY_energy_existing_kwh_year" IS NOT NULL AND
  "MOD_PROXY_energy_proposed_kwh_year" IS NOT NULL AND
  "MOD_PROXY_savings_pct" IS NOT NULL AND
  "DERIVED_speed_increase_mps" IS NOT NULL AND
  "DERIVED_speed_increase_pct" IS NOT NULL AND
  "DERIVED_induction_to_pmsm" IS NOT NULL AND
  "DERIVED_geared_to_gearless" IS NOT NULL AND
  "DERIVED_regeneration_added" IS NOT NULL;

CREATE OR REPLACE VIEW fyp_ai_iso_pre_features_23 AS
WITH p AS (
    SELECT s.case_id, s.case_ref, s.elevator_ref, s.document_id,
           s.revision_id, s.source_classification,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'common' AND v.field_name = 'lift_landing_floors') AS iso_01,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'common' AND v.field_name = 'counterbalance_percent') AS iso_02,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'common' AND v.field_name = 'acceleration_m_s2') AS iso_03,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'common' AND v.field_name = 'jerk_m_s3') AS iso_04,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'common' AND v.field_name = 'door_time_s') AS iso_05,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'daily_trips_actual') AS iso_06,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'reference_energy_wh') AS iso_07,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'short_cycle_distance_m') AS iso_08,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'short_energy_wh') AS iso_09,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'idle_power_w') AS iso_10,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'standby5_power_w') AS iso_11,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'standby30_power_w') AS iso_12,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'reference_main_energy_wh') AS iso_13,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'reference_ancillary_energy_wh') AS iso_14,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'short_main_energy_wh') AS iso_15,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'short_ancillary_energy_wh') AS iso_16,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'energy_per_m_wh') AS iso_17,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'start_stop_energy_wh') AS iso_18,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'average_cycle_energy_wh') AS iso_19,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'daily_running_energy_wh') AS iso_20,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'daily_nonrunning_energy_wh') AS iso_21,
    MAX(v.numeric_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'annual_energy_kwh') AS iso_22,
    MAX(v.text_value) FILTER (WHERE v.phase = 'existing' AND v.field_name = 'efficiency_class') AS iso_23
    FROM fyp_ai_eligible_revisions s
    JOIN engineering_values v ON v.revision_id = s.revision_id
    WHERE s.document_type = 'iso_excel'
    GROUP BY s.case_id, s.case_ref, s.elevator_ref, s.document_id,
             s.revision_id, s.source_classification
), f AS (
    SELECT case_id, case_ref, elevator_ref,
           document_id AS iso_document_id, revision_id AS iso_revision_id,
           source_classification,
           iso_01 AS "ISO_PRE_landing_floors",
           iso_02 AS "ISO_PRE_counterbalance_pct",
           iso_03 AS "ISO_PRE_acceleration_mps2",
           iso_04 AS "ISO_PRE_jerk_mps3",
           iso_05 AS "ISO_PRE_door_operation_time_s",
           iso_06 AS "ISO_PRE_daily_trips",
           iso_07 AS "ISO_PRE_reference_cycle_wh",
           iso_08 AS "ISO_PRE_short_cycle_distance_m",
           iso_09 AS "ISO_PRE_short_cycle_wh",
           iso_10 AS "ISO_PRE_idle_power_w",
           iso_11 AS "ISO_PRE_standby_5min_w",
           iso_12 AS "ISO_PRE_standby_30min_w",
           iso_13 AS "ISO_PRE_reference_main_wh",
           iso_14 AS "ISO_PRE_reference_ancillary_wh",
           iso_15 AS "ISO_PRE_short_main_wh",
           iso_16 AS "ISO_PRE_short_ancillary_wh",
           iso_17 AS "ISO_PRE_running_energy_wh_per_m",
           iso_18 AS "ISO_PRE_start_stop_energy_wh",
           iso_19 AS "ISO_PRE_average_cycle_wh",
           iso_20 AS "ISO_PRE_running_wh_day",
           iso_21 AS "ISO_PRE_nonrunning_wh_day",
           iso_22 AS "ISO_PRE_annual_energy_kwh_year",
           NULLIF(REGEXP_REPLACE(UPPER(BTRIM(iso_23)), '^CLASS[[:space:]]+', ''), '') AS "ISO_PRE_energy_class"
    FROM p
)
SELECT * FROM f
WHERE "ISO_PRE_landing_floors" > 0
  AND "ISO_PRE_annual_energy_kwh_year" > 0
  AND "ISO_PRE_energy_class" IN ('A', 'B', 'C', 'D', 'E', 'F', 'G')
  AND "ISO_PRE_landing_floors" IS NOT NULL AND
  "ISO_PRE_counterbalance_pct" IS NOT NULL AND
  "ISO_PRE_acceleration_mps2" IS NOT NULL AND
  "ISO_PRE_jerk_mps3" IS NOT NULL AND
  "ISO_PRE_door_operation_time_s" IS NOT NULL AND
  "ISO_PRE_daily_trips" IS NOT NULL AND
  "ISO_PRE_reference_cycle_wh" IS NOT NULL AND
  "ISO_PRE_short_cycle_distance_m" IS NOT NULL AND
  "ISO_PRE_short_cycle_wh" IS NOT NULL AND
  "ISO_PRE_idle_power_w" IS NOT NULL AND
  "ISO_PRE_standby_5min_w" IS NOT NULL AND
  "ISO_PRE_standby_30min_w" IS NOT NULL AND
  "ISO_PRE_reference_main_wh" IS NOT NULL AND
  "ISO_PRE_reference_ancillary_wh" IS NOT NULL AND
  "ISO_PRE_short_main_wh" IS NOT NULL AND
  "ISO_PRE_short_ancillary_wh" IS NOT NULL AND
  "ISO_PRE_running_energy_wh_per_m" IS NOT NULL AND
  "ISO_PRE_start_stop_energy_wh" IS NOT NULL AND
  "ISO_PRE_average_cycle_wh" IS NOT NULL AND
  "ISO_PRE_running_wh_day" IS NOT NULL AND
  "ISO_PRE_nonrunning_wh_day" IS NOT NULL AND
  "ISO_PRE_annual_energy_kwh_year" IS NOT NULL AND
  "ISO_PRE_energy_class" IS NOT NULL
  AND "ISO_PRE_landing_floors" >= 0 AND
  "ISO_PRE_counterbalance_pct" >= 0 AND
  "ISO_PRE_acceleration_mps2" >= 0 AND
  "ISO_PRE_jerk_mps3" >= 0 AND
  "ISO_PRE_door_operation_time_s" >= 0 AND
  "ISO_PRE_daily_trips" >= 0 AND
  "ISO_PRE_reference_cycle_wh" >= 0 AND
  "ISO_PRE_short_cycle_distance_m" >= 0 AND
  "ISO_PRE_short_cycle_wh" >= 0 AND
  "ISO_PRE_idle_power_w" >= 0 AND
  "ISO_PRE_standby_5min_w" >= 0 AND
  "ISO_PRE_standby_30min_w" >= 0 AND
  "ISO_PRE_reference_main_wh" >= 0 AND
  "ISO_PRE_reference_ancillary_wh" >= 0 AND
  "ISO_PRE_short_main_wh" >= 0 AND
  "ISO_PRE_short_ancillary_wh" >= 0 AND
  "ISO_PRE_running_energy_wh_per_m" >= 0 AND
  "ISO_PRE_start_stop_energy_wh" >= 0 AND
  "ISO_PRE_average_cycle_wh" >= 0 AND
  "ISO_PRE_running_wh_day" >= 0 AND
  "ISO_PRE_nonrunning_wh_day" >= 0 AND
  "ISO_PRE_annual_energy_kwh_year" >= 0;

CREATE OR REPLACE VIEW fyp_ai_features_49 AS
SELECT m.case_id, m.case_ref, m.elevator_ref,
       m.mod_document_id, m.mod_revision_id,
       i.iso_document_id, i.iso_revision_id,
       m.source_classification,
       m."MOD_rated_load_kg",
       m."MOD_speed_existing_mps",
       m."MOD_speed_proposed_mps",
       m."MOD_travel_distance_m",
       m."MOD_served_floors",
       m."MOD_starts_per_year",
       m."MOD_operating_days_per_year",
       m."MOD_usage_category",
       m."MOD_gear_existing",
       m."MOD_gear_proposed",
       m."MOD_drive_type_existing",
       m."MOD_drive_type_proposed",
       m."MOD_motor_technology_existing",
       m."MOD_motor_technology_proposed",
       m."MOD_roping_existing",
       m."MOD_roping_proposed",
       m."MOD_regeneration_existing",
       m."MOD_regeneration_proposed",
       m."MOD_PROXY_energy_existing_kwh_year",
       m."MOD_PROXY_energy_proposed_kwh_year",
       m."MOD_PROXY_savings_pct",
       m."DERIVED_speed_increase_mps",
       m."DERIVED_speed_increase_pct",
       m."DERIVED_induction_to_pmsm",
       m."DERIVED_geared_to_gearless",
       m."DERIVED_regeneration_added",
       i."ISO_PRE_landing_floors",
       i."ISO_PRE_counterbalance_pct",
       i."ISO_PRE_acceleration_mps2",
       i."ISO_PRE_jerk_mps3",
       i."ISO_PRE_door_operation_time_s",
       i."ISO_PRE_daily_trips",
       i."ISO_PRE_reference_cycle_wh",
       i."ISO_PRE_short_cycle_distance_m",
       i."ISO_PRE_short_cycle_wh",
       i."ISO_PRE_idle_power_w",
       i."ISO_PRE_standby_5min_w",
       i."ISO_PRE_standby_30min_w",
       i."ISO_PRE_reference_main_wh",
       i."ISO_PRE_reference_ancillary_wh",
       i."ISO_PRE_short_main_wh",
       i."ISO_PRE_short_ancillary_wh",
       i."ISO_PRE_running_energy_wh_per_m",
       i."ISO_PRE_start_stop_energy_wh",
       i."ISO_PRE_average_cycle_wh",
       i."ISO_PRE_running_wh_day",
       i."ISO_PRE_nonrunning_wh_day",
       i."ISO_PRE_annual_energy_kwh_year",
       i."ISO_PRE_energy_class"
FROM fyp_ai_mod_features_26 m
JOIN fyp_ai_iso_pre_features_23 i ON i.case_id = m.case_id;

CREATE OR REPLACE VIEW fyp_ai_iso_targets AS
SELECT i.case_id, i.iso_revision_id,
       i."ISO_PRE_annual_energy_kwh_year" AS iso_before_kwh,
       a.numeric_value AS iso_after_kwh,
       100 * (i."ISO_PRE_annual_energy_kwh_year" - a.numeric_value)
           / i."ISO_PRE_annual_energy_kwh_year" AS iso_savings_pct
FROM fyp_ai_iso_pre_features_23 i
JOIN engineering_values a ON a.revision_id = i.iso_revision_id
                         AND a.phase = 'proposed'
                         AND a.field_name = 'annual_energy_kwh'
WHERE a.numeric_value IS NOT NULL AND a.numeric_value >= 0
  AND a.source_verified AND a.value_kind <> 'synthetic_input';

CREATE OR REPLACE VIEW fyp_ai_train_mod_26 AS
SELECT m.case_id, m.case_ref, m.elevator_ref,
       m.mod_document_id, m.mod_revision_id, t.iso_revision_id,
       m.source_classification,
       m."MOD_rated_load_kg",
       m."MOD_speed_existing_mps",
       m."MOD_speed_proposed_mps",
       m."MOD_travel_distance_m",
       m."MOD_served_floors",
       m."MOD_starts_per_year",
       m."MOD_operating_days_per_year",
       m."MOD_usage_category",
       m."MOD_gear_existing",
       m."MOD_gear_proposed",
       m."MOD_drive_type_existing",
       m."MOD_drive_type_proposed",
       m."MOD_motor_technology_existing",
       m."MOD_motor_technology_proposed",
       m."MOD_roping_existing",
       m."MOD_roping_proposed",
       m."MOD_regeneration_existing",
       m."MOD_regeneration_proposed",
       m."MOD_PROXY_energy_existing_kwh_year",
       m."MOD_PROXY_energy_proposed_kwh_year",
       m."MOD_PROXY_savings_pct",
       m."DERIVED_speed_increase_mps",
       m."DERIVED_speed_increase_pct",
       m."DERIVED_induction_to_pmsm",
       m."DERIVED_geared_to_gearless",
       m."DERIVED_regeneration_added",
       t.iso_savings_pct - m."MOD_PROXY_savings_pct"
           AS "TARGET_savings_gap_pp"
FROM fyp_ai_mod_features_26 m
JOIN fyp_ai_iso_targets t ON t.case_id = m.case_id;

CREATE OR REPLACE VIEW fyp_ai_train_49 AS
SELECT f.case_id, f.case_ref, f.elevator_ref,
       f.mod_document_id, f.mod_revision_id,
       f.iso_document_id, f.iso_revision_id,
       f.source_classification,
       f."MOD_rated_load_kg",
       f."MOD_speed_existing_mps",
       f."MOD_speed_proposed_mps",
       f."MOD_travel_distance_m",
       f."MOD_served_floors",
       f."MOD_starts_per_year",
       f."MOD_operating_days_per_year",
       f."MOD_usage_category",
       f."MOD_gear_existing",
       f."MOD_gear_proposed",
       f."MOD_drive_type_existing",
       f."MOD_drive_type_proposed",
       f."MOD_motor_technology_existing",
       f."MOD_motor_technology_proposed",
       f."MOD_roping_existing",
       f."MOD_roping_proposed",
       f."MOD_regeneration_existing",
       f."MOD_regeneration_proposed",
       f."MOD_PROXY_energy_existing_kwh_year",
       f."MOD_PROXY_energy_proposed_kwh_year",
       f."MOD_PROXY_savings_pct",
       f."DERIVED_speed_increase_mps",
       f."DERIVED_speed_increase_pct",
       f."DERIVED_induction_to_pmsm",
       f."DERIVED_geared_to_gearless",
       f."DERIVED_regeneration_added",
       f."ISO_PRE_landing_floors",
       f."ISO_PRE_counterbalance_pct",
       f."ISO_PRE_acceleration_mps2",
       f."ISO_PRE_jerk_mps3",
       f."ISO_PRE_door_operation_time_s",
       f."ISO_PRE_daily_trips",
       f."ISO_PRE_reference_cycle_wh",
       f."ISO_PRE_short_cycle_distance_m",
       f."ISO_PRE_short_cycle_wh",
       f."ISO_PRE_idle_power_w",
       f."ISO_PRE_standby_5min_w",
       f."ISO_PRE_standby_30min_w",
       f."ISO_PRE_reference_main_wh",
       f."ISO_PRE_reference_ancillary_wh",
       f."ISO_PRE_short_main_wh",
       f."ISO_PRE_short_ancillary_wh",
       f."ISO_PRE_running_energy_wh_per_m",
       f."ISO_PRE_start_stop_energy_wh",
       f."ISO_PRE_average_cycle_wh",
       f."ISO_PRE_running_wh_day",
       f."ISO_PRE_nonrunning_wh_day",
       f."ISO_PRE_annual_energy_kwh_year",
       f."ISO_PRE_energy_class",
       t.iso_savings_pct - f."MOD_PROXY_savings_pct"
           AS "TARGET_savings_gap_pp"
FROM fyp_ai_features_49 f
JOIN fyp_ai_iso_targets t ON t.case_id = f.case_id
                         AND t.iso_revision_id = f.iso_revision_id;

COMMIT;
