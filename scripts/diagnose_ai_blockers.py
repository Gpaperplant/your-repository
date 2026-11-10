
"""Diagnose the next verified-data blocker without modifying project records."""

from collections import Counter

from database import get_connection


REQUIRED_RELATIONS = (
    "modernisation_cases",
    "source_documents",
    "enercal_approved_comparison_view",
    "measurement_sources",
    "energy_observations",
    "verified_measurement_pairs",
    "ai_verified_measurement_pairs_view",
)


CASE_DIAGNOSTICS_SQL = """
WITH approved_documents AS (
    SELECT DISTINCT d.case_id
    FROM source_documents d
    JOIN enercal_approved_comparison_view v
        ON v.document_id = d.document_id
    GROUP BY d.case_id, d.document_id
    HAVING COUNT(*) FILTER (
        WHERE v.technology_role = 'existing'
    ) = 1
       AND COUNT(*) FILTER (
        WHERE v.technology_role = 'proposed'
    ) = 1
),
valid_observations AS (
    SELECT
        s.verified_elevator_id AS elevator_id,
        o.source_id,
        o.metric_name,
        o.numeric_value,
        o.period_start,
        o.period_end
    FROM measurement_sources s
    JOIN energy_observations o ON o.source_id = s.source_id
    WHERE s.review_status = 'approved'
      AND s.verified_elevator_id IS NOT NULL
      AND LOWER(BTRIM(o.value_kind)) IN ('measured', 'metered')
      AND LOWER(BTRIM(o.unit)) = 'kwh'
      AND o.metric_name IS NOT NULL
      AND o.numeric_value >= 0
      AND o.period_start IS NOT NULL
      AND o.period_end >= o.period_start
),
observation_counts AS (
    SELECT
        elevator_id,
        COUNT(*) AS observation_count,
        COUNT(DISTINCT source_id) AS source_count
    FROM valid_observations
    GROUP BY elevator_id
),
chronological_candidates AS (
    SELECT DISTINCT b.elevator_id
    FROM valid_observations b
    JOIN valid_observations a
      ON a.elevator_id = b.elevator_id
     AND a.source_id <> b.source_id
     AND a.metric_name = b.metric_name
     AND b.period_end < a.period_start
    WHERE b.numeric_value > 0
)
SELECT
    m.case_id,
    m.case_ref,
    m.elevator_id,
    m.status AS case_status,
    ad.case_id IS NOT NULL AS approved_enercal,
    COALESCE(oc.observation_count, 0) AS observation_count,
    COALESCE(oc.source_count, 0) AS source_count,
    cc.elevator_id IS NOT NULL AS candidate_periods,
    latest.pair_id,
    latest.revision_no,
    latest.review_status,
    EXISTS (
        SELECT 1 FROM ai_verified_measurement_pairs_view v
        WHERE v.case_id = m.case_id
    ) AS ai_eligible
FROM modernisation_cases m
LEFT JOIN approved_documents ad ON ad.case_id = m.case_id
LEFT JOIN observation_counts oc ON oc.elevator_id = m.elevator_id
LEFT JOIN chronological_candidates cc ON cc.elevator_id = m.elevator_id
LEFT JOIN LATERAL (
    SELECT p.pair_id, p.revision_no, p.review_status
    FROM verified_measurement_pairs p
    WHERE p.case_id = m.case_id
    ORDER BY p.revision_no DESC
    LIMIT 1
) latest ON TRUE
ORDER BY m.case_ref, m.case_id
"""


def _dict_rows(cur):
    # Preserve column names so diagnostics remain easy to inspect.
    columns = [column.name for column in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _blocker(row):
    # Report the earliest actionable gate, not a prediction of data quality.
    if row["case_status"] != "completed":
        return "Case unfinished: complete modernisation before pairing meter data"

    if not row["approved_enercal"]:
        return "Approve a complete existing/proposed EnerCal comparison"

    if row["pair_id"] is not None:
        if row["review_status"] == "pending":
            return "Engineering review required for pending measurement pair"

        if row["review_status"] == "rejected":
            return "Resubmit a corrected measurement pair"

        if row["review_status"] == "approved":
            return (
                "Eligible in database view; verify AI loader exclusions"
                if row["ai_eligible"]
                else "Approved pair excluded: inspect source eligibility and dates"
            )

        return "Investigate unexpected measurement pair review status"

    if row["source_count"] < 2:
        return "Provide two different approved sources with genuine metered kWh totals"

    if not row["candidate_periods"]:
        return "Check matching metrics, period order and non-overlapping dates"

    return "Submit a before/after measurement pair for engineering review"


def main():
    print("KONE FYP | Verified measurement blocker diagnosis")
    print("READ ONLY: no approvals, submissions or model training performed")

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Set read-only mode before querying project tables.
                cur.execute("SET TRANSACTION READ ONLY")
                cur.execute("SET LOCAL statement_timeout = '15s'")
                cur.execute("SELECT current_database()")
                database_name = cur.fetchone()[0]

                missing = []

                for relation in REQUIRED_RELATIONS:
                    cur.execute("SELECT to_regclass(%s)", (relation,))

                    if cur.fetchone()[0] is None:
                        missing.append(relation)

                if missing:
                    print("Database:", database_name)
                    print("Missing relations:", ", ".join(missing))
                    print("Check your applied EnerCal/measurement migrations.")
                    return 1

                cur.execute(
                    """
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'verified_measurement_pairs'
                      AND column_name = 'revision_no'
                      AND table_schema = current_schema()
                    """
                )

                if cur.fetchone() is None:
                    print("Migration 011 is not applied: revision_no is missing.")
                    return 1

                cur.execute(CASE_DIAGNOSTICS_SQL)
                rows = _dict_rows(cur)

    except Exception as exc:
        print(f"Database diagnosis failed: {exc}")
        return 1

    print(f"Database: {database_name}")
    print(f"Cases checked: {len(rows)}")

    if not rows:
        print("NEXT: Register a modernisation case and upload an EnerCal report.")
        return 0

    status_counts = Counter()
    eligible_elevators = set()

    for row in rows:
        blocker = _blocker(row)
        status_counts[blocker] += 1

        if row["ai_eligible"]:
            eligible_elevators.add(row["elevator_id"])

        revision = (
            row["revision_no"]
            if row["revision_no"] is not None
            else "—"
        )

        print(f"\nCase {row['case_ref']} | Elevator {row['elevator_id']}")

        print(
            f"  Approved EnerCal: {row['approved_enercal']} | "
            f"Candidate metered observations: {row['observation_count']} "
            f"from {row['source_count']} sources"
        )

        print(
            f"  Latest pair revision: {revision} | "
            f"Pair status: {row['review_status'] or 'none'} | "
            f"AI-view eligible: {row['ai_eligible']}"
        )

        print(f"  NEXT: {blocker}")

    print("\nSummary")

    for status, count in status_counts.most_common():
        print(f"  {count} case(s): {status}")

    print(f"AI-view eligible distinct elevators: {len(eligible_elevators)}")
    print("NOTE: Final AI training readiness requires the dataset loader checks.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
