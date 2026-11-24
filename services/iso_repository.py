import hashlib
import re
from decimal import Decimal, InvalidOperation

from database import get_connection
from services.document_revision_repository import insert_revision
from services.engineering_value_repository import VALUE_COLUMNS, insert_engineering_values
from services.iso_value_mapper import map_iso_values
from services.project_document_repository import insert_project_document


def normalise_lift_id(value):
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def get_cases():
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT case_ref FROM modernisation_cases ORDER BY case_ref")
        return [row[0] for row in cursor.fetchall()]


def get_case_elevator(case_ref):
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.case_id, e.elevator_ref
            FROM modernisation_cases m
            JOIN elevators e ON e.elevator_id = m.elevator_id
            WHERE m.case_ref = %s
            """,
            (case_ref,),
        )
        row = cursor.fetchone()
    if row is None:
        raise ValueError("Modernisation case or linked elevator not found.")
    return row


def get_latest_iso_assessment(case_ref):
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
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
        return cursor.fetchone()


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", "").strip())
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _prepare_values(records):
    prepared = []
    seen = set()
    for record in records:
        field = record["field"]
        if not isinstance(field, str) or not field.strip() or field in seen:
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


def save_iso_excel_assessment(case_id, lift_id, filename, file_bytes,
                              records, checksum):
    """Store one ISO workbook in the five-table schema, atomically.

    The function name is retained for the ISO importer. It returns *unified*
    document and revision IDs, not a legacy ISO assessment ID. Duplicate
    uploads return the already-stored IDs without adding another revision.
    """
    if isinstance(case_id, bool) or not isinstance(case_id, int) or case_id < 1:
        raise ValueError("Invalid modernisation case ID.")
    if not normalise_lift_id(lift_id):
        raise ValueError("ISO Excel is missing a valid lift reference.")
    if not isinstance(file_bytes, bytes) or not file_bytes:
        raise ValueError("Invalid ISO Excel content.")
    if not isinstance(checksum, str) or checksum != hashlib.sha256(file_bytes).hexdigest():
        raise ValueError("Invalid ISO workbook checksum.")

    # Retain legacy extractor field-name collision checks, but no legacy writes.
    _prepare_values(records)
    lift_refs = [record.get("value") for record in records
                 if record.get("field") == "lift_id"]
    if len(lift_refs) != 1 or normalise_lift_id(lift_refs[0]) != normalise_lift_id(lift_id):
        raise ValueError("ISO extracted lift reference does not match the uploaded lift ID.")
    values = map_iso_values(records)

    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.case_ref, e.elevator_ref
            FROM modernisation_cases m
            JOIN elevators e ON e.elevator_id = m.elevator_id
            WHERE m.case_id = %s
            FOR UPDATE OF m
            """,
            (case_id,),
        )
        case = cursor.fetchone()
        if case is None:
            raise ValueError("Modernisation case not found.")
        case_ref, elevator_ref = case

        # Prevent one case from mixing two distinct ISO workbook lift IDs.
        cursor.execute(
            """
            SELECT reported_elevator_ref
            FROM project_documents
            WHERE case_id = %s AND document_type = 'iso_excel'
            ORDER BY document_id DESC
            LIMIT 1
            """,
            (case_id,),
        )
        previous = cursor.fetchone()
        if (previous and normalise_lift_id(previous[0])
                != normalise_lift_id(lift_id)):
            raise ValueError(
                f"This case already contains ISO data for {previous[0]}."
            )

        source_classification = (
            "test_fixture" if elevator_ref.upper().startswith("TEST-LIFT-")
            else "unknown"
        )
        document = insert_project_document(
            cursor,
            case_ref=case_ref,
            document_type="iso_excel",
            filename=filename,
            file_bytes=file_bytes,
            reported_elevator_ref=lift_id,
            source_classification=source_classification,
        )

        if not document["created"]:
            cursor.execute(
                """
                SELECT r.revision_id, r.revision_no,
                       (SELECT COUNT(*) FROM engineering_values v
                        WHERE v.revision_id = r.revision_id) AS value_count
                FROM document_revisions r
                WHERE r.document_id = %s
                ORDER BY r.revision_no DESC
                LIMIT 1
                """,
                (document["document_id"],),
            )
            latest = cursor.fetchone()
            if latest is None:
                raise RuntimeError("Stored ISO document has no revision.")
            return {
                "document_id": document["document_id"],
                "revision_id": latest[0],
                "revision_no": latest[1],
                "values_saved": latest[2],
                "created": False,
            }

        revision = insert_revision(
            cursor, document["document_id"], "comparison_only"
        )
        inserted = insert_engineering_values(
            cursor, revision["revision_id"], values
        )
        if inserted["values_saved"] != len(values):
            raise RuntimeError("Incomplete ISO engineering-value snapshot.")

    return {
        "document_id": document["document_id"],
        "revision_id": revision["revision_id"],
        "revision_no": revision["revision_no"],
        "values_saved": len(values),
        "created": True,
    }


