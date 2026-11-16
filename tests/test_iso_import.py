
import hashlib
import unittest
from unittest.mock import patch

from services.iso_import_service import process_iso_pair
from services.iso_pair_validator import (
    index_values,
    normalise_lift_id,
    validate_same_lift,
)
from services.iso_repository import save_iso_assessment


def sample_records():
    # Representative OP13 fields and permitted descriptive differences.
    pdf = [
        {"field": "lift_id", "value": "OP13 (L)", "kind": "identity"},
        {"field": "rated_load_kg", "value": 1635, "kind": "specification"},
        {"field": "rated_speed_m_s", "value": 7, "kind": "specification"},
        {"field": "travel_m", "value": 198.1, "kind": "specification"},
        {"field": "stops", "value": 19, "kind": "specification"},
        {"field": "test_date", "value": "2025-05-21", "kind": "metadata"},
        {"field": "reference_energy_wh", "value": 250.541, "kind": "measured"},
        {"field": "annual_energy_kwh", "value": 22084.08, "kind": "reported"},
        {
            "field": "lift_model",
            "value": "KONE MiniSpace (PT24/70-19)",
            "kind": "specification",
        },
        {"field": "instrument_model", "value": "Fluke 1738", "kind": "metadata"},
        {"field": "serial_number", "value": "42954853", "kind": "identity"},
    ]

    excel = [
        {"field": "lift_id", "value": "OP13(L)", "kind": "identity"},
        {"field": "rated_load_kg", "value": 1635, "kind": "specification"},
        {"field": "rated_speed_m_s", "value": 7, "kind": "specification"},
        {"field": "travel_m", "value": 198.1, "kind": "specification"},
        {"field": "stops", "value": 19, "kind": "specification"},
        {"field": "test_date", "value": "2025-05-21", "kind": "metadata"},
        {"field": "reference_energy_wh", "value": 250.541, "kind": "measured"},
        {"field": "annual_energy_kwh", "value": 22084.07, "kind": "calculated"},
        {
            "field": "lift_model",
            "value": "KONE MiniSpace, traction lift",
            "kind": "specification",
        },
        {
            "field": "instrument_model",
            "value": "Fluke 1736/1738",
            "kind": "metadata",
        },
        {"field": "daily_trips_iso_assumed", "value": 750, "kind": "assumed"},
    ]

    return pdf, excel


def change(records, field, value):
    next(
        row for row in records if row["field"] == field
    )["value"] = value


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
        change(self.excel, "lift_id", "OP21")

        with self.assertRaisesRegex(ValueError, "Lift ID mismatch"):
            validate_same_lift(self.pdf, self.excel)

    def test_wrong_speed_rejected(self):
        change(self.excel, "rated_speed_m_s", 5)

        with self.assertRaisesRegex(ValueError, "rated_speed_m_s"):
            validate_same_lift(self.pdf, self.excel)

    def test_insufficient_identity_rejected(self):
        self.excel = [
            row for row in self.excel
            if row["field"] in {"lift_id", "rated_load_kg"}
        ]

        with self.assertRaisesRegex(ValueError, "three matching"):
            validate_same_lift(self.pdf, self.excel)

    def test_rounding_accepted(self):
        result = validate_same_lift(self.pdf, self.excel)
        differences = {
            row["field"] for row in result["differences"]
        }
        self.assertNotIn("annual_energy_kwh", differences)

    def test_unique_fields_preserved(self):
        result = validate_same_lift(self.pdf, self.excel)
        self.assertIn("serial_number", result["pdf_only"])
        self.assertIn("daily_trips_iso_assumed", result["excel_only"])

    def test_duplicate_field_rejected(self):
        self.pdf.append(dict(self.pdf[0]))

        with self.assertRaisesRegex(ValueError, "Duplicate"):
            index_values(self.pdf)


