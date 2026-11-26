"""Tests for PostgreSQL feature mapping and frozen ElasticNet inference."""

import hashlib
import json
from pathlib import Path

import pytest

from services import elasticnet49 as ai
from enercal import elasticnet_panel as panel

ROOT = Path(__file__).resolve().parents[1]


def source_rows():
    records = json.loads((ROOT / "tests/fixtures/elasticnet49_demo_values.json").read_text())
    return [dict(row, source_classification="test_fixture",
                 review_scope="comparison_only", review_status="approved",
                 value_kind="calculated_estimate", source_note=None,
                 source_verified=False) for row in records]


class FakePipeline:
    def predict(self, frame):
        assert list(frame.columns) == list(ai.FEATURES)
        assert frame.iloc[0]["ISO_PRE_energy_class"] == "C"
        return [-3.25]


def test_manifest_and_model_file():
    assert ai._manifest()["input_features"] == list(ai.FEATURES)
    assert len(ai.FEATURES) == 49
    assert hashlib.sha256(ai.MODEL_PATH.read_bytes()).hexdigest() == ai.MODEL_SHA256


def test_all_features_come_from_source_values():
    features, evidence = ai.prepare_case_features(source_rows())
    assert evidence == "test_fixture_demo"
    assert len(features) == 49
    assert list(features) == list(ai.FEATURES)
    assert features["MOD_PROXY_savings_pct"] == pytest.approx(100 * (6689 - 3358) / 6689)
    assert features["DERIVED_induction_to_pmsm"] == 0
    assert features["ISO_PRE_energy_class"] == "C"
    assert not any(name.startswith("ISO_POST") for name in features)


def test_rejected_review_is_blocked():
    rows = source_rows()
    for row in rows:
        if row["document_type"] == "iso_excel":
            row["review_status"] = "rejected"
    with pytest.raises(ValueError, match="approved"):
        ai.prepare_case_features(rows)


def test_missing_iso_before_input_is_blocked():
    rows = [row for row in source_rows() if not (
        row["document_type"] == "iso_excel" and
        row["field_name"] == "reference_main_energy_wh")]
    with pytest.raises(ValueError, match="Missing ISO Before"):
        ai.prepare_case_features(rows)


def test_read_postgresql_for_selected_case(monkeypatch):
    calls = []

    def fake_query(sql, params):
        calls.append((sql, params))
        return source_rows()

    monkeypatch.setattr(ai, "_query", fake_query)
    features, _ = ai.load_case_features(" TEST-AI-ENG12-001 ")
    assert len(features) == 49
    assert calls == [(ai.CASE_SQL, ("TEST-AI-ENG12-001",))]


def test_model_prediction_uses_saved_pipeline(monkeypatch):
    monkeypatch.setattr(ai, "_pipeline", lambda: FakePipeline())
    features, _ = ai.prepare_case_features(source_rows())
    result = ai.predict(features)
    assert result["gap_pp"] == -3.25
    assert result["ai_savings"] == pytest.approx(result["mod_savings"] - 3.25)


def test_iso_reference_not_an_input():
    rows = source_rows()
    rows.append(dict(rows[0], document_type="iso_excel", phase="proposed",
                     field_name="energy_saving_percent", numeric_value=47.33))
    assert panel.iso_reference(rows) == 47.33
    features, _ = ai.prepare_case_features(rows)
    assert len(features) == 49



def test_same_case_comparison_reads_database_once(monkeypatch):
    rows = source_rows()
    rows.append(dict(rows[0], document_type="iso_excel", phase="proposed",
                     field_name="energy_saving_percent", numeric_value=47.33))
    queries = []

    def fake_query(sql, params):
        queries.append((sql, params))
        return rows

    monkeypatch.setattr(panel, "_query", fake_query)
    monkeypatch.setattr(panel, "predict_xgb", lambda features: {
        "mod_savings": features["MOD_PROXY_savings_pct"],
        "gap_pp": -5.36,
        "ai_savings": features["MOD_PROXY_savings_pct"] - 5.36,
    })
    monkeypatch.setattr(panel, "predict_elasticnet", lambda features: {
        "mod_savings": features["MOD_PROXY_savings_pct"],
        "gap_pp": -3.25,
        "ai_savings": features["MOD_PROXY_savings_pct"] - 3.25,
    })
    result = panel.compare_case("TEST-AI-ENG12-001")
    assert len(result["features49"]) == 49
    assert result["iso"] == 47.33
    assert result["xgb"]["gap_pp"] == -5.36
    assert result["elasticnet"]["gap_pp"] == -3.25
    assert queries == [(panel.CASE_SQL, ("TEST-AI-ENG12-001",))]


def test_paired_evaluation(monkeypatch):
    monkeypatch.setattr(panel, "list_cases", lambda: ["OK", "REJECTED"])

    def fake_compare(case):
        if case == "REJECTED":
            raise ValueError("Not approved")
        return {"case": case, "evidence": "test_fixture_demo", "iso": 47,
                "mod": 50, "xgb": {"ai_savings": 44},
                "elasticnet": {"ai_savings": 46}}

    monkeypatch.setattr(panel, "compare_case", fake_compare)
    results, excluded = panel.evaluate_cases()
    assert results.iloc[0]["MOD error (pp)"] == 3
    assert results.iloc[0]["XGBoost error (pp)"] == 3
    assert results.iloc[0]["ElasticNet error (pp)"] == 1
    assert excluded.iloc[0]["Case"] == "REJECTED"


def test_no_notebook14_validation_csv_dependency():
    code = (ROOT / "enercal/elasticnet_panel.py").read_text()
    assert "selected_scaler_valid_predictions" not in code
    dashboard = (ROOT / "enercal/enercal_ai_engineering12.py").read_text()
    assert "render_panel(case)" in dashboard
    assert 'Notebook 5' in dashboard
