"""Read-only live integration check for the KONE modernisation FYP.

Checks registered cases, EnerCal approval coverage, verified meter pairs,
and AI training readiness. No changes to PostgreSQL and no model training.

Run from the repository root:
    uv run python -m scripts.check_fyp_live_workflow --self-test
    uv run python -m scripts.check_fyp_live_workflow
"""

from collections import Counter, defaultdict
import sys
import unittest


REQUIRED_RELATIONS = (
    "public.modernisation_cases",
    "public.source_documents",
    "public.extraction_revisions",
    "public.enercal_approved_kpis_view",
    "public.verified_measurement_pairs",
    "public.ai_verified_measurement_pairs_view",
    "public.energy_observations",
    "public.measurement_sources",
)

CASE_QUERY = """
SELECT m.case_ref, m.status AS case_status,
       d.document_id, d.filename, r.revision_no, r.review_status
FROM modernisation_cases m
LEFT JOIN source_documents d ON d.case_id = m.case_id
LEFT JOIN LATERAL (
    SELECT revision_no, review_status
    FROM extraction_revisions
    WHERE document_id = d.document_id
    ORDER BY revision_no DESC
    LIMIT 1
) r ON TRUE
ORDER BY m.case_ref, d.document_id
"""

APPROVED_ROLES_QUERY = """
SELECT document_id, technology_role, COUNT(*) AS role_rows
FROM enercal_approved_kpis_view
GROUP BY document_id, technology_role
ORDER BY document_id, technology_role
"""

PAIRS_QUERY = """
SELECT p.review_status, COUNT(*) AS pair_count
FROM verified_measurement_pairs p
GROUP BY p.review_status
ORDER BY p.review_status
"""

ELIGIBLE_PAIRS_QUERY = """
SELECT COUNT(*) AS pair_count, COUNT(DISTINCT elevator_id) AS elevator_count
FROM ai_verified_measurement_pairs_view
"""


def analyse_cases(rows):
    """Summarise states; keep no-PDF cases as valid records."""
    cases = {row["case_ref"] for row in rows}
    no_pdf = {row["case_ref"] for row in rows if row["document_id"] is None}
    status_counts = Counter()
    for row in rows:
        status = (row["review_status"] if row["document_id"] is not None
                  else "no_pdf")
        status_counts[status or "no_revision"] += 1
    return {
        "cases": len(cases),
        "documents": sum(row["document_id"] is not None for row in rows),
        "cases_without_pdf": len(no_pdf),
        "review_statuses": status_counts,
    }


def validate_approved_roles(rows):
    """Check exact existing/proposed role coverage for each approved PDF."""
    grouped = defaultdict(dict)
    for document_id, role, count in rows:
        grouped[document_id][role] = grouped[document_id].get(role, 0) + count
    problems = []
    for document_id, roles in sorted(grouped.items()):
        if roles != {"existing": 1, "proposed": 1}:
            problems.append(
                f"Approved document {document_id} has role counts {roles}; "
                "expected one existing and one proposed row."
            )
    return len(grouped) - len(problems), problems


def training_ready(elevator_count, minimum):
    return elevator_count >= minimum


