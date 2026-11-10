
"""Optional live PostgreSQL tests for measurement review workflows."""

import os
import unittest
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch
from uuid import uuid4

from openpyxl import Workbook

from database import get_connection
from services import energy_measurement_extractor as extractor
from services import energy_measurement_repository as repository


@contextmanager
def _shared_transaction(connection):
    # Keep repository calls in one transaction without committing.
    yield connection


def _sample_workbook(elevator_ref):
    # Generate a valid workbook without using confidential source files.
    workbook = Workbook()
    workbook.remove(workbook.active)

    for sheet, *_ in extractor.EXCEL_FIELDS:
        if sheet not in workbook:
            workbook.create_sheet(sheet)

    workbook["Lift parameters"]["C2"] = elevator_ref
    for sheet, cell, *_ in extractor.EXCEL_FIELDS:
        workbook[sheet][cell] = 10.25

    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


class TestLiveMeasurementWorkflow(unittest.TestCase):
    def setUp(self):
        if os.getenv("RUN_MEASUREMENT_DB_TESTS") != "1":
            self.skipTest("Set RUN_MEASUREMENT_DB_TESTS=1 to use PostgreSQL")

        self.connection = get_connection()
        self.addCleanup(self.connection.close)
        self.addCleanup(self.connection.rollback)

        # Prevent repository context managers from committing test records.
        patcher = patch.object(
            repository,
            "get_connection",
            side_effect=lambda: _shared_transaction(self.connection),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('public.measurement_sources'), "
                "to_regclass('public.energy_observations')"
            )
            if None in cur.fetchone():
                self.fail("Run database migration 009 before this test")

    def _register_elevator(self, elevator_ref):
        with self.connection.cursor() as cur:
            cur.execute(
                "INSERT INTO elevators (elevator_ref) VALUES (%s) "
                "RETURNING elevator_id",
                (elevator_ref,),
            )
            return cur.fetchone()[0]

    def _save_source(self, elevator_ref):
        contents = _sample_workbook(elevator_ref)
        self.assertIsNone(repository.find_existing_measurement(contents))
        result = repository.save_measurement_source("test_iso.xlsx", contents)
        self.assertEqual(result["observations_saved"], 24)
        return result["source_id"], contents

    def test_upload_approve_and_link(self):
        reference = f"OP{uuid4().int}"
        correct_id = self._register_elevator(reference)
        wrong_id = self._register_elevator(f"OP{uuid4().int}")
        source_id, contents = self._save_source(reference)

        self.assertEqual(repository.find_existing_measurement(contents), source_id)
        with self.assertRaisesRegex(ValueError, "already saved"):
            repository.save_measurement_source("test_iso.xlsx", contents)

        details = repository.get_measurement_details(source_id)
        self.assertEqual(details["source"]["review_status"], "pending")
        self.assertEqual(len(details["observations"]), 24)

        with self.assertRaisesRegex(ValueError, "does not match"):
            repository.review_measurement_source(
                source_id, "approved", elevator_id=wrong_id
            )
        with self.assertRaisesRegex(ValueError, "must be approved"):
            repository.link_approved_measurement(source_id, correct_id)

        repository.review_measurement_source(source_id, "approved", "Checked source")
        self.assertEqual(repository.link_approved_measurement(source_id, correct_id), correct_id)

        verified = repository.get_measurement_details(source_id)["source"]
        self.assertEqual(verified["review_status"], "approved")
        self.assertEqual(verified["verified_elevator_ref"], reference)

        with self.assertRaisesRegex(ValueError, "Only pending"):
            repository.review_measurement_source(source_id, "approved")

    def test_upload_and_reject(self):
        source_id, _ = self._save_source(f"OP{uuid4().int}")

        with self.assertRaisesRegex(ValueError, "rejected"):
            repository.review_measurement_source(source_id, "rejected")

        repository.review_measurement_source(source_id, "rejected", "Unverified source")
        details = repository.get_measurement_details(source_id)
        self.assertEqual(details["source"]["review_status"], "rejected")
        self.assertIsNone(details["source"]["verified_elevator_id"])

        with self.assertRaisesRegex(ValueError, "must be approved"):
            repository.link_approved_measurement(source_id, 1)


if __name__ == "__main__":
    unittest.main()
