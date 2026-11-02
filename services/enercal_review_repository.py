
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from database import get_connection


# =====================================================
# 1. EXPECTED ENERCAL FIELDS
# =====================================================

FIELD_LABELS = {
    "existing_elevator_technology":
        "Existing elevator technology",

    "proposed_elevator_technology":
        "Proposed elevator technology",

    "existing_annual_energy_consumption":
        "Existing annual energy (kWh/year)",

    "proposed_annual_energy_consumption":
        "Proposed annual energy (kWh/year)",

    "existing_annual_regenerative_energy":
        "Existing regenerative energy (kWh/year)",

    "proposed_annual_regenerative_energy":
        "Proposed regenerative energy (kWh/year)",

    "existing_annual_electricity_expenditure":
        "Existing annual expenditure (BHD/year)",

    "proposed_annual_electricity_expenditure":
        "Proposed annual expenditure (BHD/year)",

    "existing_lifetime_electricity_expenditure":
        "Existing lifetime expenditure (BHD)",

    "proposed_lifetime_electricity_expenditure":
        "Proposed lifetime expenditure (BHD)",

    "existing_operational_co2e":
        "Existing operational CO2e (kg)",

    "proposed_operational_co2e":
        "Proposed operational CO2e (kg)",

    "existing_embodied_co2e":
        "Existing embodied CO2e (kg)",

    "proposed_embodied_co2e":
        "Proposed embodied CO2e (kg)",

    "reported_energy_saving_percent":
        "EnerCal reported saving (%)",

    "calculated_energy_saving_percent":
        "Calculated saving (%)",
}

EXPECTED_FIELDS = set(FIELD_LABELS)

TECHNOLOGY_FIELDS = {
    "existing_elevator_technology",
    "proposed_elevator_technology",
}

OPTIONAL_NUMERIC_FIELDS = {
    "existing_annual_regenerative_energy",
    "proposed_annual_regenerative_energy",
}

CALCULATED_FIELD = "calculated_energy_saving_percent"

REVIEW_DECISIONS = {
    "approved": "approved",
    "changes_requested": "changes_requested",
    "further_verification_required":
        "further_verification_required",
    "rejected": "rejected",
}


# =====================================================
# 2. DATABASE HELPER FUNCTIONS
# =====================================================

def _dict_rows(cur):
    """Convert PostgreSQL rows into dictionaries."""

    columns = [
        description.name
        for description in cur.description
    ]

    return [
        dict(zip(columns, row))
        for row in cur.fetchall()
    ]


def _lock_document(cur, document_id):
    """Prevent concurrent review updates to one PDF."""

    cur.execute(
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (document_id,)
    )


def _latest_revision(cur, document_id, for_update=False):
    """Find the latest revision for the selected PDF."""

    suffix = "FOR UPDATE" if for_update else ""

    cur.execute(
        f"""
        SELECT
            revision_id,
            document_id,
            revision_no,
            review_status,
            review_note,
            reviewed_at
        FROM extraction_revisions
        WHERE document_id = %s
        ORDER BY revision_no DESC
        LIMIT 1
        {suffix}
        """,
        (document_id,)
    )

    result = _dict_rows(cur)

    if not result:
        raise ValueError(
            "This document has no extraction revision."
        )

    return result[0]


def _revision_candidates(cur, revision_id):
    """Read values belonging to one revision only."""

    cur.execute(
        """
        SELECT
            candidate_id,
            page_id,
            field_name,
            raw_value,
            numeric_value,
            unit,
            revision_no
        FROM extraction_candidates
        WHERE extraction_revision_id = %s
        ORDER BY field_name
        """,
        (revision_id,)
    )

    return _dict_rows(cur)


