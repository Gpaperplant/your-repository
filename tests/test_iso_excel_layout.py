"""Test ISO Excel before/after extraction without requiring private project files."""

import unittest
import os
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from services.iso_excel_extractor import extract_iso_excel, _check_calculations


def workbook_bytes(*, dual=True, invalid_after=False, legacy=False):
    wb = Workbook()
    wb.active.title = "Lift parameters"
    for sheet in ("Test parameters", "Data measured", "Calculation", "Classification", "ISO Scenarios"):
        wb.create_sheet(sheet)
    lift = wb["Lift parameters"]
    for addr, val in {"C2": "OP13 (L)", "C5": 1635, "C6": 7, "C7": 198.1, "C13": 365}.items():
        lift[addr] = val
    data = wb["Data measured"]
    data["C3"] = "Input before"
    data["D3"] = "Input after" if dual else None
    data["C10"] = 490
    if dual:
        data["D10"] = 250.541
        for cell, value in {
            "C15": 10, "D15": 10,
            "C19": 484.5, "C20": 5.5,
            "D19": 247.2, "D20": 3.341,
        }.items():
            data[cell] = value
    calc = wb["Calculation"]
    calc["C1"] = "Input before"
    calc["D1"] = "Input after" if dual else None
    for cell, value in {"C6": 100, "C10": 50, "C11": 150}.items():
        calc[cell] = value
    if dual:
        for cell, value in {"D6": 70, "D10": 30, "D11": 100, "C13": 54.75, "D13": 36.5}.items():
            calc[cell] = value
    classification = wb["Classification"]
    if dual:
        for cell, value in {
            "C14": 150, "D14": 100, "C15": 54750,
            "D15": 35000 if invalid_after else 36500,
            "C16": 54.75, "D16": 36.5,
            "C17": "C", "D17": "A", "D18": 33.333,
        }.items():
            classification[cell] = value
    else:
        classification["C11"] = 150
        classification["C13"] = 54750
    if dual:
        scenario = wb["ISO Scenarios"]
        scenario["D4"] = "AFTER"
        scenario["C5"] = 511
        scenario["D5"] = 511
        for col in ("C", "D"):
            for row, value in {16: 45, 17: 19, 18: 36, 19: 100}.items():
                scenario[f"{col}{row}"] = value
    data = BytesIO()
    wb.save(data)
    wb.close()
    return data.getvalue()


