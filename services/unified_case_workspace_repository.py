"""Read-only access to unified modernisation evidence and review history."""

from database import get_connection
from services.mod_value_mapper import COMPARISON_FIELDS


MIME_TYPES = {
    "mod_pdf": "application/pdf",
    "iso_excel": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "iso_test_pdf": "application/pdf",
    "meter_csv": "text/csv",
}

VALUE_COLUMNS = (
    "value_id", "revision_id", "field_name", "phase", "value_kind",
    "raw_source_value", "numeric_value", "text_value", "unit", "source_page",
    "excel_sheet", "excel_cell", "excel_formula", "source_label", "source_note",
    "measured_on", "period_start", "period_end", "source_verified",
    "is_corrected", "correction_reason", "created_at",
)

REVIEW_COLUMNS = (
    "review_id", "revision_id", "action", "previous_status", "new_status",
    "reason", "reviewer_id", "reviewer_authenticated", "created_at",
)


def _positive_id(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")


def _case_ref(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Case reference is required.")
    return value.strip()


def _rows(cursor):
    columns = [item.name for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _one(cursor):
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(zip((item.name for item in cursor.description), row))


def list_workspace_cases():
    """List cases with at least one document in the unified structure."""
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT m.case_id, m.case_ref, m.status AS case_status,
                   e.elevator_ref, COUNT(d.document_id) AS document_count
            FROM modernisation_cases m
            JOIN elevators e ON e.elevator_id = m.elevator_id
            JOIN project_documents d ON d.case_id = m.case_id
            GROUP BY m.case_id, m.case_ref, m.status, e.elevator_ref
            ORDER BY m.case_ref
            """
        )
        return _rows(cursor)


def list_case_documents(case_ref):
    """List unified evidence without loading its original binary files."""
    reference = _case_ref(case_ref)
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT d.document_id, d.case_id, m.case_ref, e.elevator_ref,
                   d.document_type, d.source_classification,
                   d.reported_elevator_ref, d.filename, d.file_sha256,
                   d.uploaded_at, r.revision_id AS latest_revision_id,
                   r.revision_no AS latest_revision_no,
                   r.review_scope AS latest_review_scope,
                   r.review_status AS latest_review_status
            FROM project_documents d
            JOIN modernisation_cases m ON m.case_id = d.case_id
            JOIN elevators e ON e.elevator_id = m.elevator_id
            LEFT JOIN LATERAL (
                SELECT revision_id, revision_no, review_scope, review_status
                FROM document_revisions
                WHERE document_id = d.document_id
                ORDER BY revision_no DESC LIMIT 1
            ) r ON TRUE
            WHERE m.case_ref = %s
            ORDER BY d.document_id DESC
            """,
            (reference,),
        )
        return _rows(cursor)


def get_document_file(document_id):
    """Retrieve original bytes for an authorised source-file download."""
    _positive_id(document_id, "Document ID")
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT d.document_id, m.case_ref, d.document_type, d.filename,
                   d.file_sha256, d.file_content
            FROM project_documents d
            JOIN modernisation_cases m ON m.case_id = d.case_id
            WHERE d.document_id = %s
            """,
            (document_id,),
        )
        result = _one(cursor)
    if result is None:
        raise ValueError("Document not found.")
    result["file_content"] = bytes(result["file_content"])
    result["mime_type"] = MIME_TYPES[result["document_type"]]
    return result


def list_document_revisions(document_id):
    """Read each revision's status and counts without changing history."""
    _positive_id(document_id, "Document ID")
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT r.revision_id, r.document_id, r.revision_no,
                   r.review_scope, r.review_status, r.review_note,
                   r.reviewed_by, r.reviewer_authenticated,
                   r.created_at, r.reviewed_at,
                   (SELECT COUNT(*) FROM engineering_values v
                    WHERE v.revision_id = r.revision_id) AS value_count,
                   (SELECT COUNT(*) FROM engineering_values v
                    WHERE v.revision_id = r.revision_id
                      AND v.source_verified) AS verified_count,
                   (SELECT COUNT(*) FROM engineering_values v
                    WHERE v.revision_id = r.revision_id
                      AND v.is_corrected) AS corrected_count,
                   (SELECT COUNT(*) FROM document_reviews h
                    WHERE h.revision_id = r.revision_id) AS review_event_count
            FROM document_revisions r
            WHERE r.document_id = %s
            ORDER BY r.revision_no DESC
            """,
            (document_id,),
        )
        return _rows(cursor)


def get_revision_snapshot(revision_id):
    """Read one revision, its engineering values, and its audit events."""
    _positive_id(revision_id, "Revision ID")
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT r.revision_id, r.revision_no, r.review_scope,
                   r.review_status, r.review_note, r.reviewed_by,
                   r.reviewer_authenticated, r.created_at, r.reviewed_at,
                   d.document_id, d.document_type, d.filename,
                   d.file_sha256, d.source_classification,
                   d.reported_elevator_ref, d.uploaded_at,
                   m.case_id, m.case_ref, m.status AS case_status,
                   e.elevator_ref
            FROM document_revisions r
            JOIN project_documents d ON d.document_id = r.document_id
            JOIN modernisation_cases m ON m.case_id = d.case_id
            JOIN elevators e ON e.elevator_id = m.elevator_id
            WHERE r.revision_id = %s
            """,
            (revision_id,),
        )
        revision = _one(cursor)
        if revision is None:
            return None

        cursor.execute(
            f"SELECT {', '.join(VALUE_COLUMNS)} FROM engineering_values "
            "WHERE revision_id = %s ORDER BY phase, field_name",
            (revision_id,),
        )
        values = _rows(cursor)

        cursor.execute(
            f"SELECT {', '.join(REVIEW_COLUMNS)} FROM document_reviews "
            "WHERE revision_id = %s ORDER BY review_id",
            (revision_id,),
        )
        reviews = _rows(cursor)

    return {"revision": revision, "values": values, "reviews": reviews}


def mod_comparison_from_snapshot(snapshot):
    """Adapt the sixteen MOD comparison fields for a future review screen."""
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("revision"), dict):
        raise ValueError("A revision snapshot is required.")
    if snapshot["revision"].get("document_type") != "mod_pdf":
        raise ValueError("A MOD document revision is required.")

    index = {}
    for value in snapshot.get("values", ()):
        key = (value["phase"], value["field_name"])
        if key in index:
            raise ValueError("Duplicate MOD engineering field in revision.")
        index[key] = value

    needed = (
        [(phase, "elevator_technology") for phase in ("existing", "proposed")]
        + [(phase, field) for phase in ("existing", "proposed") for field in COMPARISON_FIELDS]
        + [("common", name) for name in (
            "reported_energy_saving_percent", "calculated_energy_saving_percent"
        )]
    )
    result = {}
    for phase, field in needed:
        value = index.get((phase, field))
        if value is None:
            raise ValueError(f"Missing MOD comparison field: {phase}.{field}")
        name = field if phase == "common" else f"{phase}_{field}"
        result[name] = {
            "raw_value": value["raw_source_value"],
            "numeric_value": value["numeric_value"],
            "text_value": value["text_value"],
            "unit": value["unit"],
            "source_page": value["source_page"],
            "source_verified": value["source_verified"],
            "is_corrected": value["is_corrected"],
        }
    return result
