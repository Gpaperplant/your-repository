"""Read-only diagnostic for the unified MOD/ISO five-table workflow.

Works before and after the legacy MOD tables are archived. The meter-savings
service is checked separately, preserving the measured-energy data branch.

Run:
    uv run python -m scripts.check_fyp_live_workflow --self-test
    uv run python -m scripts.check_fyp_live_workflow
"""

from collections import Counter, defaultdict
import sys
import unittest


REQUIRED_RELATIONS = (
    "public.modernisation_cases",
    "public.project_documents",
    "public.document_revisions",
    "public.engineering_values",
    "public.document_reviews",
    "public.verified_measurement_pairs",
    "public.energy_observations",
    "public.measurement_sources",
    "public.ai_verified_measurement_pairs_view",
)

CASE_QUERY = """
SELECT m.case_ref, m.status AS case_status,
       d.document_id, d.filename, r.revision_no, r.review_status
FROM modernisation_cases m
LEFT JOIN project_documents d
       ON d.case_id = m.case_id AND d.document_type = 'mod_pdf'
LEFT JOIN LATERAL (
    SELECT revision_no, review_status
    FROM document_revisions
    WHERE document_id = d.document_id
    ORDER BY revision_no DESC LIMIT 1
) r ON TRUE
ORDER BY m.case_ref, d.document_id
"""

# Exactly one annual-energy input for each existing/proposed phase in the
# latest approved MOD revision. Only unified records are considered.
APPROVED_ROLES_QUERY = """
SELECT d.document_id, v.phase AS technology_role,
       COUNT(*) AS role_rows
FROM project_documents d
JOIN LATERAL (
    SELECT revision_id, review_status
    FROM document_revisions
    WHERE document_id = d.document_id
    ORDER BY revision_no DESC LIMIT 1
) r ON TRUE
JOIN engineering_values v ON v.revision_id = r.revision_id
    AND v.field_name = 'annual_energy_consumption'
    AND v.phase IN ('existing', 'proposed')
WHERE d.document_type = 'mod_pdf'
  AND r.review_status = 'approved'
GROUP BY d.document_id, v.phase
ORDER BY d.document_id, v.phase
"""

PAIRS_QUERY = """
SELECT review_status, COUNT(*) AS pair_count
FROM verified_measurement_pairs
GROUP BY review_status
ORDER BY review_status
"""


def analyse_cases(rows):
    """Count cases and unified MOD documents, including cases without PDFs."""
    cases = {row["case_ref"] for row in rows}
    no_pdf = {row["case_ref"] for row in rows if row["document_id"] is None}
    status_counts = Counter()
    for row in rows:
        status = row["review_status"] if row["document_id"] is not None else "no_pdf"
        status_counts[status or "no_revision"] += 1
    return {
        "cases": len(cases),
        "documents": sum(row["document_id"] is not None for row in rows),
        "cases_without_pdf": len(no_pdf),
        "review_statuses": status_counts,
    }


def validate_approved_roles(rows):
    grouped = defaultdict(dict)
    for document_id, role, count in rows:
        grouped[document_id][role] = grouped[document_id].get(role, 0) + count
    problems = []
    for document_id, roles in sorted(grouped.items()):
        if roles != {"existing": 1, "proposed": 1}:
            problems.append(
                f"Approved document {document_id} has role counts {roles}; "
                "expected one existing and one proposed annual-energy field."
            )
    return len(grouped) - len(problems), problems