def _validate_revision(candidates, revision_no):
    """Check that a revision has 16 valid field entries."""

    fields = [
        candidate["field_name"]
        for candidate in candidates
    ]

    if (
        len(fields) != 16
        or set(fields) != EXPECTED_FIELDS
    ):
        raise ValueError(
            "The latest revision must contain "
            "exactly 16 unique EnerCal fields."
        )

    if len({
        candidate["page_id"]
        for candidate in candidates
    }) != 1:
        raise ValueError(
            "The 16 comparison fields must "
            "come from one PDF page."
        )

    if any(
        candidate["revision_no"] != revision_no
        for candidate in candidates
    ):
        raise ValueError(
            "Candidate revision numbers "
            "do not match their revision."
        )


def _record_event(
    cur,
    revision_id,
    event_type,
    previous_status,
    new_status,
    reason
):
    """Save one engineering review action."""

    cur.execute(
        """
        INSERT INTO enercal_review_events (
            revision_id,
            event_type,
            previous_status,
            new_status,
            reason
        )
        VALUES (%s, %s, %s, %s, %s)
        """,
        (
            revision_id,
            event_type,
            previous_status,
            new_status,
            reason,
        )
    )


# =====================================================
# 3. LIST AVAILABLE ENERCAL REPORTS
# =====================================================

def get_review_documents():
    """List stored PDFs with extraction revisions."""

    with get_connection() as conn:
        with conn.cursor() as cur:

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


# =====================================================
# 4. LOAD REVIEW DETAILS AND HISTORY
# =====================================================

def get_review_details(document_id):
    """
    Load the PDF, current revision,
    extracted fields and review history.
    """

    with get_connection() as conn:
        with conn.cursor() as cur:

            # Original PDF
            cur.execute(
                """
                SELECT filename, pdf_bytes
                FROM source_documents
                WHERE document_id = %s
                """,
                (document_id,)
            )

            document = cur.fetchone()

            if document is None:
                raise ValueError("Document not found.")

            filename, pdf_bytes = document

            # Current revision
            revision = _latest_revision(
                cur,
                document_id
            )

            # Only fields belonging to this revision
            candidates = _revision_candidates(
                cur,
                revision["revision_id"]
            )

            # Latest comparison preview
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
                (
                    document_id,
                    revision["revision_no"]
                )
            )

            comparison = _dict_rows(cur)

            # All revisions
            cur.execute(
                """
                SELECT
                    r.revision_id,
                    r.revision_no,
                    r.review_status,
                    r.review_note,
                    r.reviewed_at,
                    r.created_at,
                    COUNT(c.candidate_id) AS field_count
                FROM extraction_revisions r
                LEFT JOIN extraction_candidates c
                    ON c.extraction_revision_id =
                       r.revision_id
                WHERE r.document_id = %s
                GROUP BY r.revision_id
                ORDER BY r.revision_no
                """,
                (document_id,)
            )

            history = _dict_rows(cur)

            # Detailed audit history
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
                (document_id,)
            )

            events = _dict_rows(cur)

    return {
        "filename": filename,
        "pdf_bytes": bytes(pdf_bytes),
        "revision_no": revision["revision_no"],
        "revision_id": revision["revision_id"],
        "status": revision["review_status"],
        "candidates": candidates,
        "comparison": comparison,
        "history": history,
        "events": events,
    }


# =====================================================
# 5. APPROVE / REQUEST CHANGES / VERIFY / REJECT
# =====================================================

def review_revision(
    document_id,
    decision,
    review_note=""
):
    """Apply a decision to the latest pending revision."""

    if decision not in REVIEW_DECISIONS:
        raise ValueError("Unknown decision.")

    review_note = review_note.strip()

    if decision != "approved" and not review_note:
        raise ValueError(
            "A reason is required for this decision."
        )

    with get_connection() as conn:
        with conn.cursor() as cur:

            _lock_document(cur, document_id)

            revision = _latest_revision(
                cur,
                document_id,
                for_update=True
            )

            candidates = _revision_candidates(
                cur,
                revision["revision_id"]
            )

            _validate_revision(
                candidates,
                revision["revision_no"]
            )

            if revision["review_status"] != "pending":
                raise ValueError(
                    "Only pending revisions can "
                    "receive a new decision."
                )

            cur.execute(
                """
                UPDATE extraction_revisions
                SET
                    review_status = %s,
                    review_note = %s,
                    reviewed_at = NOW()
                WHERE revision_id = %s
                  AND review_status = 'pending'
                """,
                (
                    decision,
                    review_note or None,
                    revision["revision_id"]
                )
            )

            if cur.rowcount != 1:
                raise RuntimeError(
                    "Review status changed concurrently."
                )

            _record_event(
                cur,
                revision["revision_id"],
                decision,
                "pending",
                decision,
                review_note or None
            )

    return revision["revision_no"]


