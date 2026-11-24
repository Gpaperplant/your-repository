"""Regression tests for saved-model predictions and real-case eligibility."""

import json
import math
import shutil
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import joblib
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if "database" not in sys.modules:
    database = ModuleType("database")
    database.get_connection = lambda: None
    sys.modules["database"] = database

from services import ai_prediction as prediction
from services.ai_dataset import CATEGORICAL, METADATA, TARGET, feature_names
from services.ai_training import (
    predict_savings_gap, save_training_artifacts, train_model_comparison,
)


def sample_rows(count=36):
    results = {26: [], 49: []}
    for index in range(count):
        row = {
            "case_id": index + 1,
            "case_ref": f"MOD{index + 1:04}",
            "elevator_ref": f"LIFT{index // 2:03}",
            "mod_document_id": index + 100,
            "mod_revision_id": index + 200,
            "iso_document_id": index + 300,
            "iso_revision_id": index + 400,
            "source_classification": "real",
            TARGET: float(-8 + (index % 7) * 1.1),
        }
        for offset, name in enumerate(feature_names(49)):
            if name == "ISO_PRE_energy_class":
                row[name] = "B" if index % 2 else "C"
            elif name in CATEGORICAL:
                row[name] = "Geared" if index % 2 else "Gearless"
            elif name == "MOD_PROXY_savings_pct":
                row[name] = 52.7
            else:
                row[name] = float(1 + offset / 3 + (index % 11) * 0.4)
        for size in (26, 49):
            names = (*METADATA[(size, True)], *feature_names(size), TARGET)
            results[size].append({name: row[name] for name in names})
    return results[26], results[49]


@pytest.fixture(scope="module")
def training_result():
    return train_model_comparison(*sample_rows())


@pytest.fixture(scope="module")
def saved(tmp_path_factory, training_result):
    directory = tmp_path_factory.mktemp("fyp_prediction") / "real_evidence"
    save_training_artifacts(training_result, directory)
    return directory


@pytest.fixture
def feature_row():
    return {key: value for key, value in sample_rows()[1][0].items()
            if key in feature_names(49)}


def feature_inputs(size):
    raw = sample_rows()[1][0]
    return {name: raw[name] for name in feature_names(size)}


@pytest.mark.parametrize("size", [26, 49])
def test_load_saved_model_exact_feature_contract(saved, size):
    bundle = prediction.load_saved_model(saved, size)
    assert bundle["feature_count"] == size
    assert bundle["model"].n_features_in_ == size
    assert bundle["metadata"]["feature_names"][str(size)] == list(feature_names(size))


@pytest.mark.parametrize("size", [26, 49])
def test_predictions_match_trained_pipeline(saved, training_result, size):
    features = feature_inputs(size)
    actual = prediction.predict_features(features, saved, size)
    expected = predict_savings_gap(training_result["models"][size], features, size)
    assert actual["predicted_savings_gap_pp"] == pytest.approx(expected)
    assert actual["predicted_iso_savings_percent"] == pytest.approx(52.7 + expected)
    assert actual["mod_estimated_savings_percent"] == pytest.approx(52.7)
    assert actual["trained_case_count"] == 36
    assert actual["evaluation_mae_pp"] >= 0
    assert len(actual["training_fingerprint"]) == 64
    assert actual["source"] == "manual_unverified_features"


def test_prediction_does_not_mutate_inputs(saved, feature_row):
    original = deepcopy(feature_row)
    prediction.predict_features(feature_row, saved)
    assert feature_row == original


def test_prediction_independent_of_input_key_order(saved, feature_row):
    first = prediction.predict_features(feature_row, saved)
    last = prediction.predict_features(dict(reversed(list(feature_row.items()))), saved)
    assert last == first


@pytest.mark.parametrize("size", [26, 49])
def test_accepts_mapper_decimal_features(saved, size):
    features = feature_inputs(size)
    for name in feature_names(size):
        if name not in CATEGORICAL:
            features[name] = Decimal(str(features[name]))
    actual = prediction.predict_features(features, saved, size)
    assert math.isfinite(actual["predicted_savings_gap_pp"])


