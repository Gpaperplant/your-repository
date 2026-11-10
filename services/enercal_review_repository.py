
"""Review and revise the 16 comparison fields from 12 page EnerCal MOD PDFs."""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from database import get_connection


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
    "existing_operational_co2e": "Existing operational CO2e (kg)",
    "proposed_operational_co2e": "Proposed operational CO2e (kg)",
    "existing_embodied_co2e": "Existing embodied CO2e (kg)",
    "proposed_embodied_co2e": "Proposed embodied CO2e (kg)",
    "reported_energy_saving_percent": "EnerCal reported saving (%)",
    "calculated_energy_saving_percent": "Calculated saving (%)",
}

EXPECTED_FIELDS = set(FIELD_LABELS)

CALCULATED_FIELD = "calculated_energy_saving_percent"

TECHNOLOGY_FIELDS = {
    "existing_elevator_technology",
    "proposed_elevator_technology",
}

OPTIONAL_NUMERIC_FIELDS = {
    "existing_annual_regenerative_energy",
    "proposed_annual_regenerative_energy",
    "existing_embodied_co2e",
}

REVIEW_DECISIONS = {
    "approved",
    "changes_requested",
    "further_verification_required",
    "rejected",
}

EDITABLE_STATUSES = {
    "pending",
    "changes_requested",
    "further_verification_required",
}


def _rows(cur):
    columns = [column.name for column in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _lock(cur, document_id):
    cur.execute(
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (document_id,),
    )


def _latest(cur, document_id, for_update=False):
    query = """
        SELECT revision_id, revision_no, review_status,
               review_note, reviewed_at
        FROM extraction_revisions
        WHERE document_id = %s
        ORDER BY revision_no DESC
        LIMIT 1
    """

    cur.execute(
        query + (" FOR UPDATE" if for_update else ""),
        (document_id,),
    )

    result = _rows(cur)

    if not result:
        raise ValueError("This document has no extraction revision")

    return result[0]


def _candidates(cur, revision_id):
    cur.execute(
        """
        SELECT candidate_id, page_id, field_name, raw_value,
               numeric_value, unit, revision_no
        FROM extraction_candidates
        WHERE extraction_revision_id = %s
        ORDER BY field_name
        """,
        (revision_id,),
    )

    return _rows(cur)


def _validate(candidates, revision_no):
    names = [row["field_name"] for row in candidates]

    if len(names) != 16 or set(names) != EXPECTED_FIELDS:
        raise ValueError(
            "The revision must contain exactly 16 unique comparison fields"
        )

    if len({row["page_id"] for row in candidates}) != 1:
        raise ValueError(
            "Comparison fields must reference one source PDF page"
        )

    if any(row["revision_no"] != revision_no for row in candidates):
        raise ValueError(
            "Candidate and revision numbers do not match"
        )


def _event(cur, revision_id, action, old_status, new_status, reason):
    cur.execute(
        """
        INSERT INTO enercal_review_events
            (revision_id, event_type, previous_status, new_status, reason)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (revision_id, action, old_status, new_status, reason),
    )


def get_review_documents():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT
                d.document_id,
                m.case_ref,
                d.filename
            FROM source_documents d
            JOIN modernisation_cases m
                ON m.case_id = d.case_id
            JOIN extraction_revisions r
                ON r.document_id = d.document_id
            ORDER BY d.document_id DESC
            """
        )

        return cur.fetchall()


def get_review_details(document_id):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT filename, pdf_bytes
            FROM source_documents
            WHERE document_id = %s
            """,
            (document_id,),
        )

        document = cur.fetchone()

        if document is None:
            raise ValueError("Document not found")

        revision = _latest(cur, document_id)
        candidates = _candidates(cur, revision["revision_id"])

        cur.execute(
            """
            SELECT *
            FROM enercal_comparison_view
            WHERE document_id = %s
              AND revision_no = %s
            ORDER BY
                CASE technology_role
                    WHEN 'existing' THEN 1
                    ELSE 2
                END
            """,
            (document_id, revision["revision_no"]),
        )

        comparison = _rows(cur)

        cur.execute(
            """
            SELECT
                r.revision_id,
                r.revision_no,
                r.review_status,
                r.review_note,
                r.created_at,
                r.reviewed_at,
                COUNT(c.candidate_id) AS field_count
            FROM extraction_revisions r
            LEFT JOIN extraction_candidates c
                ON c.extraction_revision_id = r.revision_id
            WHERE r.document_id = %s
            GROUP BY r.revision_id
            ORDER BY r.revision_no
            """,
            (document_id,),
        )

        history = _rows(cur)

        cur.execute(
            """
            SELECT
                e.event_id,
                r.revision_no,
                e.event_type,
                e.previous_status,
                e.new_status,
                e.reason,
                e.created_at
            FROM enercal_review_events e
            JOIN extraction_revisions r
                ON r.revision_id = e.revision_id
            WHERE r.document_id = %s
            ORDER BY e.event_id
            """,
            (document_id,),
        )

        events = _rows(cur)

    return {
        "filename": document[0],
        "pdf_bytes": bytes(document[1]),
        "revision_id": revision["revision_id"],
        "revision_no": revision["revision_no"],
        "status": revision["review_status"],
        "candidates": candidates,
        "comparison": comparison,
        "history": history,
        "events": events,
    }


def get_specifications(document_id):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT field_name, field_value, unit, source_page
            FROM enercal_specifications
            WHERE document_id = %s
            ORDER BY source_page, field_name
            """,
            (document_id,),
        )

        return [
            {
                "Field": name.replace("_", " ").capitalize(),
                "Value": value,
                "Unit": unit or "",
                "Source page": page,
            }
            for name, value, unit, page in cur.fetchall()
        ]


