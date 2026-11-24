"""Offline regression checks for leakage-safe 26/49 training and model exports."""

import importlib.util
import json
import math
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType

import joblib
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def module():
    sys.path.insert(0, str(ROOT))
    try:
        if "database" not in sys.modules:
            database = ModuleType("database")
            database.get_connection = lambda: None
            sys.modules["database"] = database
        spec = importlib.util.spec_from_file_location(
            "_ai_training_under_test", ROOT / "services/ai_training.py"
        )
        result = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        return result
    finally:
        sys.path.remove(str(ROOT))


def sample_rows(module, count=36, elevators=None):
    datasets = {26: [], 49: []}
    for i in range(count):
        row = {
            "case_id": i + 1,
            "case_ref": f"MOD{i + 1:04}",
            "elevator_ref": f"LIFT{i // 2:03}" if elevators is None else f"LIFT{i % elevators:03}",
            "mod_document_id": 100 + i,
            "mod_revision_id": 200 + i,
            "iso_document_id": 300 + i,
            "iso_revision_id": 400 + i,
            "source_classification": "real",
            module.TARGET: -6 + (i % 8) * 0.8 + (i % 3) * 0.1,
        }
        for j, name in enumerate(module.feature_names(49)):
            if name == "ISO_PRE_energy_class":
                row[name] = "B" if i % 2 else "C"
            elif name in module.CATEGORICAL:
                row[name] = "Gearless" if i % 3 else "Geared"
            else:
                row[name] = float(2 + j / 5 + i % 11 / 7 + (i * j % 5) / 13)
        for size in (26, 49):
            columns = (
                *module.prepare_ai_rows.__globals__["METADATA"][(size, True)],
                *module.feature_names(size),
                module.TARGET,
            )
            datasets[size].append({name: row[name] for name in columns})
    return datasets[26], datasets[49]


@pytest.fixture
def rows(module):
    return sample_rows(module)


@pytest.fixture
def trained(module, rows):
    return module.train_model_comparison(*rows)


def test_comparison_trains_both_models_on_identical_cases(module, trained):
    assert set(trained["models"]) == {26, 49}
    assert set(trained["metrics"]) == {26, 49}
    assert len(trained["feature_names"][26]) == 26
    assert len(trained["feature_names"][49]) == 49
    assert trained["matched_case_count"] == 36
    assert trained["target_name"] == "TARGET_savings_gap_pp"
    assert trained["excluded_mod_only_case_count"] == 0
    assert len(trained["train_case_ids"] + trained["test_case_ids"]) == 36
    assert not set(trained["train_case_ids"]) & set(trained["test_case_ids"])
    assert len(trained["fingerprints"][26]) == 64
    assert len(trained["fingerprints"][49]) == 64


def test_grouped_splits_isolate_entire_elevators(trained):
    assert not set(trained["train_elevator_refs"]) & set(trained["test_elevator_refs"])
    assert len(trained["train_elevator_refs"]) >= 2
    assert len(trained["test_elevator_refs"]) >= 2
    for case_id in trained["train_case_ids"]:
        assert f"LIFT{(case_id - 1) // 2:03}" in trained["train_elevator_refs"]
    for case_id in trained["test_case_ids"]:
        assert f"LIFT{(case_id - 1) // 2:03}" in trained["test_elevator_refs"]


def test_metrics_are_finite_and_in_target_units(trained):
    for size in (26, 49):
        assert set(trained["metrics"][size]) == {"mae_pp", "rmse_pp", "r2"}
        assert all(math.isfinite(v) for v in trained["metrics"][size].values())
        assert trained["metrics"][size]["mae_pp"] >= 0
        assert trained["metrics"][size]["rmse_pp"] >= 0
    assert all(math.isfinite(v) for v in trained["baseline_metrics"].values())


def test_preprocessing_is_fit_on_train_only(module, rows, trained):
    names = module.feature_names(49)
    numeric_indices = [i for i, name in enumerate(names) if name not in module.CATEGORICAL]
    by_id = {row["case_id"]: row for row in rows[1]}
    expected = np.asarray([
        [by_id[case_id][names[i]] for i in numeric_indices]
        for case_id in trained["train_case_ids"]
    ], dtype=float).mean(axis=0)
    actual = trained["models"][49].named_steps["preprocessing"].named_transformers_["numbers"].mean_
    np.testing.assert_allclose(actual, expected)


def test_deterministic_split_scores_and_fingerprints(module, rows, trained):
    repeated = module.train_model_comparison(*rows)
    for key in ("train_case_ids", "test_case_ids", "fingerprints", "metrics"):
        assert repeated[key] == trained[key]