# =====================================================
# 6. SUBMIT CLARIFICATION WITHOUT CHANGING VALUES
# =====================================================

def submit_verification(document_id, explanation):
    """
    Reopen an existing revision after clarification.

    Does not create duplicate extraction candidates.
    """

    explanation = explanation.strip()

    if not explanation:
        raise ValueError(
            "Explain what was verified or clarified."
        )

    with get_connection() as conn:
        with conn.cursor() as cur:

            _lock_document(cur, document_id)

            revision = _latest_revision(
                cur,
                document_id,
                for_update=True
            )

            candidates = _revision_candidates(
                cur,
                revision["revision_id"]
            )

            _validate_revision(
                candidates,
                revision["revision_no"]
            )

            previous_status = revision["review_status"]

            if previous_status not in (
                "further_verification_required",
                "changes_requested"
            ):
                raise ValueError(
                    "Only Changes Requested or Further "
                    "Verification revisions can be "
                    "reopened without changing values."
                )

            cur.execute(
                """
                UPDATE extraction_revisions
                SET
                    review_status = 'pending',
                    review_note = %s,
                    reviewed_at = NULL
                WHERE revision_id = %s
                """,
                (
                    explanation,
                    revision["revision_id"]
                )
            )

            _record_event(
                cur,
                revision["revision_id"],
                "clarification_submitted",
                previous_status,
                "pending",
                explanation
            )

    return revision["revision_no"]


# =====================================================
# 7. VALIDATE CORRECTED FIELD VALUES
# =====================================================

def _parse_value(field_name, supplied):
    """Validate a value corrected by an engineer."""

    raw_value = str(supplied).strip()

    if not raw_value:
        raise ValueError(
            f"{FIELD_LABELS[field_name]} cannot be blank."
        )

    if field_name in TECHNOLOGY_FIELDS:
        return raw_value, None

    if raw_value.upper() in ("N/A", "NA"):
        if field_name in OPTIONAL_NUMERIC_FIELDS:
            return "N/A", None

        raise ValueError(
            f"{FIELD_LABELS[field_name]} "
            "requires a number."
        )

    cleaned = (
        raw_value.replace(",", "")
        .replace("%", "")
    )

    try:
        number = Decimal(cleaned)

    except InvalidOperation:
        raise ValueError(
            f"Invalid number for {FIELD_LABELS[field_name]}."
        ) from None

    if not number.is_finite():
        raise ValueError(
            f"{FIELD_LABELS[field_name]} "
            "must be finite."
        )

    if (
        field_name != "reported_energy_saving_percent"
        and number < 0
    ):
        raise ValueError(
            f"{FIELD_LABELS[field_name]} "
            "cannot be negative."
        )

    if (
        number.as_tuple().exponent < -3
        and number != number.quantize(
            Decimal("0.001")
        )
    ):
        raise ValueError(
            f"{FIELD_LABELS[field_name]} "
            "supports at most 3 decimals."
        )

    return raw_value, number


# =====================================================
# 8. CORRECT VALUES AND CREATE A NEW REVISION
# =====================================================

