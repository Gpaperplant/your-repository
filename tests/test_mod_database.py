
"""Optional rollback-only PostgreSQL tests for the 12-page EnerCal MOD workflow."""

import os
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4


SAMPLE_DIR = Path(os.environ.get(
    "MOD_SAMPLE_DIR", Path(__file__).resolve().parents[1] / "sample_data"
))

PDF_NAMES = (
    "EnerCal_MOD_11_3_2026.pdf",
    "EnerCal_MOD_05_11_2026.pdf",
)


class _UncommittedConnection:
    # Share one transaction without committing test records
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exception_type, exception, traceback):
        return False


@unittest.skipUnless(
    os.getenv("RUN_MOD_DB_TESTS") == "1",
    "Set RUN_MOD_DB_TESTS=1 to run live PostgreSQL tests.",
)
class TestModDatabase(unittest.TestCase):

    def setUp(self):
        from database import get_connection
        from services import enercal_repository, enercal_review_repository

        self.saving = enercal_repository
        self.review = enercal_review_repository
        self.conn = get_connection()

        self.addCleanup(self._rollback_and_close)

        if self.conn.autocommit:
            raise RuntimeError(
                "Database tests require autocommit=False for rollback."
            )

        # Prevent repository context managers from committing
        connection = _UncommittedConnection(self.conn)

        for module in (self.saving, self.review):
            mocked = patch.object(
                module,
                "get_connection",
                return_value=connection,
            )
            mocked.start()
            self.addCleanup(mocked.stop)

        # Register an isolated test elevator and case
        tag = uuid4().hex[:16]
        self.case_ref = f"TEST-MOD-DB-{tag}"

        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO elevators (elevator_ref)
                VALUES (%s)
                RETURNING elevator_id
                """,
                (f"TEST-ELEV-{tag}",),
            )

            elevator_id = cur.fetchone()[0]

            cur.execute(
                """
                INSERT INTO modernisation_cases
                    (case_ref, elevator_id)
                VALUES (%s, %s)
                """,
                (self.case_ref, elevator_id),
            )

    def _rollback_and_close(self):
        # Remove all test records when the test finishes
        self.conn.rollback()
        self.conn.close()

    def _upload(self, filename):
        path = SAMPLE_DIR / filename

        if not path.is_file():
            self.fail(f"Required sample PDF is missing: {path}")

        return self.saving.save_complete_enercal_report(
            self.case_ref,
            filename,
            path.read_bytes(),
        )

    def _approved_rows(self):
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    document_id,
                    technology_role,
                    annual_energy_kwh_year,
                    annual_electricity_cost_per_year,
                    currency_symbol,
                    lifetime_years,
                    country,
                    electricity_price_per_kwh,
                    revision_no,
                    calculated_energy_saving_percent
                FROM enercal_approved_kpis_view
                WHERE case_ref = %s
                ORDER BY document_id, technology_role
                """,
                (self.case_ref,),
            )

            return cur.fetchall()

    def test_upload_and_approval_for_both_reports(self):
        documents = [
            self._upload(filename)
            for filename in PDF_NAMES
        ]

        # Confirm both reports are fully saved
        for document in documents:
            self.assertEqual(document["pages_saved"], 12)
            self.assertEqual(document["fields_saved"], 16)
            self.assertEqual(document["additional_fields_saved"], 51)

        # Pending reports must not appear in approved reporting
        self.assertEqual(self._approved_rows(), [])

        for document in documents:
            document_id = document["document_id"]
            details = self.review.get_review_details(document_id)

            self.assertEqual(details["status"], "pending")
            self.assertEqual(len(details["candidates"]), 16)

            self.review.review_revision(document_id, "approved")

        rows = self._approved_rows()

        # Two approved reports with existing and proposed rows
        self.assertEqual(len(rows), 4)

        by_id = {
            document["document_id"]: []
            for document in documents
        }

        for row in rows:
            by_id[row[0]].append(row)

            self.assertEqual(row[4], "$")
            self.assertEqual(row[5], 25)
            self.assertEqual(row[9], Decimal("52.69"))

        china = by_id[documents[0]["document_id"]]
        singapore = by_id[documents[1]["document_id"]]

        # Verify the 3 November report
        self.assertEqual(
            {row[3] for row in china},
            {464, 219},
        )

        self.assertTrue(all(
            row[6] == "China Mainland"
            and row[7] == Decimal("0.11")
            for row in china
        ))

        # Verify the 5 November report
        self.assertEqual(
            {row[3] for row in singapore},
            {1138, 538},
        )

        self.assertTrue(all(
            row[6] == "Singapore"
            and row[7] == Decimal("0.27")
            for row in singapore
        ))

    def test_pending_correction_creates_one_new_revision(self):
        document_id = self._upload(PDF_NAMES[0])["document_id"]

        original = self.review.get_review_details(document_id)

        values = {
            row["field_name"]: row["raw_value"]
            for row in original["candidates"]
        }

        # Correct existing annual energy from 4213 to 4200
        values["existing_annual_energy_consumption"] = "4200"

        self.assertEqual(
            self.review.resubmit_revision(
                document_id,
                values,
                "Integration test",
            ),
            2,
        )

        updated = self.review.get_review_details(document_id)

        self.assertEqual(updated["revision_no"], 2)
        self.assertEqual(updated["status"], "pending")
        self.assertEqual(len(updated["candidates"]), 16)

        calculated = next(
            row["numeric_value"]
            for row in updated["candidates"]
            if row["field_name"] == "calculated_energy_saving_percent"
        )

        self.assertEqual(calculated, Decimal("52.55"))

        # Corrected data stays hidden until approval
        self.assertEqual(self._approved_rows(), [])

        self.review.review_revision(document_id, "approved")

        rows = self._approved_rows()

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[8] == 2 for row in rows))

        # Verify initial upload, correction and approval events
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM enercal_review_events e
                JOIN extraction_revisions r
                    ON r.revision_id = e.revision_id
                WHERE r.document_id = %s
                """,
                (document_id,),
            )

            self.assertEqual(cur.fetchone()[0], 4)

    def test_duplicate_pdf_is_rejected(self):
        document_id = self._upload(PDF_NAMES[0])["document_id"]

        # Match the current repository duplicate error message
        with self.assertRaisesRegex(ValueError, "already saved"):
            self._upload(PDF_NAMES[0])

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM source_documents
                WHERE document_id = %s
                """,
                (document_id,),
            )

            self.assertEqual(cur.fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
