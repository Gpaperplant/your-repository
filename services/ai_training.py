"""Compare real-evidence 26/49-feature savings-gap regressors safely."""

import json
import math
import tempfile
from pathlib import Path

import joblib
import numpy as np
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from services.ai_dataset import (
    CATEGORICAL,
    TARGET,
    dataset_fingerprint,
    feature_names,
    load_ai_dataset,
    prepare_ai_rows,
)

MIN_CASES = 12
MIN_ELEVATORS = 6
COMMON_FIELDS = (
    "case_ref", "elevator_ref", "mod_document_id",
    "mod_revision_id", "iso_revision_id", "source_classification",
)


def _parameters(test_size, random_state, alpha):
    if isinstance(test_size, bool) or not isinstance(test_size, (int, float)):
        raise ValueError("Test size must be a fraction between 0 and 1.")
    if not 0 < test_size < 1:
        raise ValueError("Test size must be a fraction between 0 and 1.")
    if type(random_state) is not int or random_state < 0:
        raise ValueError("Random state must be a nonnegative integer.")
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)):
        raise ValueError("Ridge alpha must be a positive finite number.")
    if not math.isfinite(alpha) or alpha <= 0:
        raise ValueError("Ridge alpha must be a positive finite number.")


def _matched_rows(mod_rows, combined_rows):
    mod = prepare_ai_rows(mod_rows, 26, training=True)
    combined = prepare_ai_rows(combined_rows, 49, training=True)
    if not mod or not combined:
        raise ValueError("No fully verified real labeled cases are available for both models.")

    mod_by_id = {row["case_id"]: row for row in mod}
    combined_by_id = {row["case_id"]: row for row in combined}
    if set(combined_by_id) - set(mod_by_id):
        raise ValueError("The 49-feature view contains cases missing from the 26-feature view.")

    ordered_ids = sorted(combined_by_id)
    for case_id in ordered_ids:
        left, right = mod_by_id[case_id], combined_by_id[case_id]
        for key in COMMON_FIELDS:
            if left[key] != right[key]:
                raise ValueError(f"Mismatched {key} for case {case_id}.")
        for key in (*feature_names(26), TARGET):
            a, b = left[key], right[key]
            if isinstance(a, str):
                same = a == b
            else:
                same = math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-10)
            if not same:
                raise ValueError(f"Mismatched {key} for case {case_id}.")

    return (
        [mod_by_id[key] for key in ordered_ids],
        [combined_by_id[key] for key in ordered_ids],
        len(mod_by_id) - len(combined_by_id),
    )


def _pipeline(feature_count, alpha):
    names = feature_names(feature_count)
    numerical = [i for i, name in enumerate(names) if name not in CATEGORICAL]
    categorical = [i for i, name in enumerate(names) if name in CATEGORICAL]
    preprocessing = ColumnTransformer([
        ("numbers", StandardScaler(), numerical),
        ("categories", OneHotEncoder(handle_unknown="ignore", sparse_output=False), categorical),
    ])
    return Pipeline([
        ("preprocessing", preprocessing),
        ("regressor", Ridge(alpha=alpha)),
    ])


def _matrix(rows, count):
    names = feature_names(count)
    return np.asarray([[row[name] for name in names] for row in rows], dtype=object)


def _metrics(truth, predictions):
    return {
        "mae_pp": float(mean_absolute_error(truth, predictions)),
        "rmse_pp": float(math.sqrt(mean_squared_error(truth, predictions))),
        "r2": float(r2_score(truth, predictions)),
    }


