
from decimal import Decimal, InvalidOperation

from psycopg.types.json import Jsonb

from database import get_connection


def get_cases():
    # Retrieve registered modernisation cases.
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT case_ref
            FROM modernisation_cases
            ORDER BY case_ref
        """)
        return [row[0] for row in cur.fetchall()]


def get_case_elevator(case_ref):
    # Retrieve the elevator linked to the selected case.
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT m.case_id, e.elevator_ref
            FROM modernisation_cases m
            JOIN elevators e ON e.elevator_id = m.elevator_id
            WHERE m.case_ref = %s
        """, (case_ref,))
        row = cur.fetchone()

    if row is None:
        raise ValueError(
            "Modernisation case or linked elevator not found."
        )

    return row


def get_latest_iso_assessment(case_ref):
    # Return the latest imported document pair for a case.
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT a.assessment_id, a.lift_id, a.imported_at
            FROM iso_assessments a
            JOIN modernisation_cases m ON m.case_id = a.case_id
            WHERE m.case_ref = %s
            ORDER BY a.imported_at DESC, a.assessment_id DESC
            LIMIT 1
        """, (case_ref,))
        return cur.fetchone()


def _number(value):
    # Convert numeric values without losing precision.
    if value is None or isinstance(value, bool):
        return None

    try:
        number = Decimal(str(value).replace(",", "").strip())
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _prepare_values(records):
    # Prepare every extracted field without overwriting duplicates.
    prepared = []
    seen = set()

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
        raise ValueError("No extracted fields were provided.")

    return prepared


def save_iso_assessment(case_id, lift_id, files, records, comparisons):
    # Check both document types and prepare records before saving.
    if len(files) != 2 or len(records) != 2:
        raise ValueError("Both EE PDF and ISO Excel are required.")

    if [file["type"] for file in files] != ["ee_pdf", "iso_excel"]:
        raise ValueError("Invalid ISO document types or order.")

    prepared = [_prepare_values(values) for values in records]
    pdf, excel = files

    with get_connection() as conn, conn.cursor() as cur:
        # Lock the case to prevent simultaneous conflicting imports.
        cur.execute("""
            SELECT case_id
            FROM modernisation_cases
            WHERE case_id = %s
            FOR UPDATE
        """, (case_id,))

        if cur.fetchone() is None:
            raise ValueError("Modernisation case not found.")

        # Identify the latest assessment for version tracking.
        cur.execute("""
            SELECT assessment_id
            FROM iso_assessments
            WHERE case_id = %s
            ORDER BY imported_at DESC, assessment_id DESC
            LIMIT 1
        """, (case_id,))

        latest = cur.fetchone()
        previous_id = latest[0] if latest else None

        # Detect an identical document pair.
        cur.execute("""
            SELECT assessment_id
            FROM iso_assessments
            WHERE case_id = %s
              AND pdf_sha256 = %s
              AND excel_sha256 = %s
        """, (
            case_id,
            pdf["sha256"],
            excel["sha256"],
        ))

        existing = cur.fetchone()

        if existing:
            return {
                "assessment_id": existing[0],
                "created": False,
                "is_latest": existing[0] == previous_id,
            }

        # Create a new assessment without deleting older versions.
        cur.execute("""
            INSERT INTO iso_assessments (
                case_id,
                lift_id,
                pdf_sha256,
                excel_sha256,
                comparisons
            )
            VALUES (%s, %s, %s, %s, %s)
            RETURNING assessment_id
        """, (
            case_id,
            lift_id,
            pdf["sha256"],
            excel["sha256"],
            Jsonb(comparisons),
        ))

        assessment_id = cur.fetchone()[0]

        # Store both original files and all their extracted values.
        for file, values in zip(files, prepared):
            cur.execute("""
                INSERT INTO iso_documents (
                    assessment_id,
                    document_type,
                    filename,
                    file_sha256,
                    content
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING document_id
            """, (
                assessment_id,
                file["type"],
                file["filename"],
                file["sha256"],
                file["bytes"],
            ))

            document_id = cur.fetchone()[0]

            cur.executemany("""
                INSERT INTO iso_extracted_values (
                    document_id,
                    field_name,
                    value_text,
                    numeric_value,
                    unit,
                    data_kind,
                    pdf_page,
                    excel_sheet,
                    excel_cell,
                    excel_formula,
                    source_label,
                    source_note
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s
                )
            """, [
                (document_id, *value)
                for value in values
            ])

    # Report the new assessment and the version it replaces.
    return {
        "assessment_id": assessment_id,
        "created": True,
        "is_latest": True,
        "previous_assessment_id": previous_id,
        "pdf_fields_saved": len(prepared[0]),
        "excel_fields_saved": len(prepared[1]),
    }
