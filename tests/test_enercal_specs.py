
import unittest

import pymupdf

from services.enercal_specs import _normalise, extract_specs


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

    def test_normalise(self):
        self.assertEqual(
            _normalise("Nominal Load [kg]"),
            "nominal load kg"
        )

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


if __name__ == "__main__":
    unittest.main()