@pytest.mark.parametrize("extra", ["TARGET_savings_gap_pp", "ISO_AFTER_annual_energy_kwh_year", "case_id"])
def test_rejects_extra_training_or_metadata_columns(saved, feature_row, extra):
    feature_row[extra] = 3
    with pytest.raises(ValueError, match="exactly the selected feature"):
        prediction.predict_features(feature_row, saved)


def test_rejects_missing_feature(saved, feature_row):
    feature_row.pop("ISO_PRE_daily_trips")
    with pytest.raises(ValueError, match="exactly the selected feature"):
        prediction.predict_features(feature_row, saved)


@pytest.mark.parametrize("bad", [None, True, False, float("nan"), float("inf"), "10", Decimal("NaN")])
def test_rejects_bad_numeric_before_loading_model(monkeypatch, saved, feature_row, bad):
    feature_row["MOD_PROXY_savings_pct"] = bad
    monkeypatch.setattr(prediction, "load_saved_model", lambda *args: pytest.fail("model loaded"))
    with pytest.raises(ValueError, match="numeric prediction feature"):
        prediction.predict_features(feature_row, saved)


@pytest.mark.parametrize("bad", [" ", 5, None])
def test_rejects_bad_categorical(saved, feature_row, bad):
    feature_row["MOD_gear_existing"] = bad
    with pytest.raises(ValueError, match="categorical prediction feature"):
        prediction.predict_features(feature_row, saved)


@pytest.mark.parametrize("bad", ["Q", "c", "Class C", "AB"])
def test_rejects_bad_energy_class(saved, feature_row, bad):
    feature_row["ISO_PRE_energy_class"] = bad
    with pytest.raises(ValueError, match="Invalid ISO energy class"):
        prediction.predict_features(feature_row, saved)


@pytest.mark.parametrize("size", [25, 0, True, 50, "49"])
def test_unsupported_model_size(saved, size):
    with pytest.raises(ValueError, match="26 or 49"):
        prediction.load_saved_model(saved, size)


def test_missing_model_directory(tmp_path):
    with pytest.raises(FileNotFoundError, match="Model directory"):
        prediction.load_saved_model(tmp_path / "not_found")


@pytest.mark.parametrize("directory", [None, "", "   ", 2])
def test_invalid_model_directory(directory):
    with pytest.raises(ValueError, match="Model directory"):
        prediction.load_saved_model(directory)


def clone(saved, tmp_path):
    destination = tmp_path / "copy"
    shutil.copytree(saved, destination)
    return destination


@pytest.mark.parametrize("key,value,expected", [
    ("target_name", "WRONG", "target"),
    ("sklearn_version", "0.0.0", "scikit-learn version"),
    ("matched_case_count", "36", "case count"),
    ("fingerprints", {"49": "not-a-hash"}, "fingerprint"),
    ("metrics", {"49": {"mae_pp": float("nan")}}, "MAE"),
])
def test_metadata_mismatch_blocks_loading(saved, tmp_path, monkeypatch, key, value, expected):
    destination = clone(saved, tmp_path)
    path = destination / "evaluation.json"
    data = json.loads(path.read_text())
    data[key] = value
    path.write_text(json.dumps(data))
    monkeypatch.setattr(prediction.joblib, "load", lambda _: pytest.fail("untrusted load"))
    with pytest.raises(ValueError, match=expected):
        prediction.load_saved_model(destination)


def test_feature_order_mismatch_blocks_loading(saved, tmp_path, monkeypatch):
    destination = clone(saved, tmp_path)
    path = destination / "evaluation.json"
    data = json.loads(path.read_text())
    data["feature_names"]["49"] = list(reversed(data["feature_names"]["49"]))
    path.write_text(json.dumps(data))
    monkeypatch.setattr(prediction.joblib, "load", lambda _: pytest.fail("untrusted load"))
    with pytest.raises(ValueError, match="feature names or order"):
        prediction.load_saved_model(destination)


def test_missing_model_file(saved, tmp_path):
    directory = clone(saved, tmp_path)
    (directory / "ridge_26.joblib").unlink()
    with pytest.raises(FileNotFoundError, match="Saved Ridge model"):
        prediction.load_saved_model(directory, 26)


def test_missing_metadata_file(saved, tmp_path):
    directory = clone(saved, tmp_path)
    (directory / "evaluation.json").unlink()
    with pytest.raises(FileNotFoundError, match="evaluation.json"):
        prediction.load_saved_model(directory)


