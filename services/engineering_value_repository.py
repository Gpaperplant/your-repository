import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from database import get_connection


PHASES = frozenset({"existing", "proposed", "common"})
VALUE_KINDS = frozenset({
    "identity", "engineering_specification", "test_measurement",
    "meter_measurement", "assumption", "synthetic_input",
    "calculated_estimate", "reported_estimate", "metadata",
})
VALUE_COLUMNS = (
    "field_name", "phase", "value_kind", "raw_source_value", "numeric_value",
    "text_value", "unit", "source_page", "excel_sheet", "excel_cell",
    "excel_formula", "source_label", "source_note", "measured_on",
    "period_start", "period_end", "source_verified", "is_corrected",
    "correction_reason",
)
INSERT_COLUMNS = ("revision_id", *VALUE_COLUMNS)
INSERT_SQL = (
    f"INSERT INTO engineering_values ({', '.join(INSERT_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(INSERT_COLUMNS))})"
)


def _positive_id(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer.")


def _optional_text(value, label):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text.")
    return value.strip() or None


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("Numeric value cannot be Boolean.")
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError("Numeric value is invalid.") from error
    if not number.is_finite():
        raise ValueError("Numeric value must be finite.")
    return number


def _date(value, label):
    if value is None:
        return None
    if isinstance(value, datetime):
        raise ValueError(f"{label} must be a date, not a datetime.")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(f"Invalid {label}.") from error
    raise ValueError(f"Invalid {label}.")


def _prepare_value(record):
    if not isinstance(record, dict):
        raise ValueError("Each engineering value must be a dictionary.")

    name = _optional_text(record.get("field_name"), "Field name")
    phase = record.get("phase")
    kind = record.get("value_kind")
    if not name or not re.fullmatch(r"[a-z][a-z0-9_.]*", name):
        raise ValueError("Field name must use lowercase letters, numbers, dots or underscores.")
    if not isinstance(phase, str) or phase not in PHASES:
        raise ValueError("Invalid engineering phase.")
    if not isinstance(kind, str) or kind not in VALUE_KINDS:
        raise ValueError("Invalid engineering value kind.")

    raw = record.get("raw_source_value")
    if raw is not None and not isinstance(raw, str):
        raise ValueError("Raw source value must be text.")
    numeric = _number(record.get("numeric_value"))
    text = _optional_text(record.get("text_value"), "Text value")
    if numeric is not None and text is not None:
        raise ValueError("Provide a numeric value or a text value, not both.")
    if numeric is None and text is None and (raw is None or not raw.strip()):
        raise ValueError("At least one source or engineering value is required.")

    page = record.get("source_page")
    if page is not None:
        _positive_id(page, "Source page")

    sheet = _optional_text(record.get("excel_sheet"), "Excel sheet")
    cell = _optional_text(record.get("excel_cell"), "Excel cell")
    formula = _optional_text(record.get("excel_formula"), "Excel formula")
    if bool(sheet) != bool(cell):
        raise ValueError("Excel sheet and cell must be provided together.")
    if cell:
        cell = cell.upper()
        if not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]*", cell):
            raise ValueError("Invalid Excel cell reference.")
    if formula and (not cell or not formula.startswith("=")):
        raise ValueError("Excel formula requires a source cell and must start with '='.")
    if page is not None and sheet is not None:
        raise ValueError("A value cannot have both PDF and Excel source locations.")

    start = _date(record.get("period_start"), "period start")
    end = _date(record.get("period_end"), "period end")
    if (start is None) != (end is None) or (start and end < start):
        raise ValueError("Measurement period requires valid start and end dates.")
    if kind == "meter_measurement" and start is None:
        raise ValueError("Meter measurements require a measurement period.")

    verified = record.get("source_verified", False)
    corrected = record.get("is_corrected", False)
    if type(verified) is not bool or type(corrected) is not bool:
        raise ValueError("Verification and correction flags must be Boolean.")
    reason = _optional_text(record.get("correction_reason"), "Correction reason")
    if corrected and not reason:
        raise ValueError("Corrected values require a correction reason.")

    return {
        "field_name": name,
        "phase": phase,
        "value_kind": kind,
        "raw_source_value": raw,
        "numeric_value": numeric,
        "text_value": text,
        "unit": _optional_text(record.get("unit"), "Unit"),
        "source_page": page,
        "excel_sheet": sheet,
        "excel_cell": cell,
        "excel_formula": formula,
        "source_label": _optional_text(record.get("source_label"), "Source label"),
        "source_note": _optional_text(record.get("source_note"), "Source note"),
        "measured_on": _date(record.get("measured_on"), "measurement date"),
        "period_start": start,
        "period_end": end,
        "source_verified": verified,
        "is_corrected": corrected,
        "correction_reason": reason,
    }


def insert_engineering_values(cursor, revision_id, values):
    _positive_id(revision_id, "Revision ID")
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("Provide a nonempty list of engineering values.")

    prepared = [_prepare_value(value) for value in values]
    keys = [(value["phase"], value["field_name"]) for value in prepared]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate field and phase in this revision.")

    cursor.execute(
        """
        SELECT r.review_status, EXISTS (
            SELECT 1 FROM engineering_values v WHERE v.revision_id = r.revision_id
        )
        FROM document_revisions r
        WHERE r.revision_id = %s
        FOR UPDATE OF r
        """,
        (revision_id,),
    )
    revision = cursor.fetchone()
    if revision is None:
        raise ValueError("Revision not found.")
    if revision[0] != "pending":
        raise ValueError("Only pending revisions accept engineering values.")
    if revision[1]:
        raise ValueError("This revision already has a value snapshot.")

    cursor.executemany(
        INSERT_SQL,
        [(revision_id, *(value[column] for column in VALUE_COLUMNS)) for value in prepared],
    )
    return {"revision_id": revision_id, "values_saved": len(prepared)}


def save_engineering_values(revision_id, values):
    with get_connection() as connection, connection.cursor() as cursor:
        return insert_engineering_values(cursor, revision_id, values)


def get_revision_values(revision_id):
    _positive_id(revision_id, "Revision ID")
    columns = ("value_id", "revision_id", *VALUE_COLUMNS, "created_at")
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            f"SELECT {', '.join(columns)} FROM engineering_values "
            "WHERE revision_id = %s ORDER BY phase, field_name",
            (revision_id,),
        )
        rows = cursor.fetchall()
    return [dict(zip(columns, row)) for row in rows]
