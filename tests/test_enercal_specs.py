import unittest

import pymupdf

from services.enercal_specs import _normalise, extract_specs


class EnercalSpecificationsTests(unittest.TestCase):
    def test_label_normalisation(self):
        self.assertEqual(_normalise("Nominal Load [kg]"), "nominal load kg")

    def test_synthetic_report(self):
        # Use a generated PDF so tests do not depend on company files.
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=960, height=540)
            page.insert_text((25, 40), "Summary of Input Data")
            page.insert_text((25, 100), "Starts per year")
            page.insert_text((400, 100), "109500")
            page.insert_text((25, 130), "Travel distance [m]")
            page.insert_text((400, 130), "5.25")
            page.insert_text((25, 160), "Regenerative drive")
            page.insert_text((400, 160), "No")
            page.insert_text((690, 160), "Yes")

            page = pdf.new_page(width=960, height=540)
            page.insert_text((25, 40), "Summary of Input Data")
            page.insert_text((340, 100), "Running time [h / operating day]")
            page.insert_text((520, 100), "1.79")
            data = pdf.tobytes()

        values = {name: value for name, value, _, _ in extract_specs(data)}
        self.assertEqual(values["starts_per_year"], "109500")
        self.assertEqual(values["travel_distance_m"], "5.25")
        self.assertEqual(values["existing_regenerative_drive"], "No")
        self.assertEqual(values["proposed_regenerative_drive"], "Yes")
        self.assertEqual(values["running_hours_per_day"], "1.79")


if __name__ == "__main__":
    unittest.main()
