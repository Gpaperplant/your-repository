
import unittest

import pymupdf

from services.enercal_specs import extract_specs


def synthetic_report(with_values=True):
    with pymupdf.open() as pdf:
        for index in range(12):
            page = pdf.new_page(width=792, height=632)
            if index == 1:
                page.insert_text((20, 35), "General Information")
                page.insert_text((20, 120), "KONE Contact Information")
            elif index == 4:
                page.insert_text((20, 35), "Energy Savings")
            elif index == 7:
                page.insert_text((20, 35), "Summary of Input Data")
                if with_values:
                    entries = (
                        (100, "Nominal load [kg]", "1600", "1800"),
                        (135, "Nominal speed [m/s]", "1", "1.5"),
                        (170, "Regenerative drive", "No", "Yes"),
                        (205, "Starts per year", "109500", None),
                    )
                    for y, label, existing, proposed in entries:
                        page.insert_text((20, y), label)
                        page.insert_text((340, y), existing)
                        if proposed is not None:
                            page.insert_text((540, y), proposed)
        return pdf.tobytes()


class ExtractionTest(unittest.TestCase):

    def test_supported_engineering_fields(self):
        values = {
            key: value
            for key, value, _, _ in extract_specs(synthetic_report())
        }
        self.assertEqual(values["existing_nominal_load_kg"], "1600")
        self.assertEqual(values["proposed_nominal_load_kg"], "1800")
        self.assertEqual(values["existing_nominal_speed_m_s"], "1")
        self.assertEqual(values["proposed_nominal_speed_m_s"], "1.5")
        self.assertEqual(values["existing_regenerative_drive"], "No")
        self.assertEqual(values["proposed_regenerative_drive"], "Yes")
        self.assertEqual(values["starts_per_year"], "109500")

    def test_unresolved_does_not_guess(self):
        self.assertEqual(extract_specs(synthetic_report(with_values=False)), [])


class UploadCandidatesTest(unittest.TestCase):
    def test_comparison_preserves_sixteen_fields_and_missing_values(self):
        from decimal import Decimal
        from services.enercal_extractor import COMPARISON_FIELDS
        from services.enercal_repository import _comparison_candidates

        before = {field: Decimal("10") for field in COMPARISON_FIELDS}
        after = {field: Decimal("5") for field in COMPARISON_FIELDS}
        after["annual_regenerative_energy"] = None
        comparison = {
            "currency_symbol": "$",
            "lifetime_years": 25,
            "rows": {
                "existing": {"elevator_technology": "Old", **before},
                "proposed": {"elevator_technology": "New", **after},
            },
            "reported_energy_saving_percent": Decimal("50"),
            "calculated_energy_saving_percent": Decimal("50.00"),
        }

        rows = _comparison_candidates(comparison)
        self.assertEqual(len(rows), 16)
        self.assertEqual(len({row[0] for row in rows}), 16)
        self.assertEqual(rows[0][:2], ("existing_elevator_technology", "Old"))
        self.assertEqual(rows[7][:2], ("proposed_elevator_technology", "New"))
        self.assertEqual(rows[8][0], "proposed_annual_energy_consumption")
        self.assertEqual(rows[-1][0], "calculated_energy_saving_percent")
        missing = next(
            row for row in rows if row[0] == "proposed_annual_regenerative_energy"
        )
        self.assertEqual(missing[1:3], ("N/A", None))


if __name__ == "__main__":
    unittest.main()