def test_malformed_metadata_file(saved, tmp_path):
    directory = clone(saved, tmp_path)
    (directory / "evaluation.json").write_text("broken json")
    with pytest.raises(ValueError, match="unreadable or invalid"):
        prediction.load_saved_model(directory)


def test_non_dict_metadata(saved, tmp_path):
    directory = clone(saved, tmp_path)
    (directory / "evaluation.json").write_text("[]")
    with pytest.raises(ValueError, match="JSON object"):
        prediction.load_saved_model(directory)


def test_oversized_metadata(saved, tmp_path):
    directory = clone(saved, tmp_path)
    (directory / "evaluation.json").write_text(" " * (1_048_576 + 1))
    with pytest.raises(ValueError, match="unexpectedly large"):
        prediction.load_saved_model(directory)


def test_symlink_model_not_loaded(saved, tmp_path):
    directory = clone(saved, tmp_path)
    model_file = directory / "ridge_49.joblib"
    model_file.unlink()
    model_file.symlink_to(saved / "ridge_49.joblib")
    with pytest.raises(FileNotFoundError, match="Saved Ridge model"):
        prediction.load_saved_model(directory)


def test_wrong_estimator_shape(saved, tmp_path):
    directory = clone(saved, tmp_path)
    joblib.dump({"bad": "model"}, directory / "ridge_49.joblib")
    with pytest.raises(ValueError, match="pipeline"):
        prediction.load_saved_model(directory)


def test_rejects_nonfinite_prediction(saved, monkeypatch, feature_row):
    monkeypatch.setattr(prediction, "predict_savings_gap", lambda *args: float("nan"))
    with pytest.raises(ValueError, match="non-finite"):
        prediction.predict_features(feature_row, saved)


def test_out_of_range_estimate_is_flagged_without_clipping(saved, monkeypatch, feature_row):
    monkeypatch.setattr(prediction, "predict_savings_gap", lambda *args: 80.0)
    result = prediction.predict_features(feature_row, saved)
    assert result["predicted_iso_savings_percent"] == pytest.approx(132.7)
    assert "warning" in result


@pytest.mark.parametrize("size", [26, 49])
def test_database_prediction_uses_only_eligible_view(saved, monkeypatch, size):
    raw = sample_rows()[1][0]
    fields = (*METADATA[(size, False)], *feature_names(size))
    row = {name: raw[name] for name in fields}
    calls = []

    def fake_loader(count, *, training, case_ref):
        calls.append((count, training, case_ref))
        return [row]

    monkeypatch.setattr(prediction, "load_ai_dataset", fake_loader)
    result = prediction.predict_case_from_database(" MOD0001 ", saved, size)
    assert calls == [(size, False, "MOD0001")]
    assert result["source"] == "verified_real_database_case"
    assert result["case_ref"] == "MOD0001"
    assert result["mod_document_id"] == 100
    assert result["mod_revision_id"] == 200
    assert result["elevator_ref"] == "LIFT000"
    assert ("iso_document_id" in result) == (size == 49)


def test_missing_database_case_rejected(saved, monkeypatch):
    monkeypatch.setattr(prediction, "load_ai_dataset", lambda *a, **kw: [])
    with pytest.raises(ValueError, match="No fully verified real prediction record"):
        prediction.predict_case_from_database("MOD0001", saved)


def test_multiple_database_rows_rejected(saved, monkeypatch):
    monkeypatch.setattr(prediction, "load_ai_dataset", lambda *a, **kw: [{}, {}])
    with pytest.raises(ValueError, match="exactly one"):
        prediction.predict_case_from_database("MOD0001", saved)


@pytest.mark.parametrize("case_ref", [None, "", "   ", 123])
def test_invalid_database_case_reference(saved, case_ref):
    with pytest.raises(ValueError, match="Case reference"):
        prediction.predict_case_from_database(case_ref, saved)


def test_prediction_never_writes_a_file(saved, feature_row):
    files = {p.name: p.read_bytes() for p in saved.iterdir()}
    prediction.predict_features(feature_row, saved)
    assert {p.name: p.read_bytes() for p in saved.iterdir()} == files
