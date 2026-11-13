
import hashlib
import unittest
from unittest.mock import patch

from services.iso_pair_validator import (
    index_values,
    normalise_lift_id,
    validate_same_lift,
)
from services.iso_import_service import process_iso_pair


def sample_records():
    # Sample OP13 data from the EE PDF and ISO Excel.
    pdf = [
        {"field": "lift_id", "value": "OP13 (L)"},
        {"field": "serial_number", "value": "42954853"},
        {"field": "rated_load_kg", "value": 1635},
        {"field": "rated_speed_m_s", "value": 7},
        {"field": "travel_m", "value": 198.1},
        {"field": "stops", "value": 19},
        {"field": "reference_energy_wh", "value": 250.541},
        {"field": "annual_energy_kwh", "value": 22084.08},
    ]

    excel = [
        {"field": "lift_id", "value": "OP13(L)"},
        {"field": "rated_load_kg", "value": 1635},
        {"field": "rated_speed_m_s", "value": 7},
        {"field": "travel_m", "value": 198.1},
        {"field": "stops", "value": 19},
        {"field": "reference_energy_wh", "value": 250.541},
        {"field": "annual_energy_kwh", "value": 22084.07},
        {"field": "daily_trips_iso_assumed", "value": 750},
    ]

    return pdf, excel


class TestPairValidator(unittest.TestCase):

    def setUp(self):
        self.pdf, self.excel = sample_records()

    def test_lift_id_normalisation(self):
        self.assertEqual(
            normalise_lift_id("OP13 (L)"),
            normalise_lift_id("OP13(L)"),
        )

    def test_valid_pair(self):
        result = validate_same_lift(self.pdf, self.excel)

        self.assertEqual(result["lift_id"], "OP13 (L)")
        self.assertEqual(len(result["checked_specs"]), 4)

    def test_wrong_lift_rejected(self):
        self.excel[0]["value"] = "OP21"

        with self.assertRaisesRegex(ValueError, "Lift ID mismatch"):
            validate_same_lift(self.pdf, self.excel)

    def test_wrong_specification_rejected(self):
        self.excel[2]["value"] = 5

        with self.assertRaisesRegex(ValueError, "rated_speed_m_s"):
            validate_same_lift(self.pdf, self.excel)

    def test_serial_number_mismatch_rejected(self):
        self.excel.append({
            "field": "serial_number",
            "value": "99999999",
        })

        with self.assertRaisesRegex(ValueError, "serial numbers"):
            validate_same_lift(self.pdf, self.excel)

    def test_insufficient_identity_data_rejected(self):
        self.excel = [
            row for row in self.excel
            if row["field"] in (
                "lift_id",
                "rated_load_kg",
                "rated_speed_m_s",
            )
        ]

        with self.assertRaisesRegex(ValueError, "three matching"):
            validate_same_lift(self.pdf, self.excel)

    def test_rounding_difference_accepted(self):
        result = validate_same_lift(self.pdf, self.excel)

        differences = {
            row["field"] for row in result["differences"]
        }

        self.assertNotIn("annual_energy_kwh", differences)

    def test_large_energy_difference_recorded(self):
        self.excel[6]["value"] = 20000

        result = validate_same_lift(self.pdf, self.excel)

        differences = {
            row["field"] for row in result["differences"]
        }

        self.assertIn("annual_energy_kwh", differences)

    def test_unique_fields_preserved(self):
        result = validate_same_lift(self.pdf, self.excel)

        self.assertIn("serial_number", result["pdf_only"])
        self.assertIn(
            "daily_trips_iso_assumed",
            result["excel_only"],
        )

    def test_duplicate_field_rejected(self):
        self.pdf.append({
            "field": "lift_id",
            "value": "OP13",
        })

        with self.assertRaisesRegex(ValueError, "Duplicate"):
            index_values(self.pdf)


class TestImportService(unittest.TestCase):

    def setUp(self):
        self.pdf_records, self.excel_records = sample_records()

        # Mock extraction and database functions.
        targets = {
            "pdf": (
                "services.iso_import_service.extract_ee_pdf",
                self.pdf_records,
            ),
            "excel": (
                "services.iso_import_service.extract_iso_excel",
                self.excel_records,
            ),
            "case": (
                "services.iso_import_service.get_case_elevator",
                (1, "OP13 (L)"),
            ),
            "save": (
                "services.iso_import_service.save_iso_assessment",
                {"assessment_id": 10, "created": True},
            ),
        }

        self.mocks = {}

        for name, (target, result) in targets.items():
            patcher = patch(target, return_value=result)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def import_pair(self, pdf_bytes=b"%PDF-1.7", excel_bytes=b"PK\x03\x04"):
        # Run the normal import workflow using test files.
        return process_iso_pair(
            "CASE-001",
            "OP13_EE.pdf",
            pdf_bytes,
            "OP13_ISO.xlsx",
            excel_bytes,
        )

    def test_successful_import(self):
        result = self.import_pair()

        self.assertTrue(result["created"])
        self.assertEqual(result["assessment_id"], 10)
        self.assertEqual(result["lift_id"], "OP13 (L)")
        self.mocks["save"].assert_called_once()

    def test_all_fields_sent_to_repository(self):
        self.import_pair()

        args = self.mocks["save"].call_args.args

        self.assertEqual(args[3][0], self.pdf_records)
        self.assertEqual(args[3][1], self.excel_records)

    def test_original_file_hashes(self):
        self.import_pair()

        files = self.mocks["save"].call_args.args[2]

        self.assertEqual(
            files[0]["sha256"],
            hashlib.sha256(b"%PDF-1.7").hexdigest(),
        )
        self.assertEqual(
            files[1]["sha256"],
            hashlib.sha256(b"PK\x03\x04").hexdigest(),
        )

    def test_wrong_case_rejected(self):
        self.mocks["case"].return_value = (1, "OP21")

        with self.assertRaisesRegex(ValueError, "Selected case"):
            self.import_pair()

        self.mocks["save"].assert_not_called()

    def test_invalid_pdf_rejected(self):
        with self.assertRaisesRegex(ValueError, "Upload an EE PDF"):
            self.import_pair(pdf_bytes=b"invalid")

        self.mocks["save"].assert_not_called()

    def test_invalid_excel_rejected(self):
        with self.assertRaisesRegex(ValueError, "Upload an EE PDF"):
            self.import_pair(excel_bytes=b"invalid")

        self.mocks["save"].assert_not_called()

    def test_oversized_file_rejected(self):
        with patch(
            "services.iso_import_service.MAX_FILE_BYTES",
            8,
        ):
            with self.assertRaisesRegex(ValueError, "15 MB"):
                self.import_pair(pdf_bytes=b"%PDF-123456789")

        self.mocks["save"].assert_not_called()

    def test_duplicate_import_response(self):
        self.mocks["save"].return_value = {
            "assessment_id": 10,
            "created": False,
            "is_latest": True,
        }

        result = self.import_pair()

        self.assertFalse(result["created"])
        self.assertEqual(result["assessment_id"], 10)

    def test_extraction_failure_prevents_save(self):
        self.mocks["pdf"].side_effect = ValueError(
            "Unsupported EE PDF"
        )

        with self.assertRaisesRegex(ValueError, "Unsupported EE PDF"):
            self.import_pair()

        self.mocks["save"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
