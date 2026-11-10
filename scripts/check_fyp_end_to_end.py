
"""Read-only end-to-end readiness check for the KONE modernisation AI workflow."""

from unittest.mock import patch

from database import get_connection
from scripts.check_ai_prediction_db import main as check_prediction_queries
from services import ai_dataset, ai_measurement_loader, ai_training


# Each query is fixed and only returns aggregate workflow counts.
COUNT_CHECKS = (
    (
        "Modernisation cases", ("modernisation_cases",),
        "SELECT COUNT(*) FROM modernisation_cases",
    ),
    (
        "Completed cases", ("modernisation_cases",),
        "SELECT COUNT(*) FROM modernisation_cases WHERE status = 'completed'",
    ),
    (
        "Uploaded EnerCal documents", ("source_documents",),
        "SELECT COUNT(*) FROM source_documents",
    ),
    (
        "Approved EnerCal documents", ("enercal_approved_comparison_view",),
        "SELECT COUNT(DISTINCT document_id) FROM enercal_approved_comparison_view",
    ),
    (
        "Measurement source files", ("measurement_sources",),
        "SELECT COUNT(*) FROM measurement_sources",
    ),
    (
        "Approved measurement sources", ("measurement_sources",),
        "SELECT COUNT(*) FROM measurement_sources WHERE review_status = 'approved'",
    ),
    (
        "Approved-source metered observations",
        ("measurement_sources", "energy_observations"),
        """
        SELECT COUNT(*)
        FROM energy_observations o
        JOIN measurement_sources s ON s.source_id = o.source_id
        WHERE s.review_status = 'approved'
          AND LOWER(BTRIM(o.value_kind)) IN ('measured', 'metered')
          AND LOWER(BTRIM(o.unit)) = 'kwh'
          AND o.numeric_value IS NOT NULL
          AND o.period_start IS NOT NULL
          AND o.period_end >= o.period_start
        """,
    ),
    (
        "Submitted measurement pairs", ("verified_measurement_pairs",),
        "SELECT COUNT(*) FROM verified_measurement_pairs",
    ),
    (
        "Pending measurement pairs", ("verified_measurement_pairs",),
        "SELECT COUNT(*) FROM verified_measurement_pairs WHERE review_status = 'pending'",
    ),
    (
        "Rejected pair revisions", ("verified_measurement_pairs",),
        "SELECT COUNT(*) FROM verified_measurement_pairs WHERE review_status = 'rejected'",
    ),
    (
        "Approved measurement pairs", ("verified_measurement_pairs",),
        "SELECT COUNT(*) FROM verified_measurement_pairs WHERE review_status = 'approved'",
    ),
    (
        "AI-eligible measurement pairs", ("ai_verified_measurement_pairs_view",),
        "SELECT COUNT(*) FROM ai_verified_measurement_pairs_view",
    ),
    (
        "AI-eligible distinct elevators", ("ai_verified_measurement_pairs_view",),
        "SELECT COUNT(DISTINCT elevator_id) FROM ai_verified_measurement_pairs_view",
    ),
)


REQUIRED_OBJECTS = (
    "modernisation_cases",
    "source_documents",
    "enercal_approved_comparison_view",
    "enercal_specifications",
    "measurement_sources",
    "energy_observations",
    "verified_measurement_pairs",
    "ai_verified_measurement_pairs_view",
)


def _readonly_connection():
    # Set session defaults before running any query against project records.
    conn = get_connection()

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
            )
            cur.execute(
                "SET SESSION statement_timeout = '15s'"
            )

        conn.commit()
        return conn

    except Exception:
        conn.close()
        raise


def _database_counts():
    # Check objects first, then count rows without reading raw PDFs or meters.
    counts = {}

    with _readonly_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            database_name = cur.fetchone()[0]

            print(f"Database: {database_name}")

            existing = set()

            for relation in REQUIRED_OBJECTS:
                cur.execute(
                    "SELECT to_regclass(%s)",
                    (relation,),
                )

                if cur.fetchone()[0] is not None:
                    existing.add(relation)

            missing = set(REQUIRED_OBJECTS) - existing

            for label, dependencies, query in COUNT_CHECKS:
                if any(
                    name not in existing
                    for name in dependencies
                ):
                    print(
                        f"SKIP: {label} "
                        "(missing database relation)"
                    )
                    continue

                cur.execute(query)
                counts[label] = cur.fetchone()[0]

                print(f"{label}: {counts[label]}")

    return counts, missing


def _check_verified_dataset():
    # Reuse the real loader, but force PostgreSQL sessions to be read-only.
    with (
        patch.object(
            ai_measurement_loader,
            "get_connection",
            _readonly_connection,
        ),
        patch.object(
            ai_dataset,
            "get_connection",
            _readonly_connection,
        ),
    ):
        dataset, excluded = (
            ai_measurement_loader.prepare_ai_dataset()
        )

    elevators = len({
        row["elevator_id"]
        for row in dataset
    })

    print(f"Eligible training projects: {len(dataset)}")
    print(f"Distinct training elevators: {elevators}")
    print(f"Excluded measurement pairs: {len(excluded)}")

    if excluded:
        print(
            "INFO: Review exclusion reasons "
            "in AI Training & Evaluation."
        )

    if elevators < ai_training.MIN_ELEVATORS:
        print(
            f"NOT READY: Training needs "
            f"{ai_training.MIN_ELEVATORS} "
            "distinct verified elevators."
        )
        return True

    # This is an in-memory evaluation, not a saved or deployed AI model.
    result = ai_training.train_and_evaluate(dataset)

    print(
        "PASS: In-memory AI evaluation completed "
        f"({result['train_elevators']} training elevators; "
        f"{result['test_elevators']} held-out elevators)."
    )

    for name, metrics in result["metrics"].items():
        print(
            f"  {name}: MAE "
            f"{metrics['mae_pp']:.3f} percentage points"
        )

    return True


def main():
    print(
        "KONE FYP end-to-end readiness check "
        "(no database writes)"
    )
    print("=" * 58)

    try:
        counts, missing = _database_counts()

    except Exception as exc:
        print(
            f"FAIL: Could not check PostgreSQL: {exc}"
        )
        return 1

    if missing:
        print(
            "FAIL: Missing database relations: "
            + ", ".join(sorted(missing))
        )

        print(
            "Apply or repair the required migrations "
            "before continuing."
        )
        return 1

    try:
        _check_verified_dataset()

    except Exception as exc:
        print(
            f"FAIL: Verified dataset or model evaluation: {exc}"
        )
        return 1

    print(
        "\nChecking AI Prediction page SQL "
        "and approved proposals..."
    )

    if check_prediction_queries() != 0:
        print(
            "FAIL: AI Prediction database "
            "compatibility check failed."
        )
        return 1

    if counts.get("AI-eligible measurement pairs", 0) == 0:
        print(
            "NEXT: Review and approve genuine "
            "before/after meter pairs."
        )

    elif counts.get("Pending measurement pairs", 0):
        print(
            "NEXT: Review the remaining "
            "pending measurement pairs."
        )

    else:
        print(
            "NEXT: Review training results "
            "and live page behavior."
        )

    print(
        "PASS: Read-only workflow checks completed."
    )
    print(
        "This is not a claim of verified model accuracy "
        "or production readiness."
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