def _fetch_dicts(cur, sql, parameters=None):
    if parameters is None:
        cur.execute(sql)
    else:
        cur.execute(sql, parameters)
    columns = [column.name for column in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def read_db_snapshot():
    """Read only database catalog and workflow views.

    PostgreSQL validates the SELECT statements even when a view is empty.
    """
    from database import get_connection

    with get_connection() as conn, conn.cursor() as cur:
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
            raise RuntimeError(
                "Missing required database relations: " + ", ".join(missing)
                + ". Check whether migrations 008–011 were applied."
            )

        cases = _fetch_dicts(cur, CASE_QUERY)
        approved = _fetch_dicts(cur, APPROVED_ROLES_QUERY)
        pair_statuses = _fetch_dicts(cur, PAIRS_QUERY)
        eligible = _fetch_dicts(cur, ELIGIBLE_PAIRS_QUERY)[0]

    return {
        "database": database_name,
        "cases": cases,
        "approved_roles": [
            (row["document_id"], row["technology_role"], row["role_rows"])
            for row in approved
        ],
        "pair_statuses": {
            str(row["review_status"]): row["pair_count"] for row in pair_statuses
        },
        "eligible_pair_count": eligible["pair_count"],
        "eligible_elevator_count": eligible["elevator_count"],
    }


def run_live():
    print("KONE FYP | Read-only live integration check")
    try:
        snapshot = read_db_snapshot()
    except Exception as exc:
        print(f"FAIL: PostgreSQL workflow check failed: {exc}")
        return 1

    print(f"Database: {snapshot['database']}")
    summary = analyse_cases(snapshot["cases"])
    print(f"Registered cases: {summary['cases']}")
    print(f"Uploaded EnerCal PDFs: {summary['documents']}")
    print(f"Cases without PDF: {summary['cases_without_pdf']}")
    print("Latest engineering review statuses:")
    for status, count in sorted(summary["review_statuses"].items()):
        print(f"  {status}: {count}")

    for row in snapshot["cases"]:
        if row["document_id"] is None:
            print(f"  {row['case_ref']}: No PDF (valid empty state)")
        else:
            print(
                f"  {row['case_ref']}: PDF {row['document_id']}, "
                f"revision {row['revision_no'] or 'none'}, "
                f"{row['review_status'] or 'no revision'}"
            )

    complete, problems = validate_approved_roles(snapshot["approved_roles"])
    print(f"Complete approved KPI reports: {complete}")
    for problem in problems:
        print("FAIL:", problem)
    print("Stored meter-pair review statuses:")
    if not snapshot["pair_statuses"]:
        print("  None stored")
    for status, count in sorted(snapshot["pair_statuses"].items()):
        print(f"  {status}: {count}")
    print(f"Eligible independently verified meter pairs: {snapshot['eligible_pair_count']}")
    print(f"Distinct eligible elevators (pair view): {snapshot['eligible_elevator_count']}")

    # Exercise the same real data loaders used by the reporting and AI pages.
    # Do not start training; data readiness and absence of measurements are valid.
    try:
        from services.verified_savings_reporting import load_verified_savings
        measured_rows = load_verified_savings()
        print(f"Measured-vs-estimated service rows: {len(measured_rows)}")
    except Exception as exc:
        print(f"FAIL: Measured-vs-estimated integration failed: {exc}")
        problems.append("Measured-vs-estimated service error")

    try:
        from services.ai_measurement_loader import prepare_ai_dataset
        from services.ai_training import MIN_ELEVATORS
        dataset, excluded = prepare_ai_dataset()
        distinct = len({row["elevator_id"] for row in dataset})
        print(f"AI-ready projects: {len(dataset)}")
        print(f"AI excluded records: {len(excluded)}")
        print(f"AI-ready distinct elevators: {distinct} / {MIN_ELEVATORS}")
        if training_ready(distinct, MIN_ELEVATORS):
            print("AI readiness: minimum data count met (not a model-validation result)")
        else:
            print("AI readiness: NOT READY (expected when evidence is insufficient)")
    except Exception as exc:
        print(f"FAIL: AI dataset integration failed: {exc}")
        problems.append("AI dataset integration error")

    if problems:
        print(f"FAIL: {len(problems)} integration issue(s). No data was changed.")
        return 1
    print("PASS: Database views and service loaders completed without errors.")
    print("No approvals, cases, files, meter readings or models were created.")
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
        rows = [self.example(f"MOD100{i}", 52 + i, "pending")
                for i in range(1, 6)]
        rows.append(self.example("MOD1006"))
        summary = analyse_cases(rows)
        self.assertEqual(summary["cases"], 6)
        self.assertEqual(summary["documents"], 5)
        self.assertEqual(summary["cases_without_pdf"], 1)
        self.assertEqual(summary["review_statuses"]["pending"], 5)
        self.assertEqual(summary["review_statuses"]["no_pdf"], 1)

    def test_multiple_pdfs_are_not_multiple_cases(self):
        summary = analyse_cases([
            self.example("MOD1", 10, "approved"),
            self.example("MOD1", 11, "pending"),
        ])
        self.assertEqual(summary["cases"], 1)
        self.assertEqual(summary["documents"], 2)

    def test_complete_approved_roles(self):
        complete, issues = validate_approved_roles([
            (53, "existing", 1), (53, "proposed", 1),
        ])
        self.assertEqual(complete, 1)
        self.assertFalse(issues)

    def test_no_approved_reports_is_valid(self):
        self.assertEqual(validate_approved_roles([]), (0, []))

    def test_partial_approved_report_is_flagged(self):
        complete, issues = validate_approved_roles([(53, "existing", 1)])
        self.assertEqual(complete, 0)
        self.assertEqual(len(issues), 1)

    def test_duplicate_approved_role_is_flagged(self):
        complete, issues = validate_approved_roles([
            (53, "existing", 2), (53, "proposed", 1),
        ])
        self.assertEqual(complete, 0)
        self.assertEqual(len(issues), 1)

    def test_ai_readiness_threshold(self):
        self.assertFalse(training_ready(0, 10))
        self.assertFalse(training_ready(9, 10))
        self.assertTrue(training_ready(10, 10))


def main():
    if "--self-test" in sys.argv:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(_SelfTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        return 0 if result.wasSuccessful() else 1
    return run_live()


if __name__ == "__main__":
    raise SystemExit(main())
