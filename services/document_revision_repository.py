from database import get_connection


REVIEW_SCOPES = frozenset({"full_values", "comparison_only"})
REVIEW_DECISIONS = frozenset({
    "approved", "rejected", "changes_requested", "further_verification_required"
})


def _positive_id(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer.")


def insert_revision(cursor, document_id, review_scope="full_values"):
    _positive_id(document_id, "Document ID")
    if review_scope not in REVIEW_SCOPES:
        raise ValueError("Invalid review scope.")

    cursor.execute(
        """
        SELECT d.case_id
        FROM project_documents d
        JOIN modernisation_cases m ON m.case_id = d.case_id
        WHERE d.document_id = %s
        FOR UPDATE OF m
        """,
        (document_id,),
    )
    if cursor.fetchone() is None:
        raise ValueError("Document not found.")

    cursor.execute(
        """
        SELECT revision_no, review_status
        FROM document_revisions
        WHERE document_id = %s
        ORDER BY revision_no DESC
        LIMIT 1
        """,
        (document_id,),
    )
    latest = cursor.fetchone()
    if latest and latest[1] == "pending":
        raise ValueError("Review the pending revision before creating another.")

    number = latest[0] + 1 if latest else 1
    cursor.execute(
        """
        INSERT INTO document_revisions (document_id, revision_no, review_scope)
        VALUES (%s, %s, %s)
        RETURNING revision_id
        """,
        (document_id, number, review_scope),
    )
    return {
        "revision_id": cursor.fetchone()[0],
        "document_id": document_id,
        "revision_no": number,
        "review_status": "pending",
    }


def create_revision(document_id, review_scope="full_values"):
    with get_connection() as connection, connection.cursor() as cursor:
        return insert_revision(cursor, document_id, review_scope)


def review_revision(revision_id, decision, reason=None, reviewer_id=None):
    _positive_id(revision_id, "Revision ID")
    if decision not in REVIEW_DECISIONS:
        raise ValueError("Invalid review decision.")

    reason = str(reason or "").strip() or None
    reviewer_id = str(reviewer_id or "").strip() or None
    if decision != "approved" and reason is None:
        raise ValueError("A reason is required for this decision.")

    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT review_document_revision(%s, %s, %s, %s, FALSE)",
            (revision_id, decision, reason, reviewer_id),
        )
        event_id = cursor.fetchone()[0]
        if event_id is None:
            raise RuntimeError("Review event was not created.")

    return {"revision_id": revision_id, "review_id": event_id, "review_status": decision}


def get_latest_revision(document_id):
    _positive_id(document_id, "Document ID")
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT revision_id, document_id, revision_no, review_scope,
                   review_status, review_note, reviewed_by, created_at, reviewed_at
            FROM document_revisions
            WHERE document_id = %s
            ORDER BY revision_no DESC
            LIMIT 1
            """,
            (document_id,),
        )
        row = cursor.fetchone()

    if row is None:
        return None
    keys = (
        "revision_id", "document_id", "revision_no", "review_scope",
        "review_status", "review_note", "reviewed_by", "created_at", "reviewed_at",
    )
    return dict(zip(keys, row))


def get_review_history(revision_id):
    _positive_id(revision_id, "Revision ID")
    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT review_id, revision_id, action, previous_status, new_status,
                   reason, reviewer_id, reviewer_authenticated, created_at
            FROM document_reviews
            WHERE revision_id = %s
            ORDER BY review_id
            """,
            (revision_id,),
        )
        rows = cursor.fetchall()

    keys = (
        "review_id", "revision_id", "action", "previous_status", "new_status",
        "reason", "reviewer_id", "reviewer_authenticated", "created_at",
    )
    return [dict(zip(keys, row)) for row in rows]
