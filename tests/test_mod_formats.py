
"""Regression tests for the two supported 12-page EnerCal MOD PDFs."""

import os
import unittest
from decimal import Decimal
from pathlib import Path

import pymupdf

from services.enercal_extractor import COMPARISON_FIELDS, extract_mod_document
from services.enercal_specs import extract_specs


# Read the PDFs from sample_data unless another directory is configured
SAMPLE_DIR = Path(os.environ.get(
    "MOD_SAMPLE_DIR", Path(__file__).resolve().parents[1] / "sample_data"
))

# Expected values from both November 2026 MOD reports
CASES = {
    "EnerCal_MOD_11_3_2026.pdf": {
        "country": "China Mainland",
        "date": "2026-11-03",
        "price": "0.11",
        "existing_cost": 464,
        "proposed_cost": 219,
        "existing_lifetime": 14862,
        "proposed_lifetime": 7015,
        "existing_co2e": 65943,
        "proposed_co2e": 31187,
    },
    "EnerCal_MOD_05_11_2026.pdf": {
        "country": "Singapore",
        "date": "2026-11-05",
        "price": "0.27",
        "existing_cost": 1138,
        "proposed_cost": 538,
        "existing_lifetime": 36450,
        "proposed_lifetime": 17232,
        "existing_co2e": 40661,
        "proposed_co2e": 19230,
    },
}


class TestModFormats(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.reports = {}

        for filename in CASES:
            path = SAMPLE_DIR / filename

            if not path.is_file():
                raise FileNotFoundError(
                    f"Missing test PDF: {path}"
                )

            pdf_bytes = path.read_bytes()

            cls.reports[filename] = (
                extract_mod_document(pdf_bytes),
                extract_specs(pdf_bytes),
            )

    def test_report_structure(self):
        for filename, (document, specs) in self.reports.items():
            with self.subTest(pdf=filename):
                self.assertEqual(len(document["pages"]), 12)
                self.assertEqual(
                    document["comparison"]["source_page"], 4
                )
                self.assertEqual(
                    [page["page_number"] for page in document["pages"]],
                    list(range(1, 13)),
                )
                self.assertEqual(
                    document["comparison"]["currency_symbol"], "$"
                )
                self.assertEqual(
                    document["comparison"]["lifetime_years"], 25
                )
                self.assertEqual(len(specs), 51)

    def test_page_four_comparison(self):
        for filename, (document, _) in self.reports.items():
            with self.subTest(pdf=filename):
                expected = CASES[filename]
                comparison = document["comparison"]

                existing = comparison["rows"]["existing"]
                proposed = comparison["rows"]["proposed"]

                self.assertEqual(
                    set(existing) - {"elevator_technology"},
                    set(COMPARISON_FIELDS),
                )
                self.assertEqual(
                    set(proposed) - {"elevator_technology"},
                    set(COMPARISON_FIELDS),
                )

                self.assertEqual(
                    existing["elevator_technology"],
                    "Typical Traction Elevator",
                )
                self.assertEqual(
                    proposed["elevator_technology"],
                    "ReGenerate 400 DX",
                )

                self.assertEqual(
                    existing["annual_energy_consumption"], 4213
                )
                self.assertEqual(
                    proposed["annual_energy_consumption"], 1993
                )

                self.assertIsNone(
                    existing["annual_regenerative_energy"]
                )
                self.assertEqual(
                    proposed["annual_regenerative_energy"], 450
                )

                self.assertIsNone(existing["embodied_co2e"])
                self.assertEqual(proposed["embodied_co2e"], 0)

                self.assertEqual(
                    existing["annual_electricity_expenditure"],
                    expected["existing_cost"],
                )
                self.assertEqual(
                    proposed["annual_electricity_expenditure"],
                    expected["proposed_cost"],
                )

                self.assertEqual(
                    existing["lifetime_electricity_expenditure"],
                    expected["existing_lifetime"],
                )
                self.assertEqual(
                    proposed["lifetime_electricity_expenditure"],
                    expected["proposed_lifetime"],
                )

                self.assertEqual(
                    existing["operational_co2e"],
                    expected["existing_co2e"],
                )
                self.assertEqual(
                    proposed["operational_co2e"],
                    expected["proposed_co2e"],
                )

                self.assertEqual(
                    comparison["reported_energy_saving_percent"],
                    Decimal("53"),
                )
                self.assertEqual(
                    comparison["calculated_energy_saving_percent"],
                    Decimal("52.69"),
                )

    def test_unique_specifications(self):
        for filename, (_, specifications) in self.reports.items():
            with self.subTest(pdf=filename):
                expected = CASES[filename]

                fields = {
                    key: (value, unit, page)
                    for key, value, unit, page in specifications
                }

                self.assertEqual(
                    len(fields), len(specifications)
                )
                self.assertTrue(
                    all(
                        value.strip()
                        for value, _, _ in fields.values()
                    )
                )
                self.assertTrue(
                    all(
                        page in {2, 5, 8}
                        for _, _, page in fields.values()
                    )
                )
                self.assertFalse(
                    any(key.startswith("check_") for key in fields)
                )

                self.assertEqual(
                    fields["report_date"][0], expected["date"]
                )
                self.assertEqual(
                    fields["site"][0], expected["country"]
                )
                self.assertEqual(
                    fields["country"][0], expected["country"]
                )

                self.assertEqual(
                    fields["electricity_price"],
                    (expected["price"], "$/kWh", 8),
                )
                self.assertEqual(
                    fields["lifetime_years"],
                    ("25", "years", 8),
                )

                self.assertEqual(
                    fields["existing_nominal_speed_m_s"][0], "1"
                )
                self.assertEqual(
                    fields["proposed_nominal_speed_m_s"][0], "1.6"
                )

                self.assertEqual(
                    fields["existing_gear"][0], "Geared"
                )
                self.assertEqual(
                    fields["proposed_gear"][0], "Gearless"
                )

                self.assertEqual(
                    fields["p5_energy_without_regen_kwh"][0],
                    "2443",
                )
                self.assertEqual(
                    fields["p5_regen_saving_percent"][0],
                    "18%",
                )

    def test_unsupported_pdf_is_rejected(self):
        with pymupdf.open() as pdf:
            pdf.new_page(width=792, height=632)
            invalid_pdf = pdf.tobytes()

        with self.assertRaisesRegex(ValueError, "12-page"):
            extract_mod_document(invalid_pdf)

        with self.assertRaisesRegex(ValueError, "12-page"):
            extract_specs(invalid_pdf)


if __name__ == "__main__":
    unittest.main()
