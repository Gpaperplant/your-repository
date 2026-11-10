
"""Read-only checks for missing, pending and approved EnerCal report states."""

import sys
import unittest
from collections import Counter


REQUIRED_RELATIONS = (
    "public.modernisation_cases",
    "public.source_documents",
    "public.extraction_revisions",
    "public.extraction_candidates",
    "public.enercal_comparison_view",
    "public.enercal_approved_comparison_view",
)


CASE_QUERY = """
SELECT
    m.case_ref,
    m.status AS case_status,
    d.document_id,
    d.filename,
    r.revision_no,
    r.review_status,
    COALESCE(fields.field_count, 0) AS field_count,
    COALESCE(fields.distinct_fields, 0) AS distinct_fields,
    COALESCE(preview.role_count, 0) AS preview_roles,
    COALESCE(approved.role_count, 0) AS approved_roles
FROM public.modernisation_cases m
LEFT JOIN public.source_documents d ON d.case_id = m.case_id
LEFT JOIN LATERAL (
    SELECT revision_id, revision_no, review_status
    FROM public.extraction_revisions
    WHERE document_id = d.document_id
    ORDER BY revision_no DESC
    LIMIT 1
) r ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(*) AS field_count,
           COUNT(DISTINCT field_name) AS distinct_fields
    FROM public.extraction_candidates
    WHERE extraction_revision_id = r.revision_id
) fields ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(DISTINCT technology_role) AS role_count
    FROM public.enercal_comparison_view
    WHERE document_id = d.document_id
) preview ON TRUE
LEFT JOIN LATERAL (
    SELECT COUNT(DISTINCT technology_role) AS role_count
    FROM public.enercal_approved_comparison_view
    WHERE document_id = d.document_id
) approved ON TRUE
ORDER BY m.case_ref, d.document_id
"""


def classify(row):
    # A missing file is normal, not a failed extraction.
    if row["document_id"] is None:
        return "NO_PDF", "Upload a PDF when the case is ready."

    if row["revision_no"] is None:
        return "MISSING_REVISION", "Investigate the saved PDF extraction."

    status = row["review_status"]
    fields_ok = (
        row["field_count"] == 16
        and row["distinct_fields"] == 16
        and row["preview_roles"] == 2
    )

    if status == "approved":
        if fields_ok and row["approved_roles"] == 2:
            return "APPROVED", "Available for approved KPI reporting."
        return "INVALID_APPROVAL", "Check approved view and revision consistency."

    if row["approved_roles"] != 0:
        return "UNAPPROVED_LEAK", "Unapproved document appears in the approved view."

    if status == "pending":
        if not fields_ok:
            return "INCOMPLETE", "Inspect the 16 extracted fields in Case Workspace."
        return "PENDING", "Review against the PDF in Case Workspace."

    if status == "changes_requested":
        return "CHANGES_REQUESTED", "Review and resubmit corrected extraction."

    if status == "further_verification_required":
        return "VERIFICATION_REQUIRED", "Complete engineering verification."

    if status == "rejected":
        return "REJECTED", "Keep rejected history out of KPI reporting."

    return "UNKNOWN_STATUS", "Inspect the latest extraction revision status."


def load_rows():
    # Keep every database statement inside a read-only transaction.
    from database import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
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
                raise RuntimeError("Missing relations: " + ", ".join(missing))

            cur.execute(CASE_QUERY)
            column_names = [item.name for item in cur.description]
            rows = [dict(zip(column_names, record)) for record in cur.fetchall()]

    return database_name, rows


def run_check():
    print("KONE EnerCal | Read-only report-state verification")
    try:
        database_name, rows = load_rows()
    except Exception as exc:
        print(f"FAIL: Could not check database: {exc}")
        return 1

    print(f"Database: {database_name} | Case/report rows: {len(rows)}")
    totals = Counter()
    errors = 0

    for row in rows:
        state, action = classify(row)
        totals[state] += 1
        doc = row["document_id"] if row["document_id"] is not None else "none"
        revision = row["revision_no"] if row["revision_no"] is not None else "none"
        print(f"\n{row['case_ref']} | PDF {doc} | Revision {revision} | {state}")
        print(f"  Extracted fields: {row['field_count']}/16 | Preview roles: {row['preview_roles']}/2")
        print(f"  Approved-view roles: {row['approved_roles']}/2 | Next: {action}")

        if state in ("INVALID_APPROVAL", "UNAPPROVED_LEAK", "UNKNOWN_STATUS"):
            errors += 1

    print("\nSummary:")
    for state, count in sorted(totals.items()):
        print(f"  {state}: {count}")

    if errors:
        print(f"FAIL: {errors} database approval consistency issue(s).")
        return 1

    print("PASS: Report states are consistent with approval-view eligibility.")
    print("NOTE: This checks database behavior, not Streamlit browser rendering.")
    return 0


class StateTests(unittest.TestCase):
    def base(self, **changes):
        row = {
            "document_id": 53,
            "revision_no": 1,
            "review_status": "pending",
            "field_count": 16,
            "distinct_fields": 16,
            "preview_roles": 2,
            "approved_roles": 0,
        }
        row.update(changes)
        return row

    def test_missing_pdf(self):
        self.assertEqual(classify(self.base(document_id=None))[0], "NO_PDF")

    def test_pending(self):
        self.assertEqual(classify(self.base())[0], "PENDING")

    def test_incomplete_extraction(self):
        self.assertEqual(classify(self.base(field_count=15))[0], "INCOMPLETE")

    def test_approved(self):
        row = self.base(review_status="approved", approved_roles=2)
        self.assertEqual(classify(row)[0], "APPROVED")

    def test_rejected(self):
        self.assertEqual(classify(self.base(review_status="rejected"))[0], "REJECTED")

    def test_pending_cannot_appear_in_approved_view(self):
        self.assertEqual(classify(self.base(approved_roles=2))[0], "UNAPPROVED_LEAK")

    def test_approved_requires_complete_comparison(self):
        row = self.base(review_status="approved", approved_roles=1)
        self.assertEqual(classify(row)[0], "INVALID_APPROVAL")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(StateTests)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)

    raise SystemExit(run_check())
