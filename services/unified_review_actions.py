"""Transactional engineering comparison reviews for unified documents."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from database import get_connection
from services.document_revision_repository import insert_revision
from services.engineering_value_repository import VALUE_COLUMNS, insert_engineering_values
FIELD_LABELS = {
    "existing_elevator_technology": "Existing elevator technology",
    "proposed_elevator_technology": "Proposed elevator technology",
    "existing_annual_energy_consumption": "Existing annual energy (kWh/year)",
    "proposed_annual_energy_consumption": "Proposed annual energy (kWh/year)",
    "existing_annual_regenerative_energy": "Existing regenerative energy (kWh/year)",
    "proposed_annual_regenerative_energy": "Proposed regenerative energy (kWh/year)",
    "existing_annual_electricity_expenditure": "Existing annual electricity cost",
    "proposed_annual_electricity_expenditure": "Proposed annual electricity cost",
    "existing_lifetime_electricity_expenditure": "Existing lifetime electricity cost",
    "proposed_lifetime_electricity_expenditure": "Proposed lifetime electricity cost",
    "existing_operational_co2e": "Existing operational CO₂e (kg)",
    "proposed_operational_co2e": "Proposed operational CO₂e (kg)",
    "existing_embodied_co2e": "Existing embodied CO₂e (kg)",
    "proposed_embodied_co2e": "Proposed embodied CO₂e (kg)",
    "reported_energy_saving_percent": "EnerCal reported saving (%)",
    "calculated_energy_saving_percent": "Calculated saving (%)",
}
EXPECTED_FIELDS = frozenset(FIELD_LABELS)
CALCULATED_FIELD = "calculated_energy_saving_percent"
TECHNOLOGY_FIELDS = frozenset({
    "existing_elevator_technology", "proposed_elevator_technology",
})
OPTIONAL_NUMERIC_FIELDS = frozenset({
    "existing_annual_regenerative_energy", "proposed_annual_regenerative_energy",
    "existing_embodied_co2e", "proposed_embodied_co2e",
})


def _positive_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Document ID must be a positive integer.")


def _latest(cursor, document_id):
    cursor.execute(
        """
        SELECT d.document_type, d.source_classification, r.revision_id,
               r.revision_no, r.review_scope, r.review_status,
               EXISTS (
                   SELECT 1 FROM project_documents newer
                   WHERE newer.case_id = d.case_id
                     AND newer.document_type = d.document_type
                     AND newer.document_id > d.document_id
               ) AS superseded
        FROM project_documents d
        JOIN modernisation_cases m ON m.case_id = d.case_id
        JOIN document_revisions r ON r.document_id = d.document_id
        WHERE d.document_id = %s
        ORDER BY r.revision_no DESC LIMIT 1
        FOR UPDATE OF m
        """,
        (document_id,),
    )
    row = cursor.fetchone()
    if row is None:
        raise ValueError("Document or revision not found.")
    kind, classification, revision_id, revision_no, scope, status, superseded = row
    if superseded:
        raise ValueError("Only the latest document of this type can be reviewed.")
    return {
        "document_type": kind, "source_classification": classification,
        "revision_id": revision_id, "revision_no": revision_no,
        "review_scope": scope, "review_status": status,
    }


def _values(cursor, revision_id):
    cursor.execute(
        f"SELECT {', '.join(VALUE_COLUMNS)} FROM engineering_values "
        "WHERE revision_id = %s ORDER BY phase, field_name",
        (revision_id,),
    )
    rows = cursor.fetchall()
    if not rows:
        raise ValueError("The revision has no engineering values.")
    return [dict(zip(VALUE_COLUMNS, row, strict=True)) for row in rows]


def _decision(cursor, revision_id, decision, note, reviewer_id):
    cursor.execute(
        "SELECT review_document_revision(%s, %s, %s, %s, FALSE)",
        (revision_id, decision, note, reviewer_id),
    )
    row = cursor.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("The review event was not created.")
    return row[0]


def submit_comparison_review(document_id, decision, note, reviewer_id=None):
    """Review the latest comparison, without approving unverified full values."""
    _positive_id(document_id)
    if decision not in {"approved", "rejected", "changes_requested", "further_verification_required"}:
        raise ValueError("Invalid review decision.")
    if not isinstance(note, str):
        raise ValueError("Review note must be text.")
    note = note.strip()
    if decision != "approved" and not note:
        raise ValueError("A reason is required for this decision.")
    reviewer_id = str(reviewer_id).strip() if reviewer_id is not None else None
    reviewer_id = reviewer_id or None

    with get_connection() as connection, connection.cursor() as cursor:
        latest = _latest(cursor, document_id)
        if latest["document_type"] not in {"mod_pdf", "iso_excel"}:
            raise ValueError("Only MOD and ISO comparisons can be reviewed here.")
        if latest["review_status"] != "pending":
            raise ValueError("Only the latest pending revision can be reviewed.")

        revision_id = latest["revision_id"]
        revision_no = latest["revision_no"]
        if latest["review_scope"] == "full_values" and decision in {"approved", "rejected"}:
            values = _values(cursor, revision_id)
            _decision(
                cursor, revision_id, "further_verification_required",
                "Comparison review separated; full engineering values remain unverified.",
                reviewer_id,
            )
            next_revision = insert_revision(cursor, document_id, "comparison_only")
            insert_engineering_values(cursor, next_revision["revision_id"], values)
            revision_id = next_revision["revision_id"]
            revision_no = next_revision["revision_no"]

        review_id = _decision(cursor, revision_id, decision, note or None, reviewer_id)

    return {
        "document_id": document_id, "revision_id": revision_id,
        "revision_no": revision_no, "review_status": decision,
        "review_id": review_id,
    }


def _parse_field(field_name, supplied):
    if not isinstance(supplied, str) or not supplied.strip():
        raise ValueError(f"{FIELD_LABELS[field_name]} cannot be blank.")
    raw = supplied.strip()
    if field_name in TECHNOLOGY_FIELDS:
        return raw, None, raw
    if raw.upper() in {"N/A", "NA"}:
        if field_name in OPTIONAL_NUMERIC_FIELDS:
            return "N/A", None, None
        raise ValueError(f"{FIELD_LABELS[field_name]} requires a number.")
    try:
        number = Decimal(raw.replace(",", "").removesuffix("%"))
    except InvalidOperation as error:
        raise ValueError(f"Invalid numeric value for {FIELD_LABELS[field_name]}.") from error
    if not number.is_finite():
        raise ValueError("Engineering values must be finite.")
    if number < 0 and field_name != "reported_energy_saving_percent":
        raise ValueError(f"{FIELD_LABELS[field_name]} cannot be negative.")
    if number != number.quantize(Decimal("0.001")):
        raise ValueError("MOD comparison values support at most three decimal places.")
    return raw, number, None


def _key(field_name):
    for phase in ("existing", "proposed"):
        prefix = f"{phase}_"
        if field_name.startswith(prefix):
            return phase, field_name[len(prefix):]
    return "common", field_name


def _corrected_values(values, corrected_fields, reason):
    if not isinstance(corrected_fields, dict) or set(corrected_fields) != EXPECTED_FIELDS:
        raise ValueError("Submit all 16 MOD comparison fields.")
    updated = [dict(value) for value in values]
    lookup = {(v["phase"], v["field_name"]): v for v in updated}
    if len(lookup) != len(updated):
        raise ValueError("Duplicate engineering fields in revision.")

    changes = 0
    parsed = {}
    for name in EXPECTED_FIELDS - {CALCULATED_FIELD}:
        key = _key(name)
        if key not in lookup:
            raise ValueError(f"Missing MOD comparison field: {name}.")
        raw, numeric, text = _parse_field(name, corrected_fields[name])
        previous = lookup[key]
        is_changed = (
            (text != previous["text_value"]) if name in TECHNOLOGY_FIELDS
            else (numeric != previous["numeric_value"])
        )
        if is_changed:
            changes += 1
            previous.update(
                numeric_value=numeric,
                text_value=text,
                source_verified=False,
                is_corrected=True,
                correction_reason=reason,
            )
        parsed[name] = (raw, numeric, text)

    if not changes:
        raise ValueError("No comparison values changed.")
    before = parsed["existing_annual_energy_consumption"][1]
    after = parsed["proposed_annual_energy_consumption"][1]
    if before is None or before <= 0 or after is None:
        raise ValueError("Annual energy values are invalid.")
    saving = ((before - after) * 100 / before).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    common_key = ("common", CALCULATED_FIELD)
    if common_key not in lookup:
        raise ValueError("Calculated energy saving is missing.")
    calculated = lookup[common_key]
    if calculated["numeric_value"] != saving:
        calculated.update(
            numeric_value=saving,
            text_value=None,
            source_verified=False,
            is_corrected=True,
            correction_reason=reason,
        )
    return updated


def submit_mod_correction(document_id, corrected_fields, reason):
    """Create a complete immutable revised snapshot after a comparison correction."""
    _positive_id(document_id)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A correction reason is required.")
    reason = reason.strip()
    with get_connection() as connection, connection.cursor() as cursor:
        latest = _latest(cursor, document_id)
        if latest["document_type"] != "mod_pdf":
            raise ValueError("Corrections here require a MOD PDF.")
        if latest["review_status"] not in {
            "pending", "changes_requested", "further_verification_required"
        }:
            raise ValueError("Only an unapproved revision can be corrected.")
        values = _values(cursor, latest["revision_id"])
        updated = _corrected_values(values, corrected_fields, reason)
        if latest["review_status"] == "pending":
            _decision(cursor, latest["revision_id"], "changes_requested", reason, None)
        next_revision = insert_revision(cursor, document_id, "comparison_only")
        insert_engineering_values(cursor, next_revision["revision_id"], updated)

    return {
        "document_id": document_id,
        "revision_id": next_revision["revision_id"],
        "revision_no": next_revision["revision_no"],
        "review_status": "pending",
        "values_saved": len(updated),
    }
