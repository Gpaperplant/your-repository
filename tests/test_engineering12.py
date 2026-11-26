"""Offline checks for frozen XGBoost Engineering 12 workflow."""
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import pytest

from services import engineering12 as ai


def source_rows(classification="test_fixture", scope="comparison_only"):
    rows = []

    def add(doc, phase, name, val, *, text=False):
        rows.append({
            "document_type": doc, "source_classification": classification,
            "review_scope": scope, "review_status": "approved",
            "phase": phase, "field_name": name,
            "numeric_value": None if text else val,
            "text_value": val if text else None,
            "source_verified": classification == "real",
            "value_kind": "engineering_specification",
            "source_note": None,
        })

    add("mod_pdf", "existing", "annual_energy_consumption", 10000)
    add("mod_pdf", "proposed", "annual_energy_consumption", 6000)
    add("mod_pdf", "existing", "nominal_load_kg", 1000)
    add("mod_pdf", "existing", "nominal_speed_m_s", 1.6)
    add("mod_pdf", "common", "travel_distance_m", 40)
    add("mod_pdf", "common", "iso_usage_category", 4)
    add("mod_pdf", "existing", "drive_type", "Induction", text=True)
    add("mod_pdf", "proposed", "drive_type", "PMSM", text=True)
    add("iso_excel", "existing", "energy_per_m_wh", 0.8)
    add("iso_excel", "existing", "start_stop_energy_wh", 3.0)
    add("iso_excel", "existing", "daily_nonrunning_energy_wh", 22.0)
    add("iso_excel", "existing", "standby30_power_w", 150.0)
    # ISO After is stored, but never exposed to the model input.
    add("iso_excel", "proposed", "annual_energy_kwh", 4100)
    return rows


def test_exact_input_order_and_derived_fields():
    data, label = ai.prepare_features(source_rows())
    assert label == "test_fixture_demo"
    assert list(data) == list(ai.FEATURES)
    assert len(data) == 12
    assert data["MOD_PROXY_savings_pct"] == 40
    assert data["DERIVED_induction_to_pmsm"] == 1
    assert "annual_energy_kwh" not in data


def test_real_requires_full_values_and_all_verified():
    with pytest.raises(ValueError, match="full-values"):
        ai.prepare_features(source_rows("real"))
    rows = source_rows("real", "full_values")
    assert ai.prepare_features(rows)[1] == "real_verified"
    rows[-1]["source_verified"] = False
    with pytest.raises(ValueError, match="unverified"):
        ai.prepare_features(rows)


def test_rejects_rejected_latest_iso():
    rows = source_rows()
    for row in rows:
        if row["document_type"] == "iso_excel":
            row["review_status"] = "rejected"
    with pytest.raises(ValueError, match="Both latest"):
        ai.prepare_features(rows)


def test_missing_iso_field_is_not_imputed_from_wrong_phase():
    rows = [row for row in source_rows()
            if (row["document_type"], row["phase"], row["field_name"])
            != ("iso_excel", "existing", "standby30_power_w")]
    with pytest.raises(ValueError, match="ISO_PRE_standby_30min_w"):
        ai.prepare_features(rows)


def test_unknown_motor_is_not_false_zero():
    rows = source_rows()
    rows[6]["text_value"] = "Unspecified"
    with pytest.raises(ValueError, match="motor change"):
        ai.prepare_features(rows)


def test_derived_zero_for_known_no_transition():
    rows = source_rows()
    rows[7]["text_value"] = "Induction"
    assert ai.prepare_features(rows)[0]["DERIVED_induction_to_pmsm"] == 0


def test_mixed_document_classes_rejected():
    rows = source_rows()
    rows[-1]["source_classification"] = "unknown"
    with pytest.raises(ValueError, match="Inconsistent"):
        ai.prepare_features(rows)


def test_duplicate_field_rejected():
    rows = source_rows()
    rows.append(rows[0].copy())
    with pytest.raises(ValueError, match="Duplicate"):
        ai.prepare_features(rows)


def test_negative_energy_rejected():
    rows = source_rows()
    rows[1]["numeric_value"] = -1
    with pytest.raises(ValueError, match="annual energy"):
        ai.prepare_features(rows)


class FakeModel:
    def predict(self, frame):
        assert list(frame.columns) == list(ai.FEATURES)
        return [-5.0]


def bundle():
    return {
        "model": FakeModel(),
        "model_id": "engineering12_pseudohuber",
        "selected_features": list(ai.FEATURES),
        "target": "TARGET_savings_gap_pp",
        "model_selection_stage": "locked_for_independent_test_not_yet_independently_validated",
        "optional_conservative_offset_pp": 2.5,
        "accuracy_offset_pp": 0.0,
        "workbook_sha256": ai.WORKBOOK_SHA256,
    }


def test_prediction_formula_with_locked_model(tmp_path):
    folder = tmp_path / ai.MODEL_PATH.name
    folder.write_bytes(b"not a real model")
    data, _ = ai.prepare_features(source_rows())
    with patch("joblib.load", return_value=bundle()):
        result = ai.predict(data, model_path=folder)
    assert result == {"mod_savings": 40, "gap_pp": -5,
                      "ai_savings": 35, "conservative_savings": 32.5}


def test_wrong_model_refused(tmp_path):
    folder = tmp_path / ai.MODEL_PATH.name
    folder.write_bytes(b"not a real model")
    data, _ = ai.prepare_features(source_rows())
    bad = bundle()
    bad["selected_features"] = list(reversed(ai.FEATURES))
    with patch("joblib.load", return_value=bad):
        with pytest.raises(ValueError, match="approved frozen"):
            ai.predict(data, model_path=folder)


def test_model_missing_graceful(tmp_path):
    data, _ = ai.prepare_features(source_rows())
    with pytest.raises(FileNotFoundError, match="LOCKED.joblib"):
        ai.predict(data, model_path=tmp_path / ai.MODEL_PATH.name)


def test_sql_only_uses_current_five_tables():
    assert "legacy_archive" not in ai.CASE_SQL
    for table in ("modernisation_cases", "project_documents", "document_revisions", "engineering_values"):
        assert table in ai.CASE_SQL
