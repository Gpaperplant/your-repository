"""Dashboard structure checks replacing outdated legacy sidebar expectations."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "streamlit_app.py"
DASHBOARD = ROOT / "enercal" / "enercal_ai_engineering12.py"


def test_dashboard_is_single_prediction_and_evaluation_page():
    page = DASHBOARD.read_text()
    assert "Predict ISO-calculated savings" in page
    assert "Evaluate eligible cases" in page
    assert "MOD vs AI vs ISO" in page
    assert "Download evaluation CSV" in page


def test_model_is_frozen_and_evidence_gated():
    page = DASHBOARD.read_text()
    assert "from services.engineering12 import" in page
    assert "load_case_features" in page
    assert "test_fixture_demo" in page
    assert '"real_verified"' in page
    assert "model.fit(" not in page
    assert "UPDATE document_reviews" not in page
    assert "INSERT INTO document_reviews" not in page


def test_navigation_points_to_existing_dashboard_file():
    assert "enercal/enercal_ai_engineering12.py" in APP.read_text()
    assert DASHBOARD.is_file()