def review_revision(document_id, decision, review_note=""):
    if decision not in REVIEW_DECISIONS:
        raise ValueError("Unknown review decision")

    review_note = review_note.strip()

    if decision != "approved" and not review_note:
        raise ValueError("A reason is required for this decision")

    with get_connection() as conn, conn.cursor() as cur:
        _lock(cur, document_id)

        revision = _latest(cur, document_id, for_update=True)

        _validate(
            _candidates(cur, revision["revision_id"]),
            revision["revision_no"],
        )

        if revision["review_status"] != "pending":
            raise ValueError(
                "Only pending revisions can receive a review decision"
            )

        cur.execute(
            """
            UPDATE extraction_revisions
            SET review_status = %s,
                review_note = %s,
                reviewed_at = NOW()
            WHERE revision_id = %s
              AND review_status = 'pending'
            """,
            (
                decision,
                review_note or None,
                revision["revision_id"],
            ),
        )

        if cur.rowcount != 1:
            raise RuntimeError(
                "The review status changed during approval"
            )

        _event(
            cur,
            revision["revision_id"],
            decision,
            "pending",
            decision,
            review_note or None,
        )

    return revision["revision_no"]


def submit_verification(document_id, explanation):
    explanation = explanation.strip()

    if not explanation:
        raise ValueError("A clarification reason is required")

    with get_connection() as conn, conn.cursor() as cur:
        _lock(cur, document_id)

        revision = _latest(cur, document_id, for_update=True)

        _validate(
            _candidates(cur, revision["revision_id"]),
            revision["revision_no"],
        )

        old_status = revision["review_status"]

        if old_status not in {
            "changes_requested",
            "further_verification_required",
        }:
            raise ValueError(
                "Only revisions awaiting clarification can be reopened"
            )

        cur.execute(
            """
            UPDATE extraction_revisions
            SET review_status = 'pending',
                review_note = %s,
                reviewed_at = NULL
            WHERE revision_id = %s
            """,
            (explanation, revision["revision_id"]),
        )

        _event(
            cur,
            revision["revision_id"],
            "clarification_submitted",
            old_status,
            "pending",
            explanation,
        )

    return revision["revision_no"]