def test_input_order_does_not_change_results(module, rows, trained):
    shuffled = (list(reversed(rows[0])), list(reversed(rows[1])))
    result = module.train_model_comparison(*shuffled)
    assert result["train_case_ids"] == trained["train_case_ids"]
    assert result["test_case_ids"] == trained["test_case_ids"]
    assert result["metrics"] == trained["metrics"]


def test_mod_only_cases_are_excluded_from_fair_comparison(module, rows):
    result = module.train_model_comparison(rows[0], rows[1][:-4])
    assert result["matched_case_count"] == 32
    assert result["excluded_mod_only_case_count"] == 4
    assert set(result["train_case_ids"] + result["test_case_ids"]) == set(range(1, 33))


def test_group_by_elevator_not_case_id(module):
    mod, full = sample_rows(module, count=30, elevators=6)
    result = module.train_model_comparison(mod, full, test_size=0.3)
    assert not set(result["train_elevator_refs"]) & set(result["test_elevator_refs"])


@pytest.mark.parametrize("field", [
    "case_ref", "elevator_ref", "mod_document_id", "mod_revision_id", "iso_revision_id",
])
def test_metadata_mismatch_rejected(module, rows, field):
    mod, full = deepcopy(rows)
    full[0][field] = ("WRONG" if isinstance(full[0][field], str) else 999999)
    with pytest.raises(ValueError, match=f"Mismatched {field}"):
        module.train_model_comparison(mod, full)


def test_target_mismatch_rejected(module, rows):
    mod, full = deepcopy(rows)
    full[2][module.TARGET] += 1
    with pytest.raises(ValueError, match="Mismatched TARGET"):
        module.train_model_comparison(mod, full)


def test_mod_feature_mismatch_rejected(module, rows):
    mod, full = deepcopy(rows)
    full[1]["MOD_starts_per_year"] += 1
    with pytest.raises(ValueError, match="Mismatched MOD_starts_per_year"):
        module.train_model_comparison(mod, full)


def test_49_cannot_contain_extra_case_not_in_26(module, rows):
    mod, full = deepcopy(rows)
    with pytest.raises(ValueError, match="missing from the 26-feature"):
        module.train_model_comparison(mod[:-1], full)


@pytest.mark.parametrize("source", ["synthetic", "unknown", "test_fixture"])
def test_ineligible_source_rejected(module, rows, source):
    mod, full = deepcopy(rows)
    full[0]["source_classification"] = source
    with pytest.raises(ValueError, match="verified real evidence"):
        module.train_model_comparison(mod, full)


def test_missing_or_nonfinite_input_rejected(module, rows):
    mod, full = deepcopy(rows)
    full[0]["ISO_PRE_daily_trips"] = None
    with pytest.raises(ValueError, match="Invalid numeric"):
        module.train_model_comparison(mod, full)
    mod, full = deepcopy(rows)
    full[0]["ISO_PRE_daily_trips"] = math.inf
    with pytest.raises(ValueError, match="Non-finite"):
        module.train_model_comparison(mod, full)


def test_extra_leakage_column_rejected(module, rows):
    mod, full = deepcopy(rows)
    full[0]["ISO_AFTER_annual_energy_kwh"] = 42
    with pytest.raises(ValueError, match="view columns mismatch"):
        module.train_model_comparison(mod, full)


def test_empty_dataset_fails_without_fabricating_rows(module, rows):
    with pytest.raises(ValueError, match="No fully verified"):
        module.train_model_comparison([], [])
    with pytest.raises(ValueError, match="No fully verified"):
        module.train_model_comparison(rows[0], [])


def test_insufficient_real_cases_rejected(module):
    with pytest.raises(ValueError, match="at least 12 matched"):
        module.train_model_comparison(*sample_rows(module, count=10))


def test_insufficient_distinct_elevators_rejected(module):
    with pytest.raises(ValueError, match="6 distinct elevators"):
        module.train_model_comparison(*sample_rows(module, count=30, elevators=4))


def test_too_few_test_elevators_rejected(module, rows):
    with pytest.raises(ValueError, match="distinct training and test elevators"):
        module.train_model_comparison(*rows, test_size=0.01)


def test_invalid_parameters_rejected_before_training(module, rows):
    for kwargs, message in [
        ({"test_size": 0}, "Test size"),
        ({"test_size": 1}, "Test size"),
        ({"test_size": True}, "Test size"),
        ({"test_size": "0.2"}, "Test size"),
        ({"random_state": -1}, "Random state"),
        ({"random_state": True}, "Random state"),
        ({"alpha": 0}, "Ridge alpha"),
        ({"alpha": float("nan")}, "Ridge alpha"),
        ({"alpha": float("inf")}, "Ridge alpha"),
    ]:
        with pytest.raises(ValueError, match=message):
            module.train_model_comparison(*rows, **kwargs)


