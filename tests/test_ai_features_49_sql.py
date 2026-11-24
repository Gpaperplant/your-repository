from pathlib import Path
import re

from services.iso_value_mapper import ISO_PRE_FEATURES, ISO_PRE_FIELDS
from services.mod_value_mapper import MOD_FEATURES

SQL = (
    Path(__file__).resolve().parents[1]
    / 'database/migrations/015_ai_features_49.sql'
).read_text()

VIEWS = (
    'fyp_ai_eligible_revisions',
    'fyp_ai_mod_features_26',
    'fyp_ai_iso_pre_features_23',
    'fyp_ai_features_49',
    'fyp_ai_iso_targets',
    'fyp_ai_train_mod_26',
    'fyp_ai_train_49',
)


def body(view):
    pattern = rf'CREATE OR REPLACE VIEW {view} AS\s*(.*?);'
    found = re.search(pattern, SQL, re.S)
    assert found, f'Missing view: {view}'
    return found.group(1)


def test_only_additive_views_inside_transaction():
    assert SQL.strip().startswith('BEGIN;')
    assert SQL.strip().endswith('COMMIT;')
    assert not re.search(r'\b(?:DROP|TRUNCATE|ALTER|DELETE|UPDATE|INSERT)\b', SQL, re.I)
    assert tuple(re.findall(r'CREATE OR REPLACE VIEW\s+(\w+)', SQL)) == VIEWS


def test_views_can_be_rerun():
    assert SQL.count('CREATE OR REPLACE VIEW') == len(VIEWS)


def test_only_latest_document_per_case_and_type():
    sql = body('fyp_ai_eligible_revisions')
    assert 'DISTINCT ON (d.case_id, d.document_type)' in sql
    assert 'ORDER BY d.case_id, d.document_type, d.document_id DESC' in sql
    assert 'ORDER BY revision_no DESC' in sql
    assert 'LIMIT 1' in sql


def test_training_requires_real_fully_approved_evidence():
    sql = body('fyp_ai_eligible_revisions')
    for expression in (
        "d.source_classification = 'real'",
        "d.review_status = 'approved'",
        "d.review_scope = 'full_values'",
        'NOT v.source_verified',
        "v.value_kind = 'synthetic_input'",
        'NOT EXISTS',
    ):
        assert expression in sql


def test_excludes_synthetic_before_notes():
    assert "ILIKE '%synthetic before%'" in body('fyp_ai_eligible_revisions')


def test_mod_features_exactly_26_and_correct_order():
    sql = body('fyp_ai_mod_features_26')
    actual = re.findall(r' AS "(MOD_[^"]+|DERIVED_[^"]+)"', sql)
    assert len(actual) == 26
    assert tuple(actual) == MOD_FEATURES


def test_iso_features_exactly_23_and_correct_order():
    sql = body('fyp_ai_iso_pre_features_23')
    actual = re.findall(r' AS "(ISO_PRE_[^"]+)"', sql)
    assert len(actual) == 23
    assert tuple(actual) == ISO_PRE_FEATURES


def test_iso_phase_source_mapping_matches_python():
    sql = body('fyp_ai_iso_pre_features_23')
    matches = re.findall(
        r"FILTER \(WHERE v.phase = '(\w+)' AND v.field_name = '([\w.]+)'\) AS iso_\d+",
        sql,
    )
    assert matches == [(phase, field) for _, phase, field in ISO_PRE_FIELDS]
    assert not any(phase == 'proposed' for phase, _ in matches)


def test_49_feature_view_matches_model_workbook_order():
    sql = body('fyp_ai_features_49')
    actual = re.findall(r'(?:m|i)\.\"(MOD_[^\"]+|DERIVED_[^\"]+|ISO_PRE_[^\"]+)\"', sql)
    assert len(actual) == 49
    assert tuple(actual) == MOD_FEATURES + ISO_PRE_FEATURES


