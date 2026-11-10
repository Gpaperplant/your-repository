
from math import isfinite

import numpy as np
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.model_selection import GroupShuffleSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from services.ai_dataset import FEATURE_COLUMNS, model_inputs


MIN_ELEVATORS = 10


def _metrics(actual, predicted):
    # Report errors in percentage points, not relative percentages.
    errors = np.asarray(predicted) - np.asarray(actual)

    return {
        "mae_pp": round(float(np.mean(np.abs(errors))), 3),
        "rmse_pp": round(float(np.sqrt(np.mean(errors ** 2))), 3),
        "bias_pp": round(float(np.mean(errors)), 3),
    }


def _check_dataset(dataset):
    # Reject duplicate projects, missing identities and inconsistent targets.
    if len(dataset) < MIN_ELEVATORS:
        raise ValueError(
            f"At least {MIN_ELEVATORS} verified projects are required."
        )

    for key in ("case_id", "document_id", "elevator_id", "case_ref"):
        if any(row.get(key) is None for row in dataset):
            raise ValueError(f"Every row must have {key}.")

    for key in ("case_id", "document_id"):
        if len({row[key] for row in dataset}) != len(dataset):
            raise ValueError(f"Duplicate {key} in the training dataset.")

    if len({row["elevator_id"] for row in dataset}) < MIN_ELEVATORS:
        raise ValueError(
            f"At least {MIN_ELEVATORS} distinct elevators are required."
        )

    for row in dataset:
        try:
            predicted = float(row["predicted_saving_percent"])
            measured = float(row["measured_saving_percent"])
            correction = float(row["target_correction_pp"])

        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "Missing or invalid saving values in dataset."
            ) from exc

        if not all(map(isfinite, (predicted, measured, correction))):
            raise ValueError(
                "Saving values must be finite numbers."
            )

        if predicted > 100 or measured > 100:
            raise ValueError(
                "Energy saving cannot exceed 100%."
            )

        if abs(measured - predicted - correction) > 0.02:
            raise ValueError(
                "Measured saving does not match target correction."
            )


def train_and_evaluate(dataset, test_size=0.25, random_state=42):
    # Reserve independent elevators for testing before fitting any preprocessing.
    dataset = list(dataset)
    _check_dataset(dataset)

    if not 0 < test_size < 1:
        raise ValueError(
            "test_size must be between 0 and 1."
        )

    features, targets, groups = model_inputs(dataset)

    try:
        x = np.array([
            [
                np.nan if row[name] is None else float(row[name])
                for name in FEATURE_COLUMNS
            ]
            for row in features
        ], dtype=float)

        y = np.asarray(targets, dtype=float)

    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "Model features must be numeric or None."
        ) from exc

    if not np.isfinite(y).all() or np.isinf(x).any():
        raise ValueError(
            "Model inputs contain infinite or invalid values."
        )

    split = GroupShuffleSplit(
        n_splits=1,
        test_size=test_size,
        random_state=random_state,
    )

    train_idx, test_idx = next(
        split.split(x, y, groups)
    )

    train_groups = {groups[i] for i in train_idx}
    test_groups = {groups[i] for i in test_idx}

    if len(train_groups) < 2 or len(test_groups) < 2:
        raise ValueError(
            "Split needs at least two independent elevators in each set."
        )

    # Drop features missing for every training record without using test data.
    selected = np.flatnonzero(
        ~np.isnan(x[train_idx]).all(axis=0)
    )

    feature_names = [
        FEATURE_COLUMNS[i]
        for i in selected
    ]

    if "predicted_saving_percent" not in feature_names:
        raise ValueError(
            "The EnerCal saving estimate is required."
        )

    model = make_pipeline(
        SimpleImputer(strategy="median"),
        StandardScaler(),
        Ridge(alpha=10.0),
    )

    model.fit(
        x[train_idx][:, selected],
        y[train_idx],
    )

    baseline = x[
        test_idx,
        FEATURE_COLUMNS.index("predicted_saving_percent")
    ]

    actual = baseline + y[test_idx]
    bias = float(np.mean(y[train_idx]))

    # Evaluate all three methods using the same unseen elevators.
    estimates = {
        "enercal": baseline,
        "bias_correction": np.minimum(
            100,
            baseline + bias,
        ),
        "ridge_ai": np.minimum(
            100,
            baseline + model.predict(
                x[test_idx][:, selected]
            ),
        ),
    }

    results = {
        "model": model,
        "feature_names": feature_names,
        "train_projects": len(train_idx),
        "test_projects": len(test_idx),
        "train_elevators": len(train_groups),
        "test_elevators": len(test_groups),
        "metrics": {
            name: _metrics(actual, values)
            for name, values in estimates.items()
        },
        "test_predictions": [],
    }

    for position, index in enumerate(test_idx):
        results["test_predictions"].append({
            "case_ref": dataset[index]["case_ref"],
            "elevator_id": groups[index],
            "measured_saving_percent": round(
                float(actual[position]), 3
            ),
            **{
                f"{name}_percent": round(
                    float(values[position]), 3
                )
                for name, values in estimates.items()
            },
        })

    return results


def predict_saving(training_result, enercal_features):
    # Accept only pre-modernisation features for new, unmeasured projects.
    if "predicted_saving_percent" not in enercal_features:
        raise ValueError(
            "An EnerCal saving estimate is required."
        )

    try:
        baseline = float(
            enercal_features["predicted_saving_percent"]
        )

        values = np.array([[
            np.nan if enercal_features.get(name) is None
            else float(enercal_features[name])
            for name in training_result["feature_names"]
        ]], dtype=float)

    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Features must be numeric or None."
        ) from exc

    if (
        not isfinite(baseline)
        or baseline > 100
        or np.isinf(values).any()
    ):
        raise ValueError(
            "Invalid EnerCal input values."
        )

    adjusted = min(
        100,
        baseline + float(
            training_result["model"].predict(values)[0]
        ),
    )

    return {
        "enercal_saving_percent": round(baseline, 3),
        "ai_adjusted_saving_percent": round(adjusted, 3),
        "correction_pp": round(adjusted - baseline, 3),
    }