def _fetch_dicts(cur, sql):
    cur.execute(sql)
    names = [column.name for column in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def read_db_snapshot():
    from database import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        cur.execute("SET LOCAL statement_timeout = '15s'")
        cur.execute("SELECT current_database()")
        db_name = cur.fetchone()[0]
        missing = []
        for relation in REQUIRED_RELATIONS:
            cur.execute("SELECT to_regclass(%s)", (relation,))
            if cur.fetchone()[0] is None:
                missing.append(relation)
        if missing:
            raise RuntimeError("Missing required unified relations: " + ", ".join(missing))
        cases = _fetch_dicts(cur, CASE_QUERY)
        approved = _fetch_dicts(cur, APPROVED_ROLES_QUERY)
        pairs = _fetch_dicts(cur, PAIRS_QUERY)
    return {
        "database": db_name,
        "cases": cases,
        "approved_roles": [
            (r["document_id"], r["technology_role"], r["role_rows"])
            for r in approved
        ],
        "pair_statuses": {str(r["review_status"]): r["pair_count"] for r in pairs},
    }


def run_live():
    print("KONE FYP | Read-only unified workflow check")
    try:
        snapshot = read_db_snapshot()
    except Exception as exc:
        print(f"FAIL: PostgreSQL workflow check failed: {exc}")
        return 1
    print(f"Database: {snapshot['database']}")
    summary = analyse_cases(snapshot["cases"])
    print(f"Registered cases: {summary['cases']}")
    print(f"Unified MOD PDFs: {summary['documents']}")
    print(f"Cases without unified MOD: {summary['cases_without_pdf']}")
    print("Latest unified engineering review statuses:")
    for status, count in sorted(summary["review_statuses"].items()):
        print(f"  {status}: {count}")
    complete, problems = validate_approved_roles(snapshot["approved_roles"])
    print(f"Complete approved MOD annual-energy comparisons: {complete}")
    for problem in problems:
        print("FAIL:", problem)
    print("Stored meter-pair review statuses:")
    if not snapshot["pair_statuses"]:
        print("  None stored")
    for status, count in sorted(snapshot["pair_statuses"].items()):
        print(f"  {status}: {count}")
    try:
        from services.verified_savings_reporting import load_verified_savings
        measured = load_verified_savings()
        print(f"Verified measured-vs-estimated rows: {len(measured)}")
    except Exception as exc:
        print(f"FAIL: Measured-savings diagnostic failed: {exc}")
        problems.append("Measured-savings diagnostic error")
    if problems:
        print(f"FAIL: {len(problems)} integration issue(s). No data was changed.")
        return 1
    print("PASS: Unified workflow and meter-savings checks completed.")
    print("No database data was changed.")
    return 0


class _SelfTests(unittest.TestCase):
    def example(self, case, doc=None, status=None):
        return {"case_ref": case, "case_status": "registered",
                "document_id": doc, "review_status": status}

    def test_no_cases_is_valid(self):
        result = analyse_cases([])
        self.assertEqual(result["cases"], 0)
        self.assertEqual(result["documents"], 0)

    def test_six_case_dataset(self):
        rows = [self.example(f"MOD100{i}", 52 + i, "pending") for i in range(1, 6)]
        rows.append(self.example("MOD1006"))
        summary = analyse_cases(rows)
        self.assertEqual(summary["cases"], 6)
        self.assertEqual(summary["documents"], 5)
        self.assertEqual(summary["cases_without_pdf"], 1)
        self.assertEqual(summary["review_statuses"]["pending"], 5)

    def test_multiple_pdfs_are_not_multiple_cases(self):
        result = analyse_cases([
            self.example("MOD1", 10, "approved"),
            self.example("MOD1", 11, "pending"),
        ])
        self.assertEqual(result["cases"], 1)
        self.assertEqual(result["documents"], 2)

    def test_complete_approved_roles(self):
        self.assertEqual(validate_approved_roles([
            (53, "existing", 1), (53, "proposed", 1),
        ]), (1, []))

    def test_no_approved_reports_is_valid(self):
        self.assertEqual(validate_approved_roles([]), (0, []))

    def test_partial_report_is_flagged(self):
        self.assertEqual(validate_approved_roles([(53, "existing", 1)])[0], 0)

    def test_duplicate_role_is_flagged(self):
        self.assertEqual(validate_approved_roles([
            (53, "existing", 2), (53, "proposed", 1),
        ])[0], 0)


def main():
    if "--self-test" in sys.argv:
        outcome = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(_SelfTests)
        )
        return 0 if outcome.wasSuccessful() else 1
    return run_live()


if __name__ == "__main__":
    raise SystemExit(main())
