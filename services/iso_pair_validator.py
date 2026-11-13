
import math
import re
from decimal import Decimal, InvalidOperation


# Major specifications required to verify the same elevator.
IDENTITY_FIELDS = {
    "rated_load_kg": 1,
    "rated_speed_m_s": 0.02,
    "travel_m": 0.2,
    "stops": 0,
}

# Allow small rounding differences between PDF and Excel results.
TOLERANCES = {
    "annual_energy_kwh": 0.05,
    "daily_energy_wh": 0.1,
    "reference_energy_wh": 0.01,
    "short_energy_wh": 0.01,
    "specific_reference_mwh_kg_m": 0.001,
    "avg_running_specific_mwh_kg_m": 0.001,
}


def normalise_lift_id(value):
    # Standardise IDs such as OP13 (L) and OP13(L).
    return re.sub(
        r"[^A-Z0-9]",
        "",
        str(value or "").upper(),
    )


def index_values(records):
    # Convert extracted records into a field lookup.
    values = {}

    for record in records:
        field = record["field"]
        value = record.get("value")

        if field in values:
            raise ValueError(f"Duplicate extracted field: {field}")

        if value is not None and str(value).strip():
            values[field] = value

    return values


def _number(value):
    # Convert numeric values without accepting invalid numbers.
    if isinstance(value, bool):
        return None

    try:
        number = Decimal(str(value).replace(",", "").strip())
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _same(first, second, field):
    # Compare elevator IDs independently from other values.
    if field == "lift_id":
        return normalise_lift_id(first) == normalise_lift_id(second)

    # Compare serial numbers exactly.
    if field == "serial_number":
        return str(first).strip() == str(second).strip()

    # Standardise energy efficiency class labels.
    if field == "efficiency_class":
        def clean(value):
            return str(value).upper().replace("CLASS", "").strip()

        return clean(first) == clean(second)

    a = _number(first)
    b = _number(second)

    # Compare numeric values using field-specific tolerances.
    if a is not None and b is not None:
        tolerance = IDENTITY_FIELDS.get(
            field,
            TOLERANCES.get(field, 0.01),
        )

        return math.isclose(
            float(a),
            float(b),
            rel_tol=0,
            abs_tol=tolerance,
        )

    # Compare text fields without case sensitivity.
    return (
        str(first).strip().casefold()
        == str(second).strip().casefold()
    )


def validate_same_lift(pdf_records, excel_records):
    # Build lookups for both documents.
    pdf = index_values(pdf_records)
    excel = index_values(excel_records)

    pdf_id = normalise_lift_id(pdf.get("lift_id"))
    excel_id = normalise_lift_id(excel.get("lift_id"))

    # Reject missing or different lift IDs.
    if not pdf_id or not excel_id or pdf_id != excel_id:
        raise ValueError(
            f"Lift ID mismatch: "
            f"EE={pdf.get('lift_id')}, "
            f"Excel={excel.get('lift_id')}."
        )

    # Compare serial numbers when both documents provide them.
    if "serial_number" in pdf and "serial_number" in excel:
        if not _same(
            pdf["serial_number"],
            excel["serial_number"],
            "serial_number",
        ):
            raise ValueError("Lift serial numbers do not match.")

    checked_specs = []

    # Reject mismatched major elevator specifications.
    for field in IDENTITY_FIELDS:
        if field not in pdf or field not in excel:
            continue

        checked_specs.append(field)

        if not _same(pdf[field], excel[field], field):
            raise ValueError(
                f"Lift specification mismatch: {field}. "
                f"EE={pdf[field]}, Excel={excel[field]}."
            )

    # Require sufficient supporting evidence of the same lift.
    if len(checked_specs) < 3:
        raise ValueError(
            "Cannot verify lift identity. "
            "At least three matching specifications are required."
        )

    comparisons = []

    # Compare all fields shared by both documents.
    for field in sorted(pdf.keys() & excel.keys()):
        comparisons.append({
            "field": field,
            "pdf_value": str(pdf[field]),
            "excel_value": str(excel[field]),
            "match": _same(pdf[field], excel[field], field),
        })

    # Preserve differences without rejecting a valid lift pair.
    differences = [
        item for item in comparisons
        if not item["match"]
    ]

    # Identify fields available from only one document.
    pdf_only = sorted(pdf.keys() - excel.keys())
    excel_only = sorted(excel.keys() - pdf.keys())

    return {
        "lift_id": pdf["lift_id"],
        "checked_specs": checked_specs,
        "comparisons": comparisons,
        "differences": differences,
        "pdf_only": pdf_only,
        "excel_only": excel_only,
    }
