"""Read verified MOD/ISO AI feature views without modifying source evidence."""

import hashlib
import json
import math
from collections.abc import Mapping
from decimal import Decimal

from database import get_connection
from services.iso_value_mapper import ISO_PRE_FEATURES
from services.mod_value_mapper import MOD_FEATURES


TARGET = "TARGET_savings_gap_pp"
FEATURE_SETS = {26: MOD_FEATURES, 49: MOD_FEATURES + ISO_PRE_FEATURES}
VIEWS = {
    (26, False): "fyp_ai_mod_features_26",
    (49, False): "fyp_ai_features_49",
    (26, True): "fyp_ai_train_mod_26",
    (49, True): "fyp_ai_train_49",
}
METADATA = {
    (26, False): (
        "case_id", "case_ref", "elevator_ref", "mod_document_id",
        "mod_revision_id", "source_classification",
    ),
    (49, False): (
        "case_id", "case_ref", "elevator_ref", "mod_document_id",
        "mod_revision_id", "iso_document_id", "iso_revision_id",
        "source_classification",
    ),
    (26, True): (
        "case_id", "case_ref", "elevator_ref", "mod_document_id",
        "mod_revision_id", "iso_revision_id", "source_classification",
    ),
    (49, True): (
        "case_id", "case_ref", "elevator_ref", "mod_document_id",
        "mod_revision_id", "iso_document_id", "iso_revision_id",
        "source_classification",
    ),
}
CATEGORICAL = frozenset({
    "MOD_gear_existing", "MOD_gear_proposed",
    "MOD_drive_type_existing", "MOD_drive_type_proposed",
    "MOD_motor_technology_existing", "MOD_motor_technology_proposed",
    "MOD_roping_existing", "MOD_roping_proposed", "ISO_PRE_energy_class",
})


def feature_names(feature_count=49):
    if type(feature_count) is not int or feature_count not in FEATURE_SETS:
        raise ValueError("Feature count must be 26 or 49.")
    return FEATURE_SETS[feature_count]


def _settings(feature_count, training):
    names = feature_names(feature_count)
    if type(training) is not bool:
        raise ValueError("Training must be Boolean.")
    return names, METADATA[(feature_count, training)], VIEWS[(feature_count, training)]


def _number(value, field, case_id):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"Invalid numeric {field} for case {case_id}.")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"Invalid numeric {field} for case {case_id}.") from None
    if not math.isfinite(number):
        raise ValueError(f"Non-finite {field} for case {case_id}.")
    return number


def _positive_id(value, field, case_id):
    if type(value) is not int or value <= 0:
        raise ValueError(f"Invalid {field} for case {case_id}.")
    return value


def prepare_ai_rows(rows, feature_count=49, *, training=False):
    """Validate view rows and keep feature order and evidence identifiers."""
    features, metadata, _ = _settings(feature_count, training)
    if not isinstance(rows, (list, tuple)):
        raise ValueError("Dataset rows must be a list or tuple.")

    expected = (*metadata, *features, *((TARGET,) if training else ()))
    prepared, seen_cases = [], set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("Every dataset row must be a mapping.")
        if set(row) != set(expected):
            missing = set(expected) - set(row)
            unexpected = set(row) - set(expected)
            raise ValueError(
                f"Dataset view columns mismatch: missing={sorted(missing)}, "
                f"unexpected={sorted(unexpected)}."
            )

        case_id = _positive_id(row["case_id"], "case_id", row.get("case_id"))
        if case_id in seen_cases:
            raise ValueError(f"Duplicate AI case: {case_id}.")
        seen_cases.add(case_id)

        if row["source_classification"] != "real":
            raise ValueError(f"Case {case_id} is not verified real evidence.")
        record = {"case_id": case_id}
        for field in metadata[1:]:
            value = row[field]
            if field.endswith("_id"):
                value = _positive_id(value, field, case_id)
            elif not isinstance(value, str) or not value.strip():
                raise ValueError(f"Invalid {field} for case {case_id}.")
            else:
                value = value.strip()
            record[field] = value

        for field in features:
            value = row[field]
            if field in CATEGORICAL:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"Missing categorical {field} for case {case_id}.")
                value = value.strip()
                if field == "ISO_PRE_energy_class" and value not in "ABCDEFG":
                    raise ValueError(f"Invalid ISO efficiency class for case {case_id}.")
            else:
                value = _number(value, field, case_id)
            record[field] = value

        if training:
            record[TARGET] = _number(row[TARGET], TARGET, case_id)
        prepared.append(record)
    return prepared


def load_ai_dataset(feature_count=49, *, training=False, case_ref=None, limit=None):
    """Read only the latest fully approved, real, verified SQL-view records."""
    features, metadata, view = _settings(feature_count, training)
    if case_ref is not None:
        if not isinstance(case_ref, str) or not case_ref.strip():
            raise ValueError("Case reference must be nonempty text.")
        case_ref = case_ref.strip()
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 100000):
        raise ValueError("Limit must be an integer from 1 to 100000.")

    sql = f"SELECT * FROM {view}"
    parameters = []
    if case_ref is not None:
        sql += " WHERE case_ref = %s"
        parameters.append(case_ref)
    sql += " ORDER BY case_id"
    if limit is not None:
        sql += " LIMIT %s"
        parameters.append(limit)

    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute(sql, tuple(parameters))
        columns = [column.name for column in cursor.description]
        if len(columns) != len(set(columns)):
            raise ValueError("AI view returned duplicate column names.")
        expected = (*metadata, *features, *((TARGET,) if training else ()))
        if tuple(columns) != expected:
            raise ValueError("AI view column order does not match the feature contract.")
        rows = []
        for values in cursor.fetchall():
            if len(values) != len(columns):
                raise ValueError("AI view returned an incomplete database row.")
            rows.append(dict(zip(columns, values)))
    return prepare_ai_rows(rows, feature_count, training=training)


def dataset_fingerprint(rows, feature_count=49, *, training=False):
    """Hash the feature contract, source revisions and validated rows."""
    features, metadata, _ = _settings(feature_count, training)
    prepared = prepare_ai_rows(rows, feature_count, training=training)
    ordered = sorted(prepared, key=lambda row: row["case_id"])
    payload = {
        "contract": "fyp-ai-49-v1",
        "features": features,
        "metadata": metadata,
        "training": training,
        "rows": ordered,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_training_matrix(rows, feature_count=49):
    """Return X, y and case groups; keep groups for leakage-safe splitting."""
    features = feature_names(feature_count)
    prepared = prepare_ai_rows(rows, feature_count, training=True)
    if not prepared:
        raise ValueError("No eligible real, approved training records were found.")
    return {
        "feature_names": features,
        "X": [[row[field] for field in features] for row in prepared],
        "y": [row[TARGET] for row in prepared],
        "groups": [row["case_id"] for row in prepared],
        "fingerprint": dataset_fingerprint(prepared, feature_count, training=True),
    }


def load_training_matrix(feature_count=49, *, case_ref=None, limit=None):
    """Load eligible labeled rows and return the model-ready matrices."""
    rows = load_ai_dataset(
        feature_count, training=True, case_ref=case_ref, limit=limit
    )
    return build_training_matrix(rows, feature_count)