def test_train_from_database_uses_real_read_only_views(module, monkeypatch, rows):
    called = []

    def fake_loader(count, *, training):
        called.append((count, training))
        return rows[0] if count == 26 else rows[1]

    monkeypatch.setattr(module, "load_ai_dataset", fake_loader)
    result = module.train_from_database()
    assert called == [(26, True), (49, True)]
    assert result["matched_case_count"] == 36


def test_predict_both_sizes(module, rows, trained):
    for size, records in ((26, rows[0]), (49, rows[1])):
        feature_row = {key: records[0][key] for key in module.feature_names(size)}
        prediction = module.predict_savings_gap(trained["models"][size], feature_row, size)
        assert math.isfinite(prediction)
        assert isinstance(prediction, float)


def test_prediction_accepts_unseen_categories_without_refitting(module, rows, trained):
    record = {name: rows[1][0][name] for name in module.feature_names(49)}
    record["MOD_gear_existing"] = "New unseen gear"
    assert math.isfinite(module.predict_savings_gap(trained["models"][49], record, 49))


@pytest.mark.parametrize("bad", [None, False, float("nan"), float("inf"), "1.5"])
def test_predict_rejects_bad_numeric_value(module, rows, trained, bad):
    record = {name: rows[1][0][name] for name in module.feature_names(49)}
    record["ISO_PRE_daily_trips"] = bad
    with pytest.raises(ValueError, match="Invalid numeric prediction"):
        module.predict_savings_gap(trained["models"][49], record, 49)


def test_predict_rejects_missing_extra_or_target_keys(module, rows, trained):
    record = {name: rows[1][0][name] for name in module.feature_names(49)}
    for bad in ({**record, "TARGET_savings_gap_pp": 0}, {k: v for k, v in record.items() if k != "ISO_PRE_daily_trips"}):
        with pytest.raises(ValueError, match="exactly the selected feature"):
            module.predict_savings_gap(trained["models"][49], bad, 49)


def test_prediction_rejects_missing_or_invalid_categories(module, rows, trained):
    record = {name: rows[1][0][name] for name in module.feature_names(49)}
    record["MOD_gear_existing"] = "   "
    with pytest.raises(ValueError, match="categorical prediction"):
        module.predict_savings_gap(trained["models"][49], record, 49)
    record["MOD_gear_existing"] = "Geared"
    record["ISO_PRE_energy_class"] = "Q"
    with pytest.raises(ValueError, match="Invalid ISO energy class"):
        module.predict_savings_gap(trained["models"][49], record, 49)


def test_invalid_feature_count_rejected(module, trained):
    with pytest.raises(ValueError, match="26 or 49"):
        module.predict_savings_gap(trained["models"][49], {}, 50)


def test_export_models_and_audit_metadata(module, rows, trained, tmp_path):
    output = tmp_path / "models_v1"
    paths = module.save_training_artifacts(trained, output)
    assert set(paths) == {"model_26", "model_49", "evaluation"}
    assert all(Path(path).is_file() for path in paths.values())
    info = json.loads((output / "evaluation.json").read_text())
    assert "models" not in info
    assert info["target_name"] == module.TARGET
    assert info["matched_case_count"] == 36
    assert set(info["metrics"]) == {"26", "49"}
    assert info["train_case_ids"] == trained["train_case_ids"]
    assert info["test_case_ids"] == trained["test_case_ids"]
    for size, records in ((26, rows[0]), (49, rows[1])):
        model = joblib.load(output / f"ridge_{size}.joblib")
        record = {name: records[0][name] for name in module.feature_names(size)}
        expected = module.predict_savings_gap(trained["models"][size], record, size)
        actual = module.predict_savings_gap(model, record, size)
        assert actual == pytest.approx(expected)


def test_export_cannot_overwrite_existing_directory(module, trained, tmp_path):
    output = tmp_path / "protected"
    output.mkdir()
    sentinel = output / "important.txt"
    sentinel.write_text("do not delete")
    with pytest.raises(FileExistsError, match="already exists"):
        module.save_training_artifacts(trained, output)
    assert sentinel.read_text() == "do not delete"


def test_export_requires_existing_parent(module, trained, tmp_path):
    with pytest.raises(FileNotFoundError, match="parent"):
        module.save_training_artifacts(trained, tmp_path / "missing" / "output")


def test_models_do_not_train_from_zero_records(module, monkeypatch):
    monkeypatch.setattr(module, "load_ai_dataset", lambda size, *, training: [])
    with pytest.raises(ValueError, match="No fully verified"):
        module.train_from_database()
