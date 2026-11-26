"""Notebook 9's frozen Engineering 12 inference, using the five core tables.

Never fits a model or reads ISO After values. Test fixtures are DEMO ONLY.
"""

import math
from pathlib import Path

FEATURES = (
    "MOD_PROXY_savings_pct",
    "MOD_PROXY_energy_existing_kwh_year",
    "MOD_PROXY_energy_proposed_kwh_year",
    "MOD_rated_load_kg",
    "MOD_speed_existing_mps",
    "MOD_travel_distance_m",
    "MOD_usage_category",
    "ISO_PRE_running_energy_wh_per_m",
    "ISO_PRE_start_stop_energy_wh",
    "ISO_PRE_nonrunning_wh_day",
    "ISO_PRE_standby_30min_w",
    "DERIVED_induction_to_pmsm",
)

MODEL_PATH = (Path(__file__).resolve().parents[1] / "models"
              / "xgboost_engineering12_pseudohuber_LOCKED.joblib")
WORKBOOK_SHA256 = "e1bebe88808c07fa7c708628d6c021b6b9cb37104ea09ffb5ced8c5f23f95d1e"

# Exactly one input field per source/phase; ISO After is NOT a model input.
SOURCES = {
    "MOD_PROXY_energy_existing_kwh_year": ("mod_pdf", "existing", "annual_energy_consumption"),
    "MOD_PROXY_energy_proposed_kwh_year": ("mod_pdf", "proposed", "annual_energy_consumption"),
    "MOD_rated_load_kg": ("mod_pdf", "existing", "nominal_load_kg"),
    "MOD_speed_existing_mps": ("mod_pdf", "existing", "nominal_speed_m_s"),
    "MOD_travel_distance_m": ("mod_pdf", "common", "travel_distance_m"),
    "MOD_usage_category": ("mod_pdf", "common", "iso_usage_category"),
    "ISO_PRE_running_energy_wh_per_m": ("iso_excel", "existing", "energy_per_m_wh"),
    "ISO_PRE_start_stop_energy_wh": ("iso_excel", "existing", "start_stop_energy_wh"),
    "ISO_PRE_nonrunning_wh_day": ("iso_excel", "existing", "daily_nonrunning_energy_wh"),
    "ISO_PRE_standby_30min_w": ("iso_excel", "existing", "standby30_power_w"),
}

# Latest document and latest revision ONLY. Source-verification is evaluated
# across ALL snapshot values, not merely the 12 predictors.
CASE_SQL = """
WITH latest AS (
    SELECT DISTINCT ON (d.case_id, d.document_type)
        d.document_id, d.document_type, d.source_classification
    FROM project_documents d
    JOIN modernisation_cases m ON m.case_id = d.case_id
    WHERE m.case_ref = %s AND d.document_type IN ('mod_pdf', 'iso_excel')
    ORDER BY d.case_id, d.document_type, d.document_id DESC
)
SELECT d.document_type, d.source_classification, r.review_scope,
       r.review_status, v.phase, v.field_name, v.numeric_value,
       v.text_value, v.source_verified, v.value_kind, v.source_note
FROM latest d
JOIN LATERAL (
    SELECT revision_id, review_scope, review_status
    FROM document_revisions
    WHERE document_id = d.document_id
    ORDER BY revision_no DESC
    LIMIT 1
) r ON TRUE
LEFT JOIN engineering_values v ON v.revision_id = r.revision_id
ORDER BY d.document_type, v.phase, v.field_name
"""

CASES_SQL = """
SELECT m.case_ref
FROM modernisation_cases m
JOIN project_documents d ON d.case_id = m.case_id
WHERE d.document_type IN ('mod_pdf', 'iso_excel')
GROUP BY m.case_ref
HAVING COUNT(DISTINCT d.document_type) = 2
ORDER BY m.case_ref
"""


def _query(sql, params=None):
    from database import get_connection
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute(sql, params or ())
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def list_cases():
    return [row["case_ref"] for row in _query(CASES_SQL)]


def _motor(value):
    value = (value or "").strip().lower()
    if "pmsm" in value or "permanent magnet" in value:
        return "PMSM"
    if "induction" in value:
        return "Induction"
    return None