def test_mod_26_model_does_not_require_iso_before():
    sql = body('fyp_ai_mod_features_26')
    assert 'fyp_ai_iso_pre_features_23' not in sql
    assert "s.document_type = 'mod_pdf'" in sql


def test_49_prediction_joins_same_case_not_ambiguous_document():
    sql = body('fyp_ai_features_49')
    assert 'JOIN fyp_ai_iso_pre_features_23 i ON i.case_id = m.case_id' in sql
    assert 'mod_document_id' in sql and 'iso_document_id' in sql


def test_no_after_values_in_prediction_views():
    for name in ('fyp_ai_mod_features_26', 'fyp_ai_iso_pre_features_23', 'fyp_ai_features_49'):
        sql = body(name)
        assert "a.phase = 'proposed'" not in sql
        assert 'iso_after_kwh' not in sql
        assert 'TARGET_savings_gap_pp' not in sql


def test_iso_after_only_for_label_not_feature():
    sql = body('fyp_ai_iso_targets')
    assert "a.phase = 'proposed'" in sql
    assert "a.field_name = 'annual_energy_kwh'" in sql
    assert 'a.source_verified' in sql
    assert "a.value_kind <> 'synthetic_input'" in sql
    assert 'a.numeric_value >= 0' in sql


def test_target_is_gap_in_percentage_points():
    targets = body('fyp_ai_iso_targets')
    assert '100 * (i."ISO_PRE_annual_energy_kwh_year" - a.numeric_value)' in targets
    for name in ('fyp_ai_train_mod_26', 'fyp_ai_train_49'):
        sql = body(name)
        assert '"TARGET_savings_gap_pp"' in sql
        assert 'iso_savings_pct' in sql
        assert '"MOD_PROXY_savings_pct"' in sql
        assert 'iso_after_kwh' not in sql


def test_latest_approved_not_previous_approved_if_new_upload_pending():
    sql = body('fyp_ai_eligible_revisions')
    assert sql.index('DISTINCT ON') < sql.index("d.review_status = 'approved'")
    assert sql.index('latest_revisions AS') < sql.index("d.review_status = 'approved'")


def test_derived_feature_rules_match_python():
    sql = body('fyp_ai_mod_features_26')
    for phrase in (
        "LIKE '%pmsm%'", "LIKE '%permanent magnet%'",
        "LIKE '%induction%'", "IN ('yes', 'true', '1')",
        "IN ('no', 'false', '0')",
        "AS \"DERIVED_speed_increase_mps\"",
        "AS \"DERIVED_speed_increase_pct\"",
        "AS \"DERIVED_geared_to_gearless\"",
        "AS \"DERIVED_regeneration_added\"",
    ):
        assert phrase in sql


def test_numeric_safety_and_complete_rows():
    mod = body('fyp_ai_mod_features_26')
    iso = body('fyp_ai_iso_pre_features_23')
    assert 'NULLIF(energy_existing, 0)' in mod
    assert 'NULLIF(speed_existing, 0)' in mod
    assert '"MOD_PROXY_energy_existing_kwh_year" > 0' in mod
    assert '"ISO_PRE_annual_energy_kwh_year" > 0' in iso
    assert "\"ISO_PRE_energy_class\" IN ('A', 'B', 'C', 'D', 'E', 'F', 'G')" in iso
    for feature in MOD_FEATURES:
        assert f'"{feature}" IS NOT NULL' in mod
    for feature in ISO_PRE_FEATURES:
        assert f'"{feature}" IS NOT NULL' in iso


def test_labeled_49_must_match_exact_iso_revision():
    sql = body('fyp_ai_train_49')
    assert 't.iso_revision_id = f.iso_revision_id' in sql


def test_no_user_personal_data_or_document_binaries():
    forbidden = ('file_content', 'pdf_bytes', 'contact_email', 'contact_phone', 'source_note AS')
    for value in forbidden:
        assert value not in SQL
