from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from database import get_connection
from services.document_revision_repository import insert_revision
from services.engineering_value_repository import insert_engineering_values


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
    "existing_elevator_technology", "proposed_elevator_technology",
}
OPTIONAL_NUMERIC_FIELDS = {
    "existing_annual_regenerative_energy",
    "proposed_annual_regenerative_energy",
    "existing_embodied_co2e",
}
REVIEW_DECISIONS = {
    "approved", "changes_requested", "further_verification_required", "rejected",
}
EDITABLE_STATUSES = {
    "pending", "changes_requested", "further_verification_required",
}
VALUE_COLUMNS = (
    "field_name", "phase", "value_kind", "raw_source_value",
    "numeric_value", "text_value", "unit", "source_page", "excel_sheet",
    "excel_cell", "excel_formula", "source_label", "source_note",
    "measured_on", "period_start", "period_end", "source_verified",
    "is_corrected", "correction_reason",
)


def _rows(cursor):
    columns = [column.name for column in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _lock(cursor, document_id):
    cursor.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (document_id,))


def _latest(cursor, document_id, for_update=False):
    query = """
        SELECT revision_id, revision_no, review_status, review_note, reviewed_at
        FROM extraction_revisions
        WHERE document_id = %s
        ORDER BY revision_no DESC
        LIMIT 1
    """
    cursor.execute(query + (" FOR UPDATE" if for_update else ""), (document_id,))
    revisions = _rows(cursor)
    if not revisions:
        raise ValueError("This document has no extraction revision")
    return revisions[0]


def _candidates(cursor, revision_id):
    cursor.execute(
        """
        SELECT candidate_id, page_id, field_name, raw_value,
               numeric_value, unit, revision_no
        FROM extraction_candidates
        WHERE extraction_revision_id = %s
        ORDER BY field_name
        """,
        (revision_id,),
    )
    return _rows(cursor)


def _validate(candidates, revision_no):
    names = [item["field_name"] for item in candidates]
    if len(names) != 16 or set(names) != EXPECTED_FIELDS:
        raise ValueError("The revision must contain exactly 16 unique comparison fields")
    if len({item["page_id"] for item in candidates}) != 1:
        raise ValueError("Comparison fields must reference one source PDF page")
    if any(item["revision_no"] != revision_no for item in candidates):
        raise ValueError("Candidate and revision numbers do not match")


def _validated_revision(cursor, document_id):
    _lock(cursor, document_id)
    revision = _latest(cursor, document_id, for_update=True)
    _validate(_candidates(cursor, revision["revision_id"]), revision["revision_no"])
    return revision


def _event(cursor, revision_id, action, old_status, new_status, reason):
    cursor.execute(
        """
        INSERT INTO enercal_review_events
            (revision_id, event_type, previous_status, new_status, reason)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (revision_id, action, old_status, new_status, reason),
    )


def _unified_revision(cursor, legacy_document_id):
    cursor.execute(
        """
        SELECT d.document_id, r.revision_id, r.revision_no,
               r.review_status, r.review_scope
        FROM source_documents s
        JOIN project_documents d
          ON d.case_id = s.case_id
         AND d.document_type = 'mod_pdf'
         AND d.file_sha256 = s.file_sha256
        JOIN document_revisions r ON r.document_id = d.document_id
        WHERE s.document_id = %s
        ORDER BY r.revision_no DESC
        LIMIT 1
        FOR UPDATE OF r
        """,
        (legacy_document_id,),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    keys = ("document_id", "revision_id", "revision_no", "review_status", "review_scope")
    return dict(zip(keys, row))


def _unified_values(cursor, revision_id):
    cursor.execute(
        f"SELECT {', '.join(VALUE_COLUMNS)} FROM engineering_values "
        "WHERE revision_id = %s ORDER BY phase, field_name",
        (revision_id,),
    )
    return [dict(zip(VALUE_COLUMNS, row)) for row in cursor.fetchall()]


def _review_unified(cursor, revision_id, decision, reason=None):
    cursor.execute(
        "SELECT review_document_revision(%s, %s, %s, %s, FALSE)",
        (revision_id, decision, reason, None),
    )
    if cursor.fetchone() is None:
        raise RuntimeError("Unified review event was not created")


def _clone_unified(cursor, current, *, scope=None, corrections=None, reason=None):
    values = _unified_values(cursor, current["revision_id"])
    if not values:
        raise ValueError("Unified revision has no engineering values")

    if corrections:
        indexes = {(value["phase"], value["field_name"]): value for value in values}
        for legacy_name, (raw, number) in corrections.items():
            if legacy_name.startswith("existing_"):
                phase, field = "existing", legacy_name.removeprefix("existing_")
            elif legacy_name.startswith("proposed_"):
                phase, field = "proposed", legacy_name.removeprefix("proposed_")
            else:
                phase, field = "common", legacy_name
            value = indexes.get((phase, field))
            if value is None:
                raise ValueError(f"Unified field missing: {phase}.{field}")
            previous = value["numeric_value"] if number is not None else value["text_value"]
            replacement = number if number is not None else (None if raw == "N/A" else raw)
            if previous != replacement:
                value.update(
                    numeric_value=number,
                    text_value=None if number is not None or raw == "N/A" else raw,
                    source_verified=False,
                    is_corrected=True,
                    correction_reason=reason,
                )

    revision = insert_revision(
        cursor, current["document_id"], scope or current["review_scope"]
    )
    insert_engineering_values(cursor, revision["revision_id"], values)
    return {**revision, "review_scope": scope or current["review_scope"]}


def _sync_review(cursor, legacy_document_id, decision, note):
    current = _unified_revision(cursor, legacy_document_id)
    if current is None:
        return
    if current["review_status"] != "pending":
        raise ValueError("Unified revision is not pending; resubmit verification first")

    if decision == "approved" and current["review_scope"] == "full_values":
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM engineering_values
                WHERE revision_id = %s AND NOT source_verified
            )
            """,
            (current["revision_id"],),
        )
        unverified = cursor.fetchone()[0]
        if unverified:
            _review_unified(
                cursor, current["revision_id"],
                "further_verification_required",
                "The MOD comparison was reviewed; full-value verification remains outstanding.",
            )
            current = _clone_unified(cursor, current, scope="comparison_only")

    _review_unified(cursor, current["revision_id"], decision, note or None)