def _unified_iso_revision(cursor, case_id, assessment_id):
    cursor.execute(
        """
        SELECT d.document_id
        FROM iso_assessments a
        JOIN project_documents d
          ON d.case_id = a.case_id
         AND d.document_type = 'iso_excel'
         AND d.file_sha256 = a.excel_sha256
        WHERE a.case_id = %s AND a.assessment_id = %s
        """,
        (case_id, assessment_id),
    )
    document = cursor.fetchone()
    if document is None:
        return None

    cursor.execute(
        """
        SELECT revision_id, review_status, review_scope
        FROM document_revisions
        WHERE document_id = %s
        ORDER BY revision_no DESC
        LIMIT 1
        FOR UPDATE
        """,
        (document[0],),
    )
    revision = cursor.fetchone()
    if revision is None:
        raise ValueError("The unified ISO document has no revision.")

    return {
        "document_id": document[0],
        "revision_id": revision[0],
        "review_status": revision[1],
        "review_scope": revision[2],
    }


def _review_unified_iso(cursor, revision_id, decision, note):
    cursor.execute(
        "SELECT review_document_revision(%s, %s, %s, %s, FALSE)",
        (revision_id, decision, note, None),
    )
    event = cursor.fetchone()
    if event is None or event[0] is None:
        raise RuntimeError("Unified ISO review event was not created.")


def _create_comparison_revision(cursor, current):
    cursor.execute(
        f"SELECT {', '.join(VALUE_COLUMNS)} FROM engineering_values "
        "WHERE revision_id = %s ORDER BY value_id",
        (current["revision_id"],),
    )
    values = [dict(zip(VALUE_COLUMNS, row)) for row in cursor.fetchall()]
    if not values:
        raise ValueError("The unified ISO revision has no engineering values.")

    revision = insert_revision(cursor, current["document_id"], "comparison_only")
    insert_engineering_values(cursor, revision["revision_id"], values)
    return revision["revision_id"]


def _sync_iso_review(cursor, case_id, assessment_id, decision, note):
    current = _unified_iso_revision(cursor, case_id, assessment_id)
    if current is None:
        return
    if current["review_status"] != "pending":
        raise ValueError("The unified ISO revision is not pending.")

    revision_id = current["revision_id"]
    if decision == "approved" and current["review_scope"] == "full_values":
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM engineering_values
                WHERE revision_id = %s AND NOT source_verified
            )
            """,
            (revision_id,),
        )
        result = cursor.fetchone()
        if result is None:
            raise RuntimeError("Cannot check ISO source verification.")
        if result[0]:
            _review_unified_iso(
                cursor,
                revision_id,
                "further_verification_required",
                "ISO summary reviewed; field-level verification remains outstanding.",
            )
            revision_id = _create_comparison_revision(cursor, current)

    _review_unified_iso(cursor, revision_id, decision, note or None)


def review_iso_assessment(case_ref, assessment_id, decision, note=""):
    if decision not in {"approved", "rejected"}:
        raise ValueError("ISO review decision must be approved or rejected.")
    note = str(note or "").strip()
    if decision == "rejected" and not note:
        raise ValueError("Give a reason when rejecting an ISO workbook.")

    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT case_id FROM modernisation_cases WHERE case_ref = %s FOR UPDATE",
            (case_ref,),
        )
        case = cursor.fetchone()
        if case is None:
            raise ValueError("Modernisation case not found.")

        cursor.execute(
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
        latest = cursor.fetchone()
        if latest is None or latest[0] != assessment_id:
            raise ValueError("Only the latest ISO Excel assessment can be reviewed.")
        if latest[1] != "pending":
            raise ValueError("Only pending ISO Excel assessments can be reviewed.")

        _sync_iso_review(cursor, case[0], assessment_id, decision, note)
        cursor.execute(
            """
            UPDATE iso_assessments
            SET review_status = %s, review_note = %s, reviewed_at = NOW()
            WHERE assessment_id = %s AND case_id = %s AND review_status = 'pending'
            RETURNING assessment_id
            """,
            (decision, note, assessment_id, case[0]),
        )
        if cursor.fetchone() is None:
            raise ValueError("ISO assessment was modified before review completed.")
    return decision
