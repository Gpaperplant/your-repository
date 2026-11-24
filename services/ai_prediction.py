"""Load trusted FYP models and predict the ISO-minus-MOD savings gap."""

import json
import math
import re
from pathlib import Path
from decimal import Decimal

import joblib
import sklearn
from sklearn.pipeline import Pipeline

from services.ai_dataset import CATEGORICAL, TARGET, feature_names, load_ai_dataset
from services.ai_training import predict_savings_gap

MODEL_FILES = {26: "ridge_26.joblib", 49: "ridge_49.joblib"}
METADATA_FILE = "evaluation.json"


def _model_directory(directory):
    if not isinstance(directory, (str, Path)) or not str(directory).strip():
        raise ValueError("Model directory must be a nonempty path.")
    path = Path(directory).expanduser()
    if not path.is_dir():
        raise FileNotFoundError(f"Model directory not found: {path}")
    return path


def _metadata(directory, feature_count):
    metadata_path = directory / METADATA_FILE
    if not metadata_path.is_file() or metadata_path.is_symlink():
        raise FileNotFoundError("Model evaluation.json was not found as a regular file.")
    if metadata_path.stat().st_size > 1_048_576:
        raise ValueError("Model metadata is unexpectedly large.")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as error:
        raise ValueError("Model evaluation.json is unreadable or invalid.") from error
    if not isinstance(metadata, dict):
        raise ValueError("Model metadata must be a JSON object.")
    if metadata.get("target_name") != TARGET:
        raise ValueError("Model target does not match the savings-gap contract.")
    names = list(feature_names(feature_count))
    if not isinstance(metadata.get("feature_names"), dict) or (
        metadata["feature_names"].get(str(feature_count)) != names
    ):
        raise ValueError("Model feature names or order do not match the current contract.")
    if metadata.get("sklearn_version") != sklearn.__version__:
        raise ValueError(
            "Model scikit-learn version differs from this environment. "
            "Use the original training environment or retrain."
        )
    fingerprints = metadata.get("fingerprints")
    if not isinstance(fingerprints, dict) or not isinstance(
        fingerprints.get(str(feature_count)), str
    ) or not re.fullmatch(r"[0-9a-f]{64}", fingerprints[str(feature_count)]):
        raise ValueError("Model training fingerprint is missing or invalid.")
    metrics = metadata.get("metrics")
    if not isinstance(metrics, dict) or not isinstance(
        metrics.get(str(feature_count)), dict
    ):
        raise ValueError("Model evaluation metrics are missing.")
    mae = metrics[str(feature_count)].get("mae_pp")
    if isinstance(mae, bool) or not isinstance(mae, (int, float)) or (
        not math.isfinite(mae) or mae < 0
    ):
        raise ValueError("Model evaluation MAE is invalid.")
    cases = metadata.get("matched_case_count")
    if type(cases) is not int or cases < 1:
        raise ValueError("Model training case count is invalid.")
    return metadata


def load_saved_model(directory, feature_count=49):
    """Load a model produced locally by save_training_artifacts.

    Joblib uses pickle: never pass untrusted or downloaded model directories.
    """
    names = feature_names(feature_count)
    folder = _model_directory(directory)
    metadata = _metadata(folder, feature_count)
    model_path = folder / MODEL_FILES[feature_count]
    if not model_path.is_file() or model_path.is_symlink():
        raise FileNotFoundError(f"Saved Ridge model not found: {model_path}")
    model = joblib.load(model_path)
    if not isinstance(model, Pipeline) or not hasattr(model, "predict"):
        raise ValueError("Saved model is not a scikit-learn pipeline.")
    if getattr(model, "n_features_in_", None) != len(names):
        raise ValueError("Saved model feature count does not match its metadata.")
    return {"model": model, "feature_count": feature_count, "metadata": metadata}


def _normalise_features(feature_row, feature_count):
    names = feature_names(feature_count)
    if not isinstance(feature_row, dict) or set(feature_row) != set(names):
        raise ValueError("Prediction must contain exactly the selected feature names.")
    normalised = {}
    for name in names:
        value = feature_row[name]
        if name in CATEGORICAL:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Invalid categorical prediction feature: {name}.")
            value = value.strip()
            if name == "ISO_PRE_energy_class" and (len(value) != 1 or value not in "ABCDEFG"):
                raise ValueError("Invalid ISO energy class.")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                raise ValueError(f"Invalid numeric prediction feature: {name}.")
            try:
                value = float(value)
            except (OverflowError, ValueError) as error:
                raise ValueError(f"Invalid numeric prediction feature: {name}.") from error
            if not math.isfinite(value):
                raise ValueError(f"Invalid numeric prediction feature: {name}.")
        normalised[name] = value
    return normalised


def _predict(bundle, feature_row, source):
    count = bundle["feature_count"]
    gap = float(predict_savings_gap(bundle["model"], feature_row, count))
    if not math.isfinite(gap):
        raise ValueError("Model returned a non-finite prediction.")
    mod_savings = float(feature_row["MOD_PROXY_savings_pct"])
    estimated_iso = mod_savings + gap
    metadata = bundle["metadata"]
    result = {
        "feature_count": count,
        "source": source,
        "predicted_savings_gap_pp": gap,
        "mod_estimated_savings_percent": mod_savings,
        "predicted_iso_savings_percent": estimated_iso,
        "evaluation_mae_pp": float(metadata["metrics"][str(count)]["mae_pp"]),
        "trained_case_count": metadata["matched_case_count"],
        "training_fingerprint": metadata["fingerprints"][str(count)],
    }
    if not 0 <= estimated_iso <= 100:
        result["warning"] = (
            "Predicted ISO saving is outside 0–100%; the estimate was not clipped."
        )
    return result


def predict_features(feature_row, model_directory, feature_count=49):
    """Predict using manual features; inputs are not evidence-verified here."""
    validated = _normalise_features(feature_row, feature_count)
    bundle = load_saved_model(model_directory, feature_count)
    return _predict(bundle, validated, "manual_unverified_features")


def predict_case_from_database(case_ref, model_directory, feature_count=49):
    """Predict for one real case eligible in the approved SQL prediction view."""
    if not isinstance(case_ref, str) or not case_ref.strip():
        raise ValueError("Case reference must be nonempty text.")
    bundle = load_saved_model(model_directory, feature_count)
    rows = load_ai_dataset(feature_count, training=False, case_ref=case_ref.strip())
    if not rows:
        raise ValueError("No fully verified real prediction record exists for this case.")
    if len(rows) != 1:
        raise ValueError("Expected exactly one approved AI row for this case.")
    row = rows[0]
    features = _normalise_features(
        {name: row[name] for name in feature_names(feature_count)}, feature_count
    )
    result = _predict(bundle, features, "verified_real_database_case")
    result.update({
        key: row[key]
        for key in (
            "case_id", "case_ref", "elevator_ref", "mod_document_id", "mod_revision_id",
            *(("iso_document_id", "iso_revision_id") if feature_count == 49 else ()),
        )
    })
    return result