def _sync_clarification(cursor, legacy_document_id):
    current = _unified_revision(cursor, legacy_document_id)
    if current is None:
        return
    if current["review_status"] not in {
        "changes_requested", "further_verification_required"
    }:
        raise ValueError("Unified revision cannot be reopened")
    _clone_unified(cursor, current, scope="comparison_only")


def _sync_correction(cursor, legacy_document_id, updates, note):
    current = _unified_revision(cursor, legacy_document_id)
    if current is None:
        return
    if current["review_status"] == "pending":
        _review_unified(
            cursor, current["revision_id"], "changes_requested",
            "A corrected MOD comparison was submitted.",
        )
    _clone_unified(
        cursor, current, scope="comparison_only",
        corrections=updates, reason=note,
    )


def get_review_documents():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT d.document_id, m.case_ref, d.filename
            FROM source_documents d
            JOIN modernisation_cases m ON m.case_id = d.case_id
            JOIN extraction_revisions r ON r.document_id = d.document_id
            ORDER BY d.document_id DESC
            """
        )
        return cur.fetchall()


def get_review_details(document_id):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT filename, pdf_bytes FROM source_documents WHERE document_id = %s",
            (document_id,),
        )
        document = cur.fetchone()
        if document is None:
            raise ValueError("Document not found")

        revision = _latest(cur, document_id)
        candidates = _candidates(cur, revision["revision_id"])
        cur.execute(
            """
            SELECT * FROM enercal_comparison_view
            WHERE document_id = %s AND revision_no = %s
            ORDER BY CASE technology_role WHEN 'existing' THEN 1 ELSE 2 END
            """,
            (document_id, revision["revision_no"]),
        )
        comparison = _rows(cur)
        cur.execute(
            """
            SELECT r.revision_id, r.revision_no, r.review_status, r.review_note,
                   r.created_at, r.reviewed_at,
                   COUNT(c.candidate_id) AS field_count
            FROM extraction_revisions r
            LEFT JOIN extraction_candidates c
              ON c.extraction_revision_id = r.revision_id
            WHERE r.document_id = %s
            GROUP BY r.revision_id ORDER BY r.revision_no
            """,
            (document_id,),
        )
        history = _rows(cur)
        cur.execute(
            """
            SELECT e.event_id, r.revision_no, e.event_type, e.previous_status,
                   e.new_status, e.reason, e.created_at
            FROM enercal_review_events e
            JOIN extraction_revisions r ON r.revision_id = e.revision_id
            WHERE r.document_id = %s ORDER BY e.event_id
            """,
            (document_id,),
        )
        events = _rows(cur)
    return {
        "filename": document[0], "pdf_bytes": bytes(document[1]),
        "revision_id": revision["revision_id"],
        "revision_no": revision["revision_no"],
        "status": revision["review_status"],
        "candidates": candidates, "comparison": comparison,
        "history": history, "events": events,
    }


def get_specifications(document_id):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT field_name, field_value, unit, source_page
            FROM enercal_specifications WHERE document_id = %s
            ORDER BY source_page, field_name
            """,
            (document_id,),
        )
        return [
            {
                "Field": name.replace("_", " ").capitalize(),
                "Value": value, "Unit": unit or "", "Source page": page,
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
        revision = _validated_revision(cur, document_id)
        if revision["review_status"] != "pending":
            raise ValueError("Only pending revisions can receive a review decision")
        _sync_review(cur, document_id, decision, review_note)
        cur.execute(
            """
            UPDATE extraction_revisions
            SET review_status = %s, review_note = %s, reviewed_at = NOW()
            WHERE revision_id = %s AND review_status = 'pending'
            """,
            (decision, review_note or None, revision["revision_id"]),
        )
        if cur.rowcount != 1:
            raise RuntimeError("The review status changed during approval")
        _event(
            cur, revision["revision_id"], decision,
            "pending", decision, review_note or None,
        )
    return revision["revision_no"]


def submit_verification(document_id, explanation):
    explanation = explanation.strip()
    if not explanation:
        raise ValueError("A clarification reason is required")

    with get_connection() as conn, conn.cursor() as cur:
        revision = _validated_revision(cur, document_id)
        old_status = revision["review_status"]
        if old_status not in {"changes_requested", "further_verification_required"}:
            raise ValueError("Only revisions awaiting clarification can be reopened")
        _sync_clarification(cur, document_id)
        cur.execute(
            """
            UPDATE extraction_revisions
            SET review_status = 'pending', review_note = %s, reviewed_at = NULL
            WHERE revision_id = %s
            """,
            (explanation, revision["revision_id"]),
        )
        _event(
            cur, revision["revision_id"], "clarification_submitted",
            old_status, "pending", explanation,
        )
    return revision["revision_no"]


def _parse_value(field_name, supplied):
    raw = str(supplied).strip()
    if not raw:
        raise ValueError(f"{FIELD_LABELS[field_name]} cannot be blank")
    if field_name in TECHNOLOGY_FIELDS:
        return raw, None
    if raw.upper() in {"N/A", "NA"}:
        if field_name in OPTIONAL_NUMERIC_FIELDS:
            return "N/A", None
        raise ValueError(f"{FIELD_LABELS[field_name]} requires a number")
    try:
        number = Decimal(raw.replace(",", "").replace("%", ""))
    except InvalidOperation:
        raise ValueError(f"Invalid number for {FIELD_LABELS[field_name]}") from None
    if not number.is_finite():
        raise ValueError(f"Invalid value for {FIELD_LABELS[field_name]}")
    if number < 0 and field_name != "reported_energy_saving_percent":
        raise ValueError(f"{FIELD_LABELS[field_name]} cannot be negative")
    if number.as_tuple().exponent < -3 and number != number.quantize(Decimal("0.001")):
        raise ValueError(f"{FIELD_LABELS[field_name]} supports at most 3 decimals")
    return raw, number


def resubmit_revision(document_id, corrected_values, resubmission_note):
    resubmission_note = resubmission_note.strip()
    if not resubmission_note:
        raise ValueError("Explain the correction before saving")
    if set(corrected_values) != EXPECTED_FIELDS:
        raise ValueError("Submit all 16 comparison fields")

    with get_connection() as conn, conn.cursor() as cur:
        previous = _validated_revision(cur, document_id)
        source = _candidates(cur, previous["revision_id"])
        old_status = previous["review_status"]
        if old_status not in EDITABLE_STATUSES:
            raise ValueError("Only reports awaiting review or correction can be edited")
        originals = {row["field_name"]: row for row in source}
        values = {
            key: _parse_value(key, corrected_values[key])
            for key in FIELD_LABELS if key != CALCULATED_FIELD
        }
        before = values["existing_annual_energy_consumption"][1]
        after = values["proposed_annual_energy_consumption"][1]
        if before <= 0:
            raise ValueError("Existing annual energy must be greater than zero")
        saving = ((before - after) / before * 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP,
        )
        values[CALCULATED_FIELD] = (str(saving), saving)
        changed = [
            key for key in FIELD_LABELS if key != CALCULATED_FIELD
            and (
                values[key][0] != originals[key]["raw_value"]
                if key in TECHNOLOGY_FIELDS
                else values[key][1] != originals[key]["numeric_value"]
            )
        ]
        if not changed:
            raise ValueError("No source values changed")

        new_number = previous["revision_no"] + 1
        _sync_correction(cur, document_id, values, resubmission_note)
        cur.execute(
            """
            INSERT INTO extraction_revisions
                (document_id, revision_no, review_status)
            VALUES (%s, %s, 'pending') RETURNING revision_id
            """,
            (document_id, new_number),
        )
        new_id = cur.fetchone()[0]
        cur.executemany(
            """
            INSERT INTO extraction_candidates
                (page_id, field_name, raw_value, numeric_value, unit,
                 revision_no, extraction_revision_id, review_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
            """,
            [
                (
                    originals[key]["page_id"], key, values[key][0],
                    values[key][1], originals[key]["unit"], new_number, new_id,
                )
                for key in FIELD_LABELS
            ],
        )
        _event(
            cur, previous["revision_id"], "corrected_revision_created",
            old_status, old_status, f"Revision {new_number} created {resubmission_note}",
        )
        _event(
            cur, new_id, "revision_submitted", None,
            "pending", resubmission_note,
        )
    return new_number
