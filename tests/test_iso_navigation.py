"""Keep ISO Excel available without resurrecting retired pages."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestNavigation(unittest.TestCase):
    def test_single_excel_upload_page(self):
        navigation = ROOT / "streamlit_app.py"
        if not navigation.is_file():
            self.skipTest("The package does not overwrite customised navigation")
        text = navigation.read_text()
        self.assertIn('"enercal/enercal_measurements.py"', text)
        self.assertNotIn('"enercal/iso_assessment.py"', text)
        self.assertNotIn('"enercal/enercal_measurement_pairs.py"', text)

    def test_no_old_pair_imports_in_new_page(self):
        text = (ROOT / "enercal/enercal_measurements.py").read_text()
        self.assertNotIn("process_iso_pair", text)
        self.assertNotIn("read_and_merge_iso_assessment", text)
        self.assertIn('type=["xlsx"]', text)


if __name__ == "__main__":
    unittest.main()