def train_model_comparison(mod_rows, combined_rows, *, test_size=0.2,
                           random_state=42, alpha=10.0):
    """Evaluate 26/49 Ridge regressors on identical elevator-grouped holdouts."""
    _parameters(test_size, random_state, alpha)
    mod, combined, dropped = _matched_rows(mod_rows, combined_rows)
    groups = [row["elevator_ref"] for row in combined]
    if len(combined) < MIN_CASES or len(set(groups)) < MIN_ELEVATORS:
        raise ValueError(
            f"Need at least {MIN_CASES} matched real cases across "
            f"{MIN_ELEVATORS} distinct elevators before training."
        )

    splitter = GroupShuffleSplit(
        n_splits=1, test_size=test_size, random_state=random_state
    )
    training_idx, testing_idx = next(splitter.split(combined, groups=groups))
    if len(training_idx) < 2 or len(testing_idx) < 2:
        raise ValueError("Group split requires at least two training and test cases.")
    train_groups = {groups[i] for i in training_idx}
    test_groups = {groups[i] for i in testing_idx}
    if train_groups & test_groups or len(train_groups) < 2 or len(test_groups) < 2:
        raise ValueError("Group split requires distinct training and test elevators.")

    target = np.asarray([row[TARGET] for row in combined], dtype=float)
    y_train, y_test = target[training_idx], target[testing_idx]
    baseline = DummyRegressor(strategy="mean")
    baseline.fit(np.zeros((len(y_train), 1)), y_train)
    baseline_metrics = _metrics(
        y_test, baseline.predict(np.zeros((len(y_test), 1)))
    )

    models, scores = {}, {}
    for count, rows in ((26, mod), (49, combined)):
        matrix = _matrix(rows, count)
        model = _pipeline(count, alpha)
        model.fit(matrix[training_idx], y_train)
        models[count] = model
        scores[count] = _metrics(y_test, model.predict(matrix[testing_idx]))

    return {
        "models": models,
        "metrics": scores,
        "baseline_metrics": baseline_metrics,
        "feature_names": {count: list(feature_names(count)) for count in (26, 49)},
        "fingerprints": {
            26: dataset_fingerprint(mod, 26, training=True),
            49: dataset_fingerprint(combined, 49, training=True),
        },
        "matched_case_count": len(combined),
        "excluded_mod_only_case_count": dropped,
        "train_case_ids": [combined[i]["case_id"] for i in training_idx],
        "test_case_ids": [combined[i]["case_id"] for i in testing_idx],
        "train_elevator_refs": sorted(train_groups),
        "test_elevator_refs": sorted(test_groups),
        "target_name": TARGET,
        "test_size": float(test_size),
        "random_state": random_state,
        "ridge_alpha": float(alpha),
        "sklearn_version": sklearn.__version__,
    }


def train_from_database(*, test_size=0.2, random_state=42, alpha=10.0):
    """Train only from the read-only, approved real-evidence SQL views."""
    return train_model_comparison(
        load_ai_dataset(26, training=True),
        load_ai_dataset(49, training=True),
        test_size=test_size,
        random_state=random_state,
        alpha=alpha,
    )


def predict_savings_gap(model, feature_row, feature_count):
    """Predict savings gap in percentage points from exactly 26 or 49 inputs."""
    names = feature_names(feature_count)
    if not isinstance(feature_row, dict) or set(feature_row) != set(names):
        raise ValueError("Prediction must contain exactly the selected feature names.")
    record = []
    for name in names:
        value = feature_row[name]
        if name in CATEGORICAL:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Invalid categorical prediction feature: {name}.")
            if name == "ISO_PRE_energy_class" and value.strip() not in "ABCDEFG":
                raise ValueError("Invalid ISO energy class.")
            record.append(value.strip())
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Invalid numeric prediction feature: {name}.")
            record.append(float(value))
    predicted = float(model.predict(np.asarray([record], dtype=object))[0])
    if not math.isfinite(predicted):
        raise ValueError("Model returned a non-finite prediction.")
    return predicted


def save_training_artifacts(result, output_directory):
    """Save evaluated models and metadata to a new directory; never overwrite."""
    destination = Path(output_directory).expanduser()
    if destination.exists():
        raise FileExistsError(f"Model output directory already exists: {destination}")
    if not destination.parent.is_dir():
        raise FileNotFoundError("Create the model output parent directory first.")

    metadata = {key: value for key, value in result.items() if key != "models"}
    metadata["fingerprints"] = {str(k): v for k, v in metadata["fingerprints"].items()}
    metadata["feature_names"] = {str(k): v for k, v in metadata["feature_names"].items()}
    metadata["metrics"] = {str(k): v for k, v in metadata["metrics"].items()}
    metadata["note"] = "Held-out results; models were fitted on training cases only."

    with tempfile.TemporaryDirectory(prefix=".fyp-ai-", dir=destination.parent) as tmp:
        folder = Path(tmp)
        joblib.dump(result["models"][26], folder / "ridge_26.joblib")
        joblib.dump(result["models"][49], folder / "ridge_49.joblib")
        (folder / "evaluation.json").write_text(
            json.dumps(metadata, indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        folder.rename(destination)
    return {
        "model_26": str(destination / "ridge_26.joblib"),
        "model_49": str(destination / "ridge_49.joblib"),
        "evaluation": str(destination / "evaluation.json"),
    }