class TestISOExcelLayouts(unittest.TestCase):
    def test_before_after_values_are_separate(self):
        rows = extract_iso_excel(workbook_bytes())
        values = {row["field"]: row["value"] for row in rows}
        self.assertEqual(len(values), len(rows))
        self.assertEqual(values["before_annual_energy_kwh"], 54.75)
        self.assertEqual(values["after_annual_energy_kwh"], 36.5)
        self.assertEqual(values["before_annual_energy_wh"], 54750)
        self.assertEqual(values["after_annual_energy_wh"], 36500)
        self.assertEqual(values["before_efficiency_class"], "C")
        self.assertEqual(values["after_efficiency_class"], "A")
        self.assertEqual(values["before_reference_energy_wh"], 490)
        self.assertEqual(values["after_reference_energy_wh"], 250.541)
        self.assertEqual(values["before_scenario_daily_trips"], 511)
        self.assertEqual(values["after_scenario_daily_trips"], 511)

    def test_after_annual_calculation_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, "after_annual_energy_wh is inconsistent"):
            extract_iso_excel(workbook_bytes(invalid_after=True))

    def test_updated_source_fields_have_stable_names_and_units(self):
        rows = extract_iso_excel(workbook_bytes())
        found = {row["field"]: row for row in rows}
        self.assertEqual(found["before_reference_main_energy_wh"]["value"], 484.5)
        self.assertEqual(found["after_reference_ancillary_energy_wh"]["unit"], "Wh/cycle")
        self.assertEqual(found["before_reference_cycle_count"]["value"], 10)
        self.assertEqual(found["after_scenario_total_time_ratio"]["value"], 100)
        self.assertFalse(any("iso_scenarios.r4" in key for key in found))

    def test_cross_sheet_annual_energy_mismatch_is_rejected(self):
        records = extract_iso_excel(workbook_bytes())
        for row in records:
            if row["field"] == "before_calculation_annual_energy_kwh":
                row["value"] = 999
        with self.assertRaisesRegex(ValueError, "before_calculation_annual_energy_kwh"):
            _check_calculations(records, dual=True)

    def test_reference_cycle_parts_must_add_up(self):
        records = extract_iso_excel(workbook_bytes())
        for row in records:
            if row["field"] == "after_reference_main_energy_wh":
                row["value"] = 100
        with self.assertRaisesRegex(ValueError, "after_reference_energy_wh"):
            _check_calculations(records, dual=True)

    def test_scenario_time_ratios_must_add_to_100(self):
        records = extract_iso_excel(workbook_bytes())
        for row in records:
            if row["field"] == "after_scenario_standby30_ratio":
                row["value"] = 35
        with self.assertRaisesRegex(ValueError, "scenario time ratios"):
            _check_calculations(records, dual=True)

    @unittest.skipUnless(os.getenv("ISO_WORKBOOK_PATH"), "Set ISO_WORKBOOK_PATH for the real workbook")
    def test_uploaded_op13_workbook(self):
        rows = extract_iso_excel(Path(os.environ["ISO_WORKBOOK_PATH"]).read_bytes())
        values = {row["field"]: row["value"] for row in rows}
        self.assertEqual(len(rows), len(values))
        self.assertEqual(len(rows), 152)
        self.assertEqual(values["lift_id"], "OP13 (L)")
        self.assertAlmostEqual(values["before_annual_energy_kwh"], 41933.05, places=2)
        self.assertAlmostEqual(values["after_annual_energy_kwh"], 22084.07, places=2)
        self.assertEqual(values["before_efficiency_class"], "C")
        self.assertEqual(values["after_efficiency_class"], "A")
        self.assertAlmostEqual(values["after_energy_saving_percent"], 47.3349, places=3)

    def test_single_column_legacy_workbook_remains_supported(self):
        rows = extract_iso_excel(workbook_bytes(dual=False))
        values = {row["field"]: row["value"] for row in rows}
        self.assertEqual(values["annual_energy_wh"], 54750)
        self.assertEqual(values["daily_energy_wh"], 150)
        self.assertNotIn("before_daily_energy_wh", values)

    def test_legacy_can_omit_optional_inputs(self):
        records = extract_iso_excel(workbook_bytes(dual=False))
        records = [r for r in records if r["field"] != "daily_nonrunning_energy_wh"]
        _check_calculations(records, dual=False)

    def test_incorrect_saving_percentage_is_rejected(self):
        records = extract_iso_excel(workbook_bytes())
        for row in records:
            if row["field"] == "after_energy_saving_percent":
                row["value"] = 99
        with self.assertRaisesRegex(ValueError, "saving percentage"):
            _check_calculations(records, dual=True)

    def test_missing_energy_input_is_rejected(self):
        records = extract_iso_excel(workbook_bytes())
        for row in records:
            if row["field"] == "after_daily_nonrunning_energy_wh":
                row["value"] = None
        with self.assertRaisesRegex(ValueError, "Missing Excel values"):
            _check_calculations(records, dual=True)

    def test_nonnumeric_daily_energy_is_rejected(self):
        records = extract_iso_excel(workbook_bytes())
        for row in records:
            if row["field"] == "after_daily_energy_wh":
                row["value"] = "invalid"
        with self.assertRaisesRegex(ValueError, "Invalid Excel numeric value"):
            _check_calculations(records, dual=True)

    def test_missing_operating_days_is_rejected(self):
        records = extract_iso_excel(workbook_bytes())
        for row in records:
            if row["field"] == "operating_days":
                row["value"] = None
        with self.assertRaisesRegex(ValueError, "Missing Excel values"):
            _check_calculations(records, dual=True)

    def test_inconsistent_calculation_savings_is_rejected(self):
        records = extract_iso_excel(workbook_bytes())
        records.append({"field": "after_calculation_energy_saving_percent", "value": -20})
        with self.assertRaisesRegex(ValueError, "saving percentage"):
            _check_calculations(records, dual=True)

    def test_rejects_unreadable_workbook(self):
        with self.assertRaises(ValueError):
            extract_iso_excel(b"not-xlsx")


if __name__ == "__main__":
    unittest.main()
