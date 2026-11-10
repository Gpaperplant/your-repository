
from decimal import Decimal, InvalidOperation

from database import get_connection


def _dict_rows(cur):
    columns = [item.name for item in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def list_measurement_candidates(elevator_id, limit=100):
    # List measured period values from approved sources only.
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("Limit must be between 1 and 500.")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    o.observation_id,
                    o.source_id,
                    o.metric_name,
                    o.numeric_value,
                    o.unit,
                    o.period_start,
                    o.period_end
                FROM energy_observations o
                JOIN measurement_sources s
                    ON s.source_id = o.source_id
                WHERE s.verified_elevator_id = %s
                  AND s.review_status = 'approved'
                  AND LOWER(BTRIM(o.value_kind))
                      IN ('measured', 'metered')
                  AND LOWER(BTRIM(o.unit)) = 'kwh'
                  AND o.numeric_value IS NOT NULL
                  AND o.period_start IS NOT NULL
                  AND o.period_end IS NOT NULL
                ORDER BY
                    o.period_start DESC,
                    o.observation_id DESC
                LIMIT %s
                """,
                (elevator_id, limit),
            )
            return _dict_rows(cur)


def list_measurement_pair_revisions(case_id):
    # Preserve rejected revisions and their submission and review notes.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    pair_id,
                    revision_no,
                    document_id,
                    before_observation_id,
                    after_observation_id,
                    before_days,
                    after_days,
                    review_status,
                    submission_note,
                    review_note,
                    created_at,
                    reviewed_at
                FROM verified_measurement_pairs
                WHERE case_id = %s
                ORDER BY revision_no DESC
                """,
                (case_id,),
            )
            return _dict_rows(cur)


def _period_days(observation):
    # Calculate duration using inclusive dates.
    start = observation["period_start"]
    end = observation["period_end"]

    if start is None or end is None or end < start:
        raise ValueError(
            "Each observation requires a valid measurement period."
        )

    return (end - start).days + 1


