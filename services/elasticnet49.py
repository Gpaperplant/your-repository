
"""Predict savings with the frozen Notebook 14 ElasticNet."""

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

from services.engineering12 import (
    CASE_SQL, WORKBOOK_SHA256, _query, prepare_features
)
from services.iso_value_mapper import (
    ISO_PRE_FEATURES, iso_pre_feature_record
)
from services.mod_value_mapper import (
    MOD_FEATURES, mod_feature_record
)

ROOT = Path(__file__).resolve().parents[1]
MODEL_PATH = ROOT / "models" / "elasticnet49_selected_scaler_train.joblib"
MANIFEST_PATH = ROOT / "models" / "notebook14_manifest.json"
MODEL_SHA256 = "cdeab56c50549004bc1ec0ed2f13cc05c4ec70c522de66c751ed6330e0046254"

FEATURES = MOD_FEATURES + ISO_PRE_FEATURES

CATEGORIES = {
    "MOD_gear_existing",
    "MOD_gear_proposed",
    "MOD_drive_type_existing",
    "MOD_drive_type_proposed",
    "MOD_motor_technology_existing",
    "MOD_motor_technology_proposed",
    "MOD_roping_existing",
    "MOD_roping_proposed",
    "ISO_PRE_energy_class",
}


def _manifest():
    if not MANIFEST_PATH.is_file() or MANIFEST_PATH.is_symlink():
        raise FileNotFoundError("Missing Notebook 14 manifest")

    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    if (
        data.get("input_features") != list(FEATURES)
        or data.get("n_features") != 49
        or data.get("target") != "TARGET_savings_gap_pp"
        or data.get("workbook_sha256") != WORKBOOK_SHA256
        or data.get("train_nested_cv_selected_scale") != "Robust"
        or data.get("model_trained_on") != "TRAIN only"
    ):
        raise ValueError("Invalid Notebook 14 manifest")

    return data


def _validate(features):
    if set(features) != set(FEATURES):
        raise ValueError("ElasticNet requires exactly 49 inputs")

    clean = {}

    for name in FEATURES:
        value = features[name]

        if name in CATEGORIES:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Invalid category: {name}")

            clean[name] = value.strip()

        else:
            try:
                number = float(value)
            except (ValueError, TypeError, OverflowError) as exc:
                raise ValueError(f"Invalid number: {name}") from exc

            if not math.isfinite(number):
                raise ValueError(f"Non-finite number: {name}")

            clean[name] = number

    return clean


def prepare_case_features(rows):
    # Reuse existing document approval checks.
    _, evidence = prepare_features(rows)

    mod = [
        r for r in rows
        if r["document_type"] == "mod_pdf"
        and r["field_name"] is not None
    ]

    iso = [
        r for r in rows
        if r["document_type"] == "iso_excel"
        and r["field_name"] is not None
    ]

    features = {
        **mod_feature_record(mod),
        **iso_pre_feature_record(
            iso,
            allow_synthetic=evidence == "test_fixture_demo",
        ),
    }

    return _validate(features), evidence


def load_case_features(case_ref):
    if not isinstance(case_ref, str) or not case_ref.strip():
        raise ValueError("Select a modernisation case")

    rows = _query(CASE_SQL, (case_ref.strip(),))
    return prepare_case_features(rows)


@lru_cache(maxsize=1)
def _pipeline():
    # Load only the trusted model file.
    import joblib
    import sklearn

    manifest = _manifest()
    version = manifest["versions"]["sklearn"]

    if sklearn.__version__ != version:
        raise RuntimeError(f"ElasticNet requires scikit-learn {version}")

    if (
        not MODEL_PATH.is_file()
        or MODEL_PATH.is_symlink()
        or hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest()
        != MODEL_SHA256
    ):
        raise FileNotFoundError("ElasticNet model missing or modified")

    model = joblib.load(MODEL_PATH)
    names = getattr(model, "feature_names_in_", None)

    if not hasattr(model, "predict"):
        raise ValueError("Invalid ElasticNet pipeline")

    if names is not None and list(names) != list(FEATURES):
        raise ValueError("Incorrect model feature order")

    return model


def predict(features):
    import pandas as pd

    values = _validate(features)
    frame = pd.DataFrame([values], columns=list(FEATURES))

    gap = float(_pipeline().predict(frame)[0])

    if not math.isfinite(gap):
        raise ValueError("Invalid ElasticNet prediction")

    mod = values["MOD_PROXY_savings_pct"]

    return {
        "mod_savings": mod,
        "gap_pp": gap,
        "ai_savings": mod + gap,
    }


def predict_case(case_ref):
    features, evidence = load_case_features(case_ref)
    return predict(features), evidence