class TestImportService(unittest.TestCase):
    def setUp(self):
        self.pdf_records, self.excel_records = sample_records()

        # Mock external extraction and database operations.
        targets = {
            "preview": (
                "services.iso_import_service.read_and_merge_iso_assessment",
                {
                    "review_status": "RECONCILED",
                    "elevator_id": "OP13 (L)",
                    "conflicting_fields": [],
                    "missing_fields": [],
                },
            ),
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

        for name, (target, value) in targets.items():
            patcher = patch(target, return_value=value)
            self.mocks[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def import_pair(
        self,
        pdf_bytes=b"%PDF-1.7",
        excel_bytes=b"PK\x03\x04",
    ):
        return process_iso_pair(
            "MOD1001",
            "OP13.pdf",
            pdf_bytes,
            "OP13.xlsx",
            excel_bytes,
        )

    def test_import_saves_both_sources(self):
        result = self.import_pair()
        self.assertTrue(result["created"])

        args = self.mocks["save"].call_args.args
        self.assertIs(args[3][0], self.pdf_records)
        self.assertIs(args[3][1], self.excel_records)

    def test_descriptive_differences_are_preserved(self):
        result = self.import_pair()

        self.assertEqual(
            {row["field"] for row in result["differences"]},
            {"lift_model", "instrument_model"},
        )

    def test_measured_conflict_is_rejected(self):
        change(self.excel_records, "reference_energy_wh", 240)

        with self.assertRaisesRegex(ValueError, "reference_energy_wh"):
            self.import_pair()

        self.mocks["save"].assert_not_called()

    def test_reported_energy_conflict_is_rejected(self):
        change(self.excel_records, "annual_energy_kwh", 20000)

        with self.assertRaisesRegex(ValueError, "annual_energy_kwh"):
            self.import_pair()

        self.mocks["save"].assert_not_called()

    def test_test_date_conflict_is_rejected(self):
        change(self.excel_records, "test_date", "2025-05-22")

        with self.assertRaisesRegex(ValueError, "test_date"):
            self.import_pair()

        self.mocks["save"].assert_not_called()

    def test_preview_rejection_blocks_save(self):
        self.mocks["preview"].return_value = {
            "review_status": "REVIEW_REQUIRED",
            "conflicting_fields": ["idle_power_w"],
            "missing_fields": [],
        }

        with self.assertRaisesRegex(ValueError, "idle_power_w"):
            self.import_pair()

        self.mocks["save"].assert_not_called()

    def test_preview_id_mismatch_is_rejected(self):
        self.mocks["preview"].return_value["elevator_id"] = "OP21"

        with self.assertRaisesRegex(ValueError, "different elevators"):
            self.import_pair()

        self.mocks["save"].assert_not_called()

    def test_wrong_case_rejected(self):
        self.mocks["case"].return_value = (1, "OP21")

        with self.assertRaisesRegex(ValueError, "Selected case"):
            self.import_pair()

        self.mocks["save"].assert_not_called()

    def test_placeholder_case_allowed(self):
        self.mocks["case"].return_value = (1, "TEST-LIFT-001")
        self.assertTrue(self.import_pair()["created"])

    def test_wrong_document_types_rejected(self):
        invalid_pairs = (
            (b"invalid", b"PK\x03\x04"),
            (b"%PDF-1.7", b"invalid"),
        )

        for pdf_bytes, excel_bytes in invalid_pairs:
            with self.assertRaisesRegex(ValueError, "Upload an EE PDF"):
                self.import_pair(pdf_bytes, excel_bytes)

        self.mocks["save"].assert_not_called()

    def test_file_size_limit(self):
        with patch("services.iso_import_service.MAX_FILE_BYTES", 8):
            with self.assertRaisesRegex(ValueError, "15 MB"):
                self.import_pair(pdf_bytes=b"%PDF-123456789")

        self.mocks["save"].assert_not_called()

    def test_duplicate_result(self):
        self.mocks["save"].return_value = {
            "assessment_id": 10,
            "created": False,
        }

        self.assertFalse(self.import_pair()["created"])

    def test_source_hashes(self):
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


class TestIsoRepository(unittest.TestCase):
    def test_different_lift_rejected_under_case_lock(self):
        # Simulate a locked case containing a different lift.
        pdf, excel = sample_records()

        files = [
            {
                "type": "ee_pdf",
                "sha256": "a" * 64,
                "filename": "OP13.pdf",
                "bytes": b"%PDF",
            },
            {
                "type": "iso_excel",
                "sha256": "b" * 64,
                "filename": "OP13.xlsx",
                "bytes": b"PK",
            },
        ]

        with patch(
            "services.iso_repository.get_connection"
        ) as connection:
            cursor = (
                connection.return_value.__enter__.return_value
                .cursor.return_value.__enter__.return_value
            )

            cursor.fetchone.side_effect = [
                (1,),
                (7, "OP21"),
            ]

            with self.assertRaisesRegex(
                ValueError,
                "already contains ISO data",
            ):
                save_iso_assessment(
                    1,
                    "OP13 (L)",
                    files,
                    (pdf, excel),
                    [],
                )

            self.assertEqual(cursor.execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
