import unittest
from pathlib import Path

from services.enercal_specs import _normalise, extract_specs

PDF = Path(__file__).resolve().parents[1] / "sample_data" / "EnerCal_MOD_8_11_2026_test.pdf"


class EnercalSpecificationsTests(unittest.TestCase):
    def test_label_normalisation(self):
        self.assertEqual(_normalise("Nominal Load [kg]"), "nominal load kg")

    def test_sample_report(self):
        values = {name: value for name, value, _, _ in extract_specs(PDF.read_bytes())}
        self.assertEqual(len(values), 28)
        self.assertEqual(values["starts_per_year"], "109500")
        self.assertEqual(values["travel_distance_m"], "5.25")
        self.assertEqual(values["running_hours_per_day"], "1.79")
        self.assertEqual(values["existing_regenerative_drive"], "No")
        self.assertEqual(values["proposed_regenerative_drive"], "Yes")


if __name__ == "__main__":
    unittest.main()
