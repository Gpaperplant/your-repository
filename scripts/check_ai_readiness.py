
from database import get_connection


# Read schema metadata and counts, not original measurement values.
MEASUREMENT_OBJECTS = (
    "measurement_sources",
    "energy_observations",
    "verified_measurement_pairs",
    "ai_verified_measurement_pairs_view",
)

OBJECTS = (
    "modernisation_cases",
    "source_documents",
    "enercal_specifications",
    "enercal_approved_comparison_view",
    *MEASUREMENT_OBJECTS,
)

COUNT_QUERIES = {
    "modernisation_cases": "SELECT COUNT(*) FROM modernisation_cases",
    "source_documents": "SELECT COUNT(*) FROM source_documents",
    "measurement_sources": "SELECT COUNT(*) FROM measurement_sources",
    "energy_observations": "SELECT COUNT(*) FROM energy_observations",
    "verified_measurement_pairs": "SELECT COUNT(*) FROM verified_measurement_pairs",
    "ai_verified_measurement_pairs_view": "SELECT COUNT(*) FROM ai_verified_measurement_pairs_view",
    "enercal_approved_comparison_view": (
        "SELECT COUNT(DISTINCT document_id) "
        "FROM enercal_approved_comparison_view"
    ),
}


def inspect_ai_readiness():
    # Check available columns before querying optional tables.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = ANY(%s)
                ORDER BY table_name, ordinal_position
                """,
                (list(OBJECTS),),
            )

            columns = {name: [] for name in OBJECTS}

            for table, column, data_type in cur.fetchall():
                columns[table].append((column, data_type))

            counts = {}

            for table, query in COUNT_QUERIES.items():
                if columns[table]:
                    cur.execute(query)
                    counts[table] = cur.fetchone()[0]

            statuses = []

            if "review_status" in dict(columns["measurement_sources"]):
                cur.execute(
                    """
                    SELECT review_status, COUNT(*)
                    FROM measurement_sources
                    GROUP BY review_status
                    ORDER BY review_status
                    """
                )
                statuses = cur.fetchall()

    return {
        "columns": columns,
        "counts": counts,
        "statuses": statuses,
    }


def print_ai_readiness(report):
    # Show the schema information needed for the AI database connection.
    columns = report["columns"]
    counts = report["counts"]

    print("\nAI DATABASE READINESS CHECK")
    print("=" * 35)

    for name in OBJECTS:
        status = "FOUND" if columns[name] else "MISSING"
        number = counts.get(name)
        suffix = f" | records: {number}" if number is not None else ""
        print(f"{name}: {status}{suffix}")

    print("\nMEASUREMENT COLUMN DEFINITIONS")
    print("=" * 35)

    for name in MEASUREMENT_OBJECTS:
        print(f"\n{name}:")

        if not columns[name]:
            print("  Not present in the connected database")

        for column, data_type in columns[name]:
            print(f"  {column}: {data_type}")

    if report["statuses"]:
        print("\nMEASUREMENT SOURCE REVIEW STATUS COUNTS")

        for status, count in report["statuses"]:
            print(f"  {status}: {count}")

    print("\nThis is a schema check, not proof of verified before/after")
    print("measurements or that any project is ready for AI training.")


if __name__ == "__main__":
    print_ai_readiness(inspect_ai_readiness())
