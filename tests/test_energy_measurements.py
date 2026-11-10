
"""Tests for ISO measurement extraction and repository validation."""

import hashlib
import os
import unittest
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pymupdf
from openpyxl import Workbook

from services import energy_measurement_extractor as extractor
from services import energy_measurement_repository as repository


def sample_workbook(reference="OP21"):
    # Build a small workbook with the same source cell layout.
    book = Workbook()
    book.remove(book.active)
    for sheet in {field[0] for field in extractor.EXCEL_FIELDS}:
        book.create_sheet(sheet)
    book["Lift parameters"]["C2"] = reference
    for sheet, cell, *_ in extractor.EXCEL_FIELDS:
        book[sheet][cell] = 10.25

    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()


def find_value(observations, metric):
    return next(row for row in observations if row["metric_name"] == metric)


class TestExtraction(unittest.TestCase):
    def test_excel_extracts_expected_fields(self):
        result = extractor.extract_measurements(sample_workbook(), "OP21.xlsx")
        self.assertEqual(result["source_type"], "iso_excel")
        self.assertEqual(result["reported_elevator_ref"], "OP21")
        self.assertEqual(len(result["observations"]), 24)

        idle = find_value(result["observations"], "idle_power")
        self.assertEqual(idle["numeric_value"], Decimal("10.25"))
        self.assertEqual(idle["value_kind"], "test_measurement")
        self.assertEqual(idle["source_location"], "Data measured!C13")

        annual = find_value(result["observations"], "annual_energy")
        self.assertEqual(
            (annual["unit"], annual["value_kind"]),
            ("Wh/year", "iso_estimate"),
        )

    def test_excel_requires_valid_reference(self):
        with self.assertRaisesRegex(ValueError, "Elevator reference"):
            extractor.extract_measurements(sample_workbook("OTHER"), "test.xlsx")

    def test_rejects_unsupported_or_invalid_files(self):
        for filename, contents in (
            ("test.csv", b"data"),
            ("test.xlsx", b"not a workbook"),
            ("test.pdf", b"not a pdf"),
            ("test.pdf", b""),
        ):
            with self.subTest(filename=filename, size=len(contents)):
                with self.assertRaises(ValueError):
                    extractor.extract_measurements(contents, filename)

    def test_rejects_pdf_with_wrong_page_count(self):
        with pymupdf.open() as pdf:
            pdf.new_page()
            contents = pdf.tobytes()

        with self.assertRaisesRegex(ValueError, "6 page"):
            extractor.extract_measurements(contents, "test.pdf")

    def test_rejects_invalid_numeric_values(self):
        for invalid in (-1, "NaN", "Infinity", True, "abc"):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    extractor._number(invalid)

    def test_file_size_limit(self):
        with self.assertRaisesRegex(ValueError, "15 MB"):
            extractor.extract_measurements(
                b"x" * (extractor.MAX_SIZE + 1),
                "test.xlsx",
            )

    def test_original_excel_when_provided(self):
        path = os.getenv("MEASUREMENT_XLSX")
        if not path:
            self.skipTest("Set MEASUREMENT_XLSX to test the original workbook")

        source = Path(path)
        result = extractor.extract_measurements(source.read_bytes(), source.name)

        self.assertEqual(result["reported_elevator_ref"], "OP21")
        self.assertEqual(len(result["observations"]), 24)

        idle = find_value(result["observations"], "idle_power")
        self.assertEqual(idle["numeric_value"], Decimal("361.18"))

    def test_original_pdf_when_provided(self):
        path = os.getenv("MEASUREMENT_PDF")
        if not path:
            self.skipTest("Set MEASUREMENT_PDF to test the original PDF")

        source = Path(path)
        result = extractor.extract_measurements(source.read_bytes(), source.name)

        self.assertEqual(result["reported_elevator_ref"], "OP13")
        self.assertEqual(len(result["observations"]), 23)
        self.assertEqual(
            find_value(result["observations"], "idle_power")["numeric_value"],
            Decimal("678.39"),
        )
        self.assertEqual(
            find_value(result["observations"], "annual_energy")["value_kind"],
            "iso_estimate",
        )