def _parse_value(field_name, supplied):
    raw = str(supplied).strip()

    if not raw:
        raise ValueError(
            f"{FIELD_LABELS[field_name]} cannot be blank"
        )

    if field_name in TECHNOLOGY_FIELDS:
        return raw, None

    if raw.upper() in {"N/A", "NA"}:
        if field_name in OPTIONAL_NUMERIC_FIELDS:
            return "N/A", None

        raise ValueError(
            f"{FIELD_LABELS[field_name]} requires a number"
        )

    try:
        number = Decimal(
            raw.replace(",", "").replace("%", "")
        )
    except InvalidOperation:
        raise ValueError(
            f"Invalid number for {FIELD_LABELS[field_name]}"
        ) from None

    if not number.is_finite():
        raise ValueError(
            f"Invalid value for {FIELD_LABELS[field_name]}"
        )

    if (
        number < 0
        and field_name != "reported_energy_saving_percent"
    ):
        raise ValueError(
            f"{FIELD_LABELS[field_name]} cannot be negative"
        )

    if (
        number.as_tuple().exponent < -3
        and number != number.quantize(Decimal("0.001"))
    ):
        raise ValueError(
            f"{FIELD_LABELS[field_name]} supports at most 3 decimals"
        )

    return raw, number


def resubmit_revision(document_id, corrected_values, resubmission_note):
    resubmission_note = resubmission_note.strip()

    if not resubmission_note:
        raise ValueError(
            "Explain the correction before saving"
        )

    if set(corrected_values) != EXPECTED_FIELDS:
        raise ValueError(
            "Submit all 16 comparison fields"
        )

    with get_connection() as conn, conn.cursor() as cur:
        _lock(cur, document_id)

        previous = _latest(
            cur,
            document_id,
            for_update=True,
        )

        source = _candidates(
            cur,
            previous["revision_id"],
        )

        _validate(source, previous["revision_no"])

        old_status = previous["review_status"]

        if old_status not in EDITABLE_STATUSES:
            raise ValueError(
                "Only reports awaiting review or correction can be edited"
            )

        originals = {
            row["field_name"]: row
            for row in source
        }

        # Parse the editable source values once.
        values = {
            key: _parse_value(key, corrected_values[key])
            for key in FIELD_LABELS
            if key != CALCULATED_FIELD
        }

        before = values[
            "existing_annual_energy_consumption"
        ][1]

        after = values[
            "proposed_annual_energy_consumption"
        ][1]

        if before <= 0:
            raise ValueError(
                "Existing annual energy must be greater than zero"
            )

        # Recalculate the saving from corrected energy values.
        saving = (
            (before - after) / before * 100
        ).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )

        values[CALCULATED_FIELD] = (
            str(saving),
            saving,
        )

        # Compare numeric values rather than number formatting.
        changed = [
            key
            for key in FIELD_LABELS
            if key != CALCULATED_FIELD
            and (
                values[key][0] != originals[key]["raw_value"]
                if key in TECHNOLOGY_FIELDS
                else values[key][1] != originals[key]["numeric_value"]
            )
        ]

        if not changed:
            raise ValueError(
                "No source values changed"
            )

        new_number = previous["revision_no"] + 1

        cur.execute(
            """
            INSERT INTO extraction_revisions
                (document_id, revision_no, review_status)
            VALUES (%s, %s, 'pending')
            RETURNING revision_id
            """,
            (document_id, new_number),
        )

        new_id = cur.fetchone()[0]

        # Store the full corrected comparison as a new revision.
        cur.executemany(
            """
            INSERT INTO extraction_candidates
                (page_id, field_name, raw_value, numeric_value,
                 unit, revision_no, extraction_revision_id,
                 review_status)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, 'pending')
            """,
            [
                (
                    originals[key]["page_id"],
                    key,
                    values[key][0],
                    values[key][1],
                    originals[key]["unit"],
                    new_number,
                    new_id,
                )
                for key in FIELD_LABELS
            ],
        )

        # Preserve the correction history for both revisions.
        _event(
            cur,
            previous["revision_id"],
            "corrected_revision_created",
            old_status,
            old_status,
            f"Revision {new_number} created {resubmission_note}",
        )

        _event(
            cur,
            new_id,
            "revision_submitted",
            None,
            "pending",
            resubmission_note,
        )

    return new_number
