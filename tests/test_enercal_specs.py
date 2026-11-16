
import unittest

import pymupdf

from services.enercal_specs import extract_specs


def synthetic_report():
    # Generate an alternative PDF layout for testing.
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=820, height=610)

        for x, y, text in [
            (20, 35, "Engineering inputs"),
            (380, 65, "As modernization technology"),
            (640, 65, "As existing technology"),
            (20, 100, "Rated load [kg]"),
            (420, 100, "1800"),
            (690, 100, "1600"),
            (20, 135, "Rated speed [m/s]"),
            (420, 135, "1.5"),
            (690, 135, "1"),
            (20, 170, "Starts per year"),
            (520, 170, "109500"),
            (20, 205, "Travel distance [m]"),
            (520, 205, "5.25"),
            (20, 240, "Regenerative drive"),
            (420, 240, "Yes"),
            (690, 240, "No"),
        ]:
            page.insert_text((x, y), text)

        page = pdf.new_page(width=820, height=610)

        for x, y, text in [
            (20, 80, "Control system"),
            (200, 80, "KCE"),
            (340, 80, "Group Control"),
            (600, 80, "Simplex"),
            (20, 115, "Display Types"),
            (200, 115, "Dot Matrix"),
            (340, 115, "Car width [mm]"),
            (600, 115, "1500"),
            (20, 150, "Number of landings"),
            (200, 150, "2"),
            (340, 150, "Running time [h / operating day]"),
            (600, 150, "1.79"),
        ]:
            page.insert_text((x, y), text)

        return pdf.tobytes()


class ExtractionTest(unittest.TestCase):

    def test_shifted_layout_and_alias(self):
        values = {
            key: value
            for key, value, _, _ in extract_specs(synthetic_report())
        }

        self.assertEqual(values["proposed_nominal_load_kg"], "1800")
        self.assertEqual(values["existing_nominal_load_kg"], "1600")
        self.assertEqual(values["proposed_nominal_speed_m_s"], "1.5")
        self.assertEqual(values["existing_regenerative_drive"], "No")
        self.assertEqual(values["proposed_regenerative_drive"], "Yes")
        self.assertEqual(values["running_hours_per_day"], "1.79")
        self.assertEqual(values["group_control"], "Simplex")
        self.assertEqual(values["control_system"], "KCE")

    def test_unresolved_does_not_guess(self):
        # Reject insufficient evidence instead of inventing values.
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 50), "Starts per year")
            page.insert_text((300, 50), "109500")

            self.assertEqual(extract_specs(pdf.tobytes()), [])


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
