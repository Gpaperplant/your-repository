"""Store ISO Excel evidence without changing historical assessments."""

import re
from decimal import Decimal, InvalidOperation

from database import get_connection


def normalise_lift_id(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def get_cases():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT case_ref FROM modernisation_cases ORDER BY case_ref")
        return [row[0] for row in cur.fetchall()]


def get_case_elevator(case_ref):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT m.case_id, e.elevator_ref
            FROM modernisation_cases m
            JOIN elevators e ON e.elevator_id = m.elevator_id
            WHERE m.case_ref = %s
            """,
            (case_ref,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError("Modernisation case or linked elevator not found.")
    return row


def get_latest_iso_assessment(case_ref):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.assessment_id, a.lift_id, a.imported_at
            FROM iso_assessments a
            JOIN modernisation_cases m ON m.case_id = a.case_id
            WHERE m.case_ref = %s
            ORDER BY a.imported_at DESC, a.assessment_id DESC
            LIMIT 1
            """,
            (case_ref,),
        )
        return cur.fetchone()


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _prepare_values(records):
    prepared, seen = [], set()
    for record in records:
        field = record["field"]
        if not field or field in seen:
            raise ValueError(f"Duplicate or invalid field: {field}")
        seen.add(field)
        value = record.get("value")
        prepared.append((
            field,
            None if value is None else str(value),
            _number(value),
            record.get("unit"),
            record["kind"],
            record.get("page"),
            record.get("sheet"),
            record.get("cell"),
            record.get("formula"),
            record.get("label"),
            record.get("note"),
        ))
    if not prepared:
        raise ValueError("The workbook did not provide any extracted fields.")
    return prepared


def save_iso_excel_assessment(case_id, lift_id, filename, file_bytes, records, checksum):
    """Save an ISO workbook and its values atomically; retain prior assessments."""
    prepared = _prepare_values(records)
    if len(checksum) != 64:
        raise ValueError("Invalid ISO workbook checksum.")

    with get_connection() as conn, conn.cursor() as cur:
        # The case lock serializes duplicate checks and assessment creation.
        cur.execute(
            "SELECT case_id FROM modernisation_cases WHERE case_id = %s FOR UPDATE",
            (case_id,),
        )
        if cur.fetchone() is None:
            raise ValueError("Modernisation case not found.")

        cur.execute(
            """
            SELECT assessment_id, lift_id
            FROM iso_assessments
            WHERE case_id = %s
            ORDER BY imported_at DESC, assessment_id DESC
            LIMIT 1
            """,
            (case_id,),
        )
        latest = cur.fetchone()
        if latest and normalise_lift_id(latest[1]) != normalise_lift_id(lift_id):
            raise ValueError(f"This case already contains ISO data for {latest[1]}.")

        cur.execute(
            """
            SELECT assessment_id
            FROM iso_assessments
            WHERE case_id = %s AND excel_sha256 = %s
            ORDER BY assessment_id DESC
            LIMIT 1
            """,
            (case_id, checksum),
        )
        existing = cur.fetchone()
        if existing:
            return {"assessment_id": existing[0], "created": False}

        # NULL distinguishes Excel-only records from historical paired uploads.
        cur.execute(
            """
            INSERT INTO iso_assessments (case_id, lift_id, pdf_sha256, excel_sha256)
            VALUES (%s, %s, NULL, %s)
            RETURNING assessment_id
            """,
            (case_id, lift_id, checksum),
        )
        assessment_id = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO iso_documents (
                assessment_id, document_type, filename, file_sha256, content
            ) VALUES (%s, 'iso_excel', %s, %s, %s)
            RETURNING document_id
            """,
            (assessment_id, filename, checksum, file_bytes),
        )
        document_id = cur.fetchone()[0]
        cur.executemany(
            """
            INSERT INTO iso_extracted_values (
                document_id, field_name, value_text, numeric_value, unit,
                data_kind, pdf_page, excel_sheet, excel_cell, excel_formula,
                source_label, source_note
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [(document_id, *values) for values in prepared],
        )
    return {"assessment_id": assessment_id, "created": True}


def review_iso_assessment(case_ref, assessment_id, decision, note=""):
    """Approve or reject only the latest pending workbook for its case."""
    if decision not in {"approved", "rejected"}:
        raise ValueError("ISO review decision must be approved or rejected.")
    note = str(note or "").strip()
    if decision == "rejected" and not note:
        raise ValueError("Give a reason when rejecting an ISO workbook.")

    with get_connection() as conn, conn.cursor() as cur:
        # Lock the case so an upload cannot change the latest assessment during review.
        cur.execute(
            "SELECT case_id FROM modernisation_cases WHERE case_ref = %s FOR UPDATE",
            (case_ref,),
        )
        case = cur.fetchone()
        if case is None:
            raise ValueError("Modernisation case not found.")

        cur.execute(
            """
            SELECT a.assessment_id, a.review_status
            FROM iso_assessments a
            WHERE a.case_id = %s
              AND EXISTS (
                  SELECT 1 FROM iso_documents d
                  WHERE d.assessment_id = a.assessment_id
                    AND d.document_type = 'iso_excel'
              )
            ORDER BY a.imported_at DESC, a.assessment_id DESC
            LIMIT 1
            """,
            (case[0],),
        )
        latest = cur.fetchone()
        if latest is None or latest[0] != assessment_id:
            raise ValueError("Only the latest ISO Excel assessment can be reviewed.")
        if latest[1] != "pending":
            raise ValueError("Only pending ISO Excel assessments can be reviewed.")

        cur.execute(
            """
            UPDATE iso_assessments
            SET review_status = %s, review_note = %s, reviewed_at = NOW()
            WHERE assessment_id = %s AND case_id = %s AND review_status = 'pending'
            RETURNING assessment_id
            """,
            (decision, note, assessment_id, case[0]),
        )
        if cur.fetchone() is None:
            raise ValueError("ISO assessment was modified before review completed.")
    return decision