class TestRepository(unittest.TestCase):
    def setUp(self):
        # Replace PostgreSQL with a cursor that records queries and results.
        self.cursor = MagicMock()
        self.connection = MagicMock()
        self.connection.cursor.return_value.__enter__.return_value = self.cursor

        patcher = patch.object(repository, "get_connection")
        self.connect = patcher.start()
        self.addCleanup(patcher.stop)
        self.connect.return_value.__enter__.return_value = self.connection

        self.file_bytes = sample_workbook()
        self.data = extractor.extract_measurements(
            self.file_bytes, "OP21.xlsx"
        )

    def test_find_existing_source_by_sha256(self):
        self.cursor.fetchone.return_value = (7,)

        self.assertEqual(
            repository.find_existing_measurement(self.file_bytes), 7
        )

        checksum = hashlib.sha256(self.file_bytes).hexdigest()
        self.assertEqual(
            self.cursor.execute.call_args.args[1],
            (checksum,),
        )

    def test_save_one_source_and_all_observations(self):
        self.cursor.fetchone.return_value = (42,)

        result = repository.save_measurement_source(
            "OP21.xlsx",
            self.file_bytes,
            extracted=self.data,
        )

        self.assertEqual(
            result,
            {"source_id": 42, "observations_saved": 24},
        )
        self.cursor.executemany.assert_called_once()

        rows = self.cursor.executemany.call_args.args[1]
        self.assertEqual(len(rows), 24)
        self.assertEqual({row[0] for row in rows}, {42})
        self.assertIn(
            "ON CONFLICT",
            self.cursor.execute.call_args.args[0],
        )

    def test_duplicate_file_is_rejected(self):
        self.cursor.fetchone.return_value = None

        with self.assertRaisesRegex(ValueError, "already saved"):
            repository.save_measurement_source(
                "OP21.xlsx",
                self.file_bytes,
                extracted=self.data,
            )

        self.cursor.executemany.assert_not_called()

    def test_duplicate_observation_is_rejected_before_insert(self):
        bad_data = dict(self.data)
        bad_data["observations"] = self.data["observations"] * 2

        with self.assertRaisesRegex(ValueError, "Repeated observation"):
            repository.save_measurement_source(
                "OP21.xlsx",
                self.file_bytes,
                extracted=bad_data,
            )

        self.cursor.execute.assert_not_called()

    def test_negative_value_is_rejected_before_insert(self):
        bad_data = dict(self.data)
        bad_data["observations"] = [
            dict(row) for row in self.data["observations"]
        ]
        bad_data["observations"][0]["numeric_value"] = Decimal("-3")

        with self.assertRaisesRegex(ValueError, "Invalid measurement"):
            repository.save_measurement_source(
                "OP21.xlsx",
                self.file_bytes,
                extracted=bad_data,
            )

        self.cursor.execute.assert_not_called()

    def test_approve_without_matching_elevator(self):
        self.cursor.fetchone.return_value = ("OP21", "pending")

        self.assertEqual(
            repository.review_measurement_source(42, "approved"),
            "approved",
        )
        self.assertEqual(self.cursor.execute.call_count, 2)
        self.assertIn(
            "FOR UPDATE",
            self.cursor.execute.call_args_list[0].args[0],
        )
        self.assertIsNone(
            self.cursor.execute.call_args_list[1].args[1][2]
        )

    def test_approve_with_matching_elevator(self):
        self.cursor.fetchone.side_effect = [
            ("OP21", "pending"),
            ("OP21",),
        ]

        repository.review_measurement_source(
            42, "approved", elevator_id=9
        )

        self.assertEqual(self.cursor.execute.call_count, 3)
        self.assertEqual(
            self.cursor.execute.call_args.args[1][2],
            9,
        )

    def test_reject_wrong_elevator_link(self):
        self.cursor.fetchone.side_effect = [
            ("OP21", "pending"),
            ("OP13",),
        ]

        with self.assertRaisesRegex(ValueError, "does not match"):
            repository.review_measurement_source(
                42, "approved", elevator_id=9
            )

        self.assertEqual(self.cursor.execute.call_count, 2)

    def test_rejection_requires_note(self):
        with self.assertRaisesRegex(ValueError, "rejected"):
            repository.review_measurement_source(42, "rejected")

        self.connect.assert_not_called()

    def test_prevent_repeat_approval(self):
        self.cursor.fetchone.return_value = ("OP21", "approved")

        with self.assertRaisesRegex(ValueError, "Only pending"):
            repository.review_measurement_source(42, "approved")

        self.assertEqual(self.cursor.execute.call_count, 1)

    def test_later_link_to_matching_elevator(self):
        self.cursor.fetchone.side_effect = [
            ("OP21", "approved", None),
            ("OP21",),
        ]

        self.assertEqual(
            repository.link_approved_measurement(42, 9),
            9,
        )
        self.assertEqual(self.cursor.execute.call_count, 3)

    def test_prevent_linking_unapproved_source(self):
        self.cursor.fetchone.return_value = ("OP21", "pending", None)

        with self.assertRaisesRegex(ValueError, "approved"):
            repository.link_approved_measurement(42, 9)

        self.assertEqual(self.cursor.execute.call_count, 1)


if __name__ == "__main__":
    unittest.main()