def _check_observations(before, after, elevator_id):
    # Reject unmatched sources and non-metered energy values.
    if before["observation_id"] == after["observation_id"]:
        raise ValueError("Choose two different observations.")

    if before["source_id"] == after["source_id"]:
        raise ValueError(
            "Before and after must use different source files."
        )

    if before["metric_name"] != after["metric_name"]:
        raise ValueError(
            "Before and after must have the same metric name."
        )

    for row in (before, after):
        if row["review_status"] != "approved":
            raise ValueError(
                "Both source files must be approved first."
            )

        if row["verified_elevator_id"] != elevator_id:
            raise ValueError(
                "A source file is not linked to the selected elevator."
            )

        if str(row["value_kind"] or "").strip().lower() not in (
            "measured", "metered"
        ):
            raise ValueError(
                "Calculated and estimated values cannot be used as targets."
            )

        if str(row["unit"] or "").strip().lower() != "kwh":
            raise ValueError(
                "Both observations must be period energy totals in kWh."
            )

    try:
        before_kwh = Decimal(str(before["numeric_value"]))
        after_kwh = Decimal(str(after["numeric_value"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            "Both observations need numeric kWh values."
        ) from exc

    if not before_kwh.is_finite() or not after_kwh.is_finite():
        raise ValueError("Measurement values must be finite.")

    if before_kwh <= 0 or after_kwh < 0:
        raise ValueError(
            "Before energy must be positive; "
            "after energy cannot be negative."
        )

    before_days = _period_days(before)
    after_days = _period_days(after)

    if before["period_end"] >= after["period_start"]:
        raise ValueError(
            "The after period must begin after the before period ends."
        )

    return before_days, after_days


def _load_observations(cur, before_id, after_id):
    # Read both source observations within the same transaction.
    cur.execute(
        """
        SELECT
            o.observation_id,
            o.source_id,
            o.metric_name,
            o.numeric_value,
            o.unit,
            o.value_kind,
            o.period_start,
            o.period_end,
            s.review_status,
            s.verified_elevator_id
        FROM energy_observations o
        JOIN measurement_sources s
            ON s.source_id = o.source_id
        WHERE o.observation_id = ANY(%s)
        ORDER BY o.observation_id
        FOR SHARE OF o, s
        """,
        ([before_id, after_id],),
    )

    rows = {
        row["observation_id"]: row
        for row in _dict_rows(cur)
    }

    if before_id not in rows or after_id not in rows:
        raise ValueError(
            "One or both observations do not exist."
        )

    return rows[before_id], rows[after_id]


def create_measurement_pair(
    case_id,
    document_id,
    before_observation_id,
    after_observation_id,
    note,
):
    # Create a pending revision while preserving previously rejected pairs.
    note = str(note or "").strip()

    if not note:
        raise ValueError(
            "Explain why these observation periods were selected."
        )

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Lock the case so simultaneous submissions cannot reuse a revision number.
            cur.execute(
                """
                SELECT m.elevator_id, m.status
                FROM modernisation_cases m
                JOIN source_documents d
                    ON d.case_id = m.case_id
                WHERE m.case_id = %s
                  AND d.document_id = %s
                FOR UPDATE OF m
                """,
                (case_id, document_id),
            )

            project = cur.fetchone()

            if project is None:
                raise ValueError(
                    "EnerCal document does not belong to this case."
                )

            elevator_id, status = project

            if status != "completed":
                raise ValueError(
                    "The modernisation case must be completed."
                )

            cur.execute(
                """
                SELECT revision_no, review_status
                FROM verified_measurement_pairs
                WHERE case_id = %s
                ORDER BY revision_no DESC
                LIMIT 1
                FOR UPDATE
                """,
                (case_id,),
            )

            previous = cur.fetchone()

            if previous and previous[1] != "rejected":
                raise ValueError(
                    "An active pair already exists for this case."
                )

            revision_no = previous[0] + 1 if previous else 1

            before, after = _load_observations(
                cur,
                before_observation_id,
                after_observation_id,
            )

            before_days, after_days = _check_observations(
                before,
                after,
                elevator_id,
            )

            cur.execute(
                """
                INSERT INTO verified_measurement_pairs (
                    case_id,
                    document_id,
                    revision_no,
                    before_observation_id,
                    after_observation_id,
                    before_days,
                    after_days,
                    submission_note
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING pair_id
                """,
                (
                    case_id,
                    document_id,
                    revision_no,
                    before_observation_id,
                    after_observation_id,
                    before_days,
                    after_days,
                    note,
                ),
            )

            return cur.fetchone()[0]


def review_measurement_pair(
    pair_id,
    decision,
    note,
    *,
    conditions_verified=False,
    period_energy_verified=False,
    specifications_verified=False,
):
    # Require explicit reviewer confirmation before approval.
    if decision not in ("approved", "rejected"):
        raise ValueError(
            "Decision must be approved or rejected."
        )

    note = str(note or "").strip()

    if not note:
        raise ValueError("A review explanation is required.")

    if decision == "approved" and not (
        conditions_verified is True
        and period_energy_verified is True
    ):
        raise ValueError(
            "Confirm both comparability and actual period energy."
        )

    if type(specifications_verified) is not bool:
        raise ValueError(
            "Specifications confirmation must be True or False."
        )

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    p.review_status,
                    m.elevator_id,
                    p.before_observation_id,
                    p.after_observation_id,
                    p.before_days,
                    p.after_days
                FROM verified_measurement_pairs p
                JOIN modernisation_cases m
                    ON m.case_id = p.case_id
                WHERE p.pair_id = %s
                FOR UPDATE OF p
                """,
                (pair_id,),
            )

            result = cur.fetchone()

            if result is None:
                raise ValueError(
                    "Measurement pair not found."
                )

            (
                status,
                elevator_id,
                before_id,
                after_id,
                before_days,
                after_days,
            ) = result

            if status != "pending":
                raise ValueError(
                    "Only pending measurement pairs can be reviewed."
                )

            if decision == "approved":
                before, after = _load_observations(
                    cur,
                    before_id,
                    after_id,
                )

                checked_days = _check_observations(
                    before,
                    after,
                    elevator_id,
                )

                if checked_days != (before_days, after_days):
                    raise ValueError(
                        "Stored durations no longer match their sources."
                    )

            cur.execute(
                """
                UPDATE verified_measurement_pairs
                SET
                    review_status = %s,
                    review_note = %s,
                    conditions_verified = %s,
                    period_energy_verified = %s,
                    specifications_verified = %s,
                    reviewed_at = NOW()
                WHERE pair_id = %s
                  AND review_status = 'pending'
                """,
                (
                    decision,
                    note,
                    conditions_verified
                        if decision == "approved" else False,
                    period_energy_verified
                        if decision == "approved" else False,
                    specifications_verified
                        if decision == "approved" else False,
                    pair_id,
                ),
            )

            if cur.rowcount != 1:
                raise RuntimeError(
                    "Measurement pair changed during review."
                )

            if decision == "approved":
                # Roll back approval if the pair fails the database view checks.
                cur.execute(
                    """
                    SELECT 1
                    FROM ai_verified_measurement_pairs_view
                    WHERE pair_id = %s
                    """,
                    (pair_id,),
                )

                if cur.fetchone() is None:
                    raise ValueError(
                        "This pair fails AI eligibility checks. "
                        "Review its sources, measurement types, "
                        "dates and elevator identity."
                    )

    return decision