def resubmit_revision(
    document_id,
    corrected_values,
    resubmission_note
):
    """
    Create a new pending revision only when
    at least one source field actually changes.

    Previous revisions remain unchanged.
    """

    resubmission_note = resubmission_note.strip()

    if not resubmission_note:
        raise ValueError(
            "A correction explanation is required."
        )

    if set(corrected_values) != EXPECTED_FIELDS:
        raise ValueError(
            "All 16 fields must be submitted."
        )

    with get_connection() as conn:
        with conn.cursor() as cur:

            _lock_document(cur, document_id)

            previous_revision = _latest_revision(
                cur,
                document_id,
                for_update=True
            )

            previous = _revision_candidates(
                cur,
                previous_revision["revision_id"]
            )

            _validate_revision(
                previous,
                previous_revision["revision_no"]
            )

            old_status = previous_revision["review_status"]

            if old_status not in (
                "changes_requested",
                "further_verification_required"
            ):
                raise ValueError(
                    "Corrections require a Changes "
                    "Requested or Further Verification "
                    "Required revision."
                )

            old_by_name = {
                candidate["field_name"]: candidate
                for candidate in previous
            }

            prepared = {}

            # Engineer-supplied fields
            for field_name in EXPECTED_FIELDS:

                if field_name == CALCULATED_FIELD:
                    continue

                prepared[field_name] = _parse_value(
                    field_name,
                    corrected_values[field_name]
                )

            # Automatically recalculate the saving
            existing = prepared[
                "existing_annual_energy_consumption"
            ][1]

            proposed = prepared[
                "proposed_annual_energy_consumption"
            ][1]

            if existing <= 0:
                raise ValueError(
                    "Existing annual energy must "
                    "be greater than zero."
                )

            saving = (
                (existing - proposed)
                / existing
                * Decimal("100")
            ).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )

            prepared[CALCULATED_FIELD] = (
                str(saving),
                saving
            )

            # Only source-field corrections create revisions
            changed_fields = []

            for field_name in EXPECTED_FIELDS:

                if field_name == CALCULATED_FIELD:
                    continue

                old = old_by_name[field_name]
                new_raw, new_number = prepared[field_name]

                if field_name in TECHNOLOGY_FIELDS:
                    changed = (
                        new_raw != old["raw_value"]
                    )

                else:
                    changed = (
                        new_number != old["numeric_value"]
                    )

                if changed:
                    changed_fields.append(field_name)

            if not changed_fields:
                raise ValueError(
                    "No extracted values changed. "
                    "Use Submit Clarification to "
                    "reopen the same revision instead."
                )

            new_revision_no = (
                previous_revision["revision_no"] + 1
            )

            # One new revision record
            cur.execute(
                """
                INSERT INTO extraction_revisions (
                    document_id,
                    revision_no,
                    review_status
                )
                VALUES (%s, %s, 'pending')
                RETURNING revision_id
                """,
                (
                    document_id,
                    new_revision_no
                )
            )

            new_revision_id = cur.fetchone()[0]

            # Copy 16 fields into the new revision
            for field_name in FIELD_LABELS:

                old = old_by_name[field_name]

                raw_value, numeric_value = (
                    prepared[field_name]
                )

                cur.execute(
                    """
                    INSERT INTO extraction_candidates (
                        page_id,
                        field_name,
                        raw_value,
                        numeric_value,
                        unit,
                        revision_no,
                        extraction_revision_id,
                        review_status
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, 'pending'
                    )
                    """,
                    (
                        old["page_id"],
                        field_name,
                        raw_value,
                        numeric_value,
                        old["unit"],
                        new_revision_no,
                        new_revision_id
                    )
                )

            # Record what happened to the old revision
            _record_event(
                cur,
                previous_revision["revision_id"],
                "corrected_revision_created",
                old_status,
                old_status,
                (
                    f"Revision {new_revision_no} created. "
                    f"{resubmission_note}"
                )
            )

            # Record submission of the new revision
            _record_event(
                cur,
                new_revision_id,
                "revision_submitted",
                None,
                "pending",
                resubmission_note
            )

    return new_revision_no