def prepare_features(rows):
    """Return (ordered feature dict, classification) or a clear error."""
    docs, values = {}, {}
    for row in rows:
        kind = row["document_type"]
        if kind not in ("mod_pdf", "iso_excel"):
            raise ValueError("Unexpected source document type.")
        meta = (row["source_classification"], row["review_scope"], row["review_status"])
        if kind in docs and docs[kind] != meta:
            raise ValueError("Inconsistent document revision metadata.")
        docs[kind] = meta
        if row["field_name"] is not None:
            key = (kind, row["phase"], row["field_name"])
            if key in values:
                raise ValueError(f"Duplicate engineering value: {key}")
            values[key] = row

    if set(docs) != {"mod_pdf", "iso_excel"}:
        raise ValueError("Upload both a MOD PDF and an ISO Excel workbook first.")
    if any(status != "approved" for _, _, status in docs.values()):
        raise ValueError("Both latest MOD and ISO revisions must be approved before prediction.")

    classifications = {c for c, _, _ in docs.values()}
    if classifications == {"real"}:
        if any(scope != "full_values" for _, scope, _ in docs.values()):
            raise ValueError("Real evidence requires full-values approval of both documents.")
        if not values or any(not row["source_verified"] or
                             row["value_kind"] == "synthetic_input" or
                             "synthetic before" in (row["source_note"] or "").lower()
                             for row in values.values()):
            raise ValueError("Real evidence contains unverified or synthetic engineering values.")
        classification = "real_verified"
    elif classifications == {"test_fixture"}:
        classification = "test_fixture_demo"
    else:
        raise ValueError("Documents must both be verified real evidence or both test fixtures.")

    features = {}
    for name, key in SOURCES.items():
        row = values.get(key)
        if row is None or row["numeric_value"] is None:
            raise ValueError(f"Missing required Engineering 12 input: {name}.")
        try:
            number = float(row["numeric_value"])
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid numeric input: {name}.") from exc
        if not math.isfinite(number):
            raise ValueError(f"Non-finite numeric input: {name}.")
        features[name] = number

    before = features["MOD_PROXY_energy_existing_kwh_year"]
    after = features["MOD_PROXY_energy_proposed_kwh_year"]
    if before <= 0 or after < 0:
        raise ValueError("MOD annual energy inputs must be nonnegative and existing > 0.")
    features["MOD_PROXY_savings_pct"] = 100 * (before - after) / before
    existing = values.get(("mod_pdf", "existing", "drive_type"))
    proposed = values.get(("mod_pdf", "proposed", "drive_type"))
    existing_motor = _motor(existing["text_value"] if existing else None)
    proposed_motor = _motor(proposed["text_value"] if proposed else None)
    if not existing_motor or not proposed_motor:
        raise ValueError("Cannot derive motor change: MOD drive types are missing or unknown.")
    features["DERIVED_induction_to_pmsm"] = int(
        existing_motor == "Induction" and proposed_motor == "PMSM")
    if features["MOD_rated_load_kg"] <= 0 or features["MOD_speed_existing_mps"] <= 0:
        raise ValueError("MOD load and speed must be positive.")
    if any(features[k] < 0 for k in SOURCES if k not in (
            "MOD_rated_load_kg", "MOD_speed_existing_mps")):
        raise ValueError("Negative engineering input found.")
    return {name: features[name] for name in FEATURES}, classification


def load_case_features(case_ref):
    if not isinstance(case_ref, str) or not case_ref.strip():
        raise ValueError("Select a modernisation case.")
    return prepare_features(_query(CASE_SQL, (case_ref.strip(),)))


def predict(features, *, model_path=MODEL_PATH):
    """Only locally trusted Notebook 9 LOCKED bundles; never retrain."""
    import joblib  # Loading joblib is executable; file must be locally trusted.
    import pandas as pd

    path = Path(model_path)
    if not path.is_file() or path.is_symlink() or path.name != MODEL_PATH.name:
        raise FileNotFoundError("Place the trusted Notebook 9 LOCKED.joblib file in models/.")
    if set(features) != set(FEATURES):
        raise ValueError("Engineering 12 feature names do not match Notebook 9.")
    bundle = joblib.load(path)
    if (not isinstance(bundle, dict)
            or bundle.get("model_id") != "engineering12_pseudohuber"
            or bundle.get("selected_features") != list(FEATURES)
            or bundle.get("target") != "TARGET_savings_gap_pp"
            or bundle.get("workbook_sha256") != WORKBOOK_SHA256
            or bundle.get("model_selection_stage") !=
               "locked_for_independent_test_not_yet_independently_validated"
            or float(bundle.get("optional_conservative_offset_pp", -1)) != 2.5
            or float(bundle.get("accuracy_offset_pp", -1)) != 0.0):
        raise ValueError("The saved file is not the approved frozen Notebook 9 model.")

    ordered = pd.DataFrame([{key: features[key] for key in FEATURES}], columns=list(FEATURES))
    gap = float(bundle["model"].predict(ordered)[0])
    if not math.isfinite(gap):
        raise ValueError("Model returned a non-finite prediction.")
    mod = float(features["MOD_PROXY_savings_pct"])
    return {
        "mod_savings": mod,
        "gap_pp": gap,
        "ai_savings": mod + gap,
        "conservative_savings": mod + gap - 2.5,
    }
