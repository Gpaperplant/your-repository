"""Offline regression tests for read-only five-table KPI reporting."""

import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
import importlib.util
import unittest

import pytest

# Initialise NumPy/pandas before replacing Streamlit with a fake module in
# page-rendering tests. This mirrors test_enercal_review_iso_tab.py and keeps
# compiled dependency imports outside the temporary module override.
import numpy as np  # noqa: F401
import pandas as pd  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "database" not in sys.modules:
    try:
        import database  # noqa F401
    except ModuleNotFoundError:
        stub = ModuleType("database")
        stub.get_connection = lambda: (_ for _ in ()).throw(RuntimeError("No live DB in offline test"))
        sys.modules["database"] = stub

from services import unified_kpi_reporting as repo


class Cursor:
    def __init__(self, records):
        self.records = records
        self.description = None
        self.result = []
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if sql.startswith("SET "):
            return
        self.result = self.records
        self.description = [SimpleNamespace(name=key) for key in self.records[0]] if self.records else []

    def fetchall(self):
        return [tuple(record.values()) for record in self.result]


class Connection:
    def __init__(self, cursor):
        self.value = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.value


def source_row(**changes):
    row = dict(document_id=7, case_ref="TEST-MOD-REV-001", filename="EnerCal.pdf",
               source_classification="test_fixture", revision_id=9,
               revision_no=2, review_scope="comparison_only", review_status="approved",
               phase="common", field_name=None, numeric_value=None, text_value=None,
               raw_source_value=None, unit=None, source_verified=False)
    return {**row, **changes}


def value(phase, field, number=None, text=None, unit=None, verified=False):
    return source_row(phase=phase, field_name=field,
                      numeric_value=number, text_value=text,
                      raw_source_value=(str(number) if number is not None else text),
                      unit=unit, source_verified=verified)


def mod_rows():
    rows = [
        value("existing", "elevator_technology", text="Old"),
        value("proposed", "elevator_technology", text="New"),
        value("existing", "annual_energy_consumption", Decimal("10000"), unit="kWh/year"),
        value("proposed", "annual_energy_consumption", Decimal("5500"), unit="kWh/year"),
        value("existing", "annual_regenerative_energy", Decimal("100")),
        value("proposed", "annual_regenerative_energy", Decimal("200")),
        value("existing", "annual_electricity_expenditure", Decimal("2000"), unit="$/year"),
        value("proposed", "annual_electricity_expenditure", Decimal("1100"), unit="$/year"),
        value("existing", "lifetime_electricity_expenditure", Decimal("50000"), unit="$/lifetime"),
        value("proposed", "lifetime_electricity_expenditure", Decimal("27500"), unit="$/lifetime"),
        value("existing", "operational_co2e", Decimal("30000"), unit="kgCO2e/lifetime 25 years"),
        value("proposed", "operational_co2e", Decimal("17000"), unit="kgCO2e/lifetime 25 years"),
        value("existing", "embodied_co2e", Decimal("4000")),
        value("proposed", "embodied_co2e", Decimal("5000")),
        value("common", "calculated_energy_saving_percent", Decimal("45")),
        value("common", "reported_energy_saving_percent", Decimal("44.8")),
        value("common", "lifetime_years", Decimal("25")),
        value("common", "country", text="Singapore"),
        value("common", "report_date", text="2026-11-17"),
        value("common", "electricity_price", Decimal("0.2"), unit="$/kWh"),
    ]
    return rows


def collect_mod(rows=None):
    cursor = Cursor(mod_rows() if rows is None else rows)
    previous = repo.get_connection
    repo.get_connection = lambda: Connection(cursor)
    try:
        reports = repo.load_reports()
    finally:
        repo.get_connection = previous
    return reports, cursor


class TestRepo(unittest.TestCase):
    def test_sql_reads_new_tables_only(self):
        sql = " ".join([repo.STATUS_SQL, repo.MOD_SQL, repo.ISO_SQL]).lower()
        for table in ("modernisation_cases", "project_documents", "document_revisions", "engineering_values"):
            self.assertIn(table, sql)
        for old in ("source_documents", "extraction_revisions", "extraction_candidates",
                    "iso_assessments", "iso_documents", "iso_extracted_values",
                    "enercal_approved_kpis_view"):
            self.assertNotIn(old, sql)
        self.assertNotIn("file_content", sql)
        self.assertNotIn("delete ", sql)
        self.assertNotIn("update ", sql)

    def test_queries_use_read_only_transactions(self):
        _, cursor = collect_mod()
        self.assertEqual(cursor.queries[0][0], "SET TRANSACTION READ ONLY")
        self.assertIn("statement_timeout", cursor.queries[1][0])
        self.assertEqual(len(cursor.queries), 3)

    def test_approved_snapshot_values(self):
        result, _ = collect_mod()
        self.assertEqual(len(result), 1)
        r = result[0]
        self.assertEqual(r["existing_energy"], 10000)
        self.assertEqual(r["proposed_energy"], 5500)
        self.assertEqual(r["calculated_saving"], 45)
        self.assertEqual(r["existing_annual_cost"], 2000)
        self.assertEqual(r["lifetime_years"], 25)
        self.assertEqual(r["currency_symbol"], "$")
        self.assertEqual(r["country"], "Singapore")
        self.assertEqual(r["report_date"], "2026-11-17")
        self.assertEqual(r["electricity_price"], Decimal("0.2"))

    def test_fixture_not_real_verified(self):
        reports, _ = collect_mod()
        self.assertFalse(reports[0]["fully_source_verified"])
        self.assertFalse(reports[0]["real_verified"])
        self.assertEqual(reports[0]["source_classification"], "test_fixture")

    def test_comparison_review_does_not_imply_full_verification(self):
        result, _ = collect_mod([
            {**r, "source_classification": "real", "source_verified": True}
            for r in mod_rows()
        ])
        self.assertFalse(result[0]["fully_source_verified"])
        self.assertFalse(result[0]["real_verified"])

    def test_full_verified_real(self):
        result, _ = collect_mod([
            {**r, "review_scope": "full_values", "source_classification": "real",
             "source_verified": True} for r in mod_rows()
        ])
        self.assertTrue(result[0]["fully_source_verified"])
        self.assertTrue(result[0]["real_verified"])

    def test_full_verified_fixture_still_not_real(self):
        result, _ = collect_mod([
            {**r, "review_scope": "full_values", "source_verified": True} for r in mod_rows()
        ])
        self.assertTrue(result[0]["fully_source_verified"])
        self.assertFalse(result[0]["real_verified"])

    def test_inconsistent_saving_excluded(self):
        rows = mod_rows()
        for row in rows:
            if row["field_name"] == "calculated_energy_saving_percent":
                row["numeric_value"] = Decimal("15")
        result, _ = collect_mod(rows)
        self.assertEqual(result, [])

    def test_rounding_tolerance(self):
        rows = mod_rows()
        for row in rows:
            if row["field_name"] == "calculated_energy_saving_percent":
                row["numeric_value"] = Decimal("44.88")
        result, _ = collect_mod(rows)
        self.assertEqual(len(result), 1)

    def test_missing_technology_excluded(self):
        result, _ = collect_mod([r for r in mod_rows() if r["field_name"] != "elevator_technology" or r["phase"] != "existing"])
        self.assertEqual(result, [])

    def test_missing_existing_energy_excluded(self):
        result, _ = collect_mod([r for r in mod_rows() if (r["phase"],r["field_name"]) != ("existing", "annual_energy_consumption")])
        self.assertEqual(result, [])

    def test_zero_existing_energy_excluded(self):
        rows = mod_rows()
        for row in rows:
            if (row["phase"], row["field_name"]) == ("existing", "annual_energy_consumption"):
                row["numeric_value"] = 0
        result, _ = collect_mod(rows)
        self.assertEqual(result, [])

    def test_duplicate_field_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate MOD engineering value"):
            collect_mod([*mod_rows(), mod_rows()[0]])

    def test_null_numeric_optional(self):
        rows = mod_rows()
        for row in rows:
            if row["field_name"] == "embodied_co2e":
                row["numeric_value"] = None
                row["raw_source_value"] = "N/A"
        result, _ = collect_mod(rows)
        self.assertIsNone(result[0]["existing_embodied_co2e"])

    def test_inconsistent_currency_unset(self):
        rows = mod_rows()
        for row in rows:
            if (row["phase"], row["field_name"]) == ("proposed", "annual_electricity_expenditure"):
                row["unit"] = "SGD/year"
        result, _ = collect_mod(rows)
        self.assertEqual(result[0]["currency_symbol"], "")

    def test_lifetime_from_operational_unit(self):
        result, _ = collect_mod([r for r in mod_rows() if r["field_name"] != "lifetime_years"])
        self.assertEqual(result[0]["lifetime_years"], 25)

    def test_no_approved_rows(self):
        self.assertEqual(collect_mod([])[0], [])

    def test_numbers_guard_invalid_values(self):
        for val in (None, True, "NaN", "Infinity", "abc"):
            self.assertIsNone(repo._number(val))
        self.assertEqual(repo._number("5.25"), Decimal("5.25"))

    def test_statuses_query_keeps_cases_with_no_documents(self):
        cursor = Cursor([dict(case_ref="MOD1001", case_status="pending", document_id=None,
                              filename=None, source_classification=None,
                              revision_no=None, review_scope=None, review_status=None)])
        previous = repo.get_connection
        repo.get_connection = lambda: Connection(cursor)
        try:
            statuses = repo.load_case_statuses()
        finally:
            repo.get_connection = previous
        self.assertIsNone(statuses[0]["document_id"])
        self.assertIn("LEFT JOIN LATERAL", cursor.queries[-1][0])

    def test_mod_query_uses_latest_document_and_revision(self):
        self.assertIn("MAX(newest.document_id)", repo.MOD_SQL)
        self.assertIn("MAX(latest.revision_no)", repo.MOD_SQL)
        self.assertIn("r.review_status = 'approved'", repo.MOD_SQL)

    def test_iso_query_uses_latest_document_and_revision(self):
        self.assertIn("MAX(newest.document_id)", repo.ISO_SQL)
        self.assertIn("ORDER BY revision_no DESC LIMIT 1", repo.ISO_SQL)


def iso_row(**changes):
    row = dict(case_ref="MOD1006", document_id=3, filename="ISO.xlsx",
               reported_elevator_ref="OP13(L)", uploaded_at="2026-11-17",
               source_classification="test_fixture", revision_no=1,
               review_scope="comparison_only", review_status="approved",
               phase="existing", field_name="annual_energy_kwh", numeric_value=Decimal("10000"),
               text_value=None)
    return {**row, **changes}


def collect_iso(rows):
    cursor = Cursor(rows)
    previous = repo.get_connection
    repo.get_connection = lambda: Connection(cursor)
    try:
        result = repo.load_iso_reports()
    finally:
        repo.get_connection = previous
    return result, cursor


class TestISO(unittest.TestCase):
    def test_before_after_energy_and_class(self):
        result, cursor = collect_iso([
            iso_row(),
            iso_row(phase="proposed", numeric_value=Decimal("6000")),
            iso_row(field_name="efficiency_class", numeric_value=None, text_value="C"),
            iso_row(phase="proposed", field_name="efficiency_class", numeric_value=None, text_value="A"),
        ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["values"]["before_efficiency_class"], "C")
        self.assertEqual(result[0]["values"]["after_efficiency_class"], "A")
        self.assertEqual(repo.iso_saving(result[0]), Decimal("40"))
        self.assertEqual(cursor.queries[-1][1], (["annual_energy_kwh", "annual_energy_wh", "efficiency_class"],))
        self.assertEqual(len(cursor.queries[-1][1]), 1, "ANY(%s) binds one SQL array parameter")

    def test_iso_common_single_column(self):
        result, _ = collect_iso([iso_row(phase="common", numeric_value=Decimal("123"))])
        self.assertEqual(result[0]["values"]["annual_energy_kwh"], 123)
        self.assertEqual(repo.iso_annual_energy(result[0]), 123)

    def test_iso_annual_wh_conversion(self):
        report = {"values": {"before_annual_energy_wh": Decimal("1500000"),
                             "after_annual_energy_wh": Decimal("750000")}}
        self.assertEqual(repo.iso_annual_energy(report, "before_"), Decimal("1500"))
        self.assertEqual(repo.iso_saving(report), Decimal("50"))

    def test_iso_kwh_preferred_to_wh(self):
        report = {"values": {"before_annual_energy_kwh": 10,
                             "before_annual_energy_wh": 900000}}
        self.assertEqual(repo.iso_annual_energy(report, "before_"), 10)

    def test_iso_no_saving_without_before_after(self):
        self.assertIsNone(repo.iso_saving({"values": {"before_annual_energy_kwh": 1}}))
        self.assertIsNone(repo.iso_saving({"values": {"before_annual_energy_kwh": 0, "after_annual_energy_kwh": 3}}))

    def test_negative_iso_energy_not_displayed(self):
        self.assertIsNone(repo.iso_annual_energy({"values": {"before_annual_energy_kwh": -1}}, "before_"))

    def test_iso_no_revision_still_visible(self):
        result, _ = collect_iso([iso_row(phase=None, field_name=None, review_status=None, revision_no=None, review_scope=None, numeric_value=None)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["review_status"], "pending")
        self.assertEqual(result[0]["values"], {})

    def test_iso_duplicate_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate ISO result"):
            collect_iso([iso_row(), iso_row()])

    def test_invalid_iso_phase_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid ISO engineering phase"):
            collect_iso([iso_row(phase="unknown")])

    def test_iso_latest_classification_preserved(self):
        result, _ = collect_iso([iso_row(review_status="rejected", source_classification="unknown")])
        self.assertEqual(result[0]["source_classification"], "unknown")
        self.assertEqual(result[0]["review_status"], "rejected")

    def test_readiness_requires_both_current_approvals(self):
        statuses = [dict(case_ref="MOD1006", review_status="approved"),
                    dict(case_ref="MOD1007", review_status="pending")]
        iso = [dict(case_ref="MOD1006", review_status="approved"),
               dict(case_ref="MOD1007", review_status="approved")]
        self.assertEqual(repo.case_readiness(statuses, iso), {"MOD1006": True, "MOD1007": False})

    def test_missing_iso_not_ready(self):
        self.assertEqual(repo.case_readiness([dict(case_ref="A", review_status="approved")], []), {"A": False})


class TestPageStatic(unittest.TestCase):
    def test_page_imports_new_only_repository(self):
        text = (ROOT / "enercal" / "enercal_reporting.py").read_text()
        self.assertIn("services.unified_kpi_reporting", text)
        self.assertNotIn("from database import", text)
        for old in ("source_documents", "extraction_revisions", "extraction_candidates", "iso_assessments", "iso_documents", "iso_extracted_values", "enercal_approved_kpis_view"):
            self.assertNotIn(old, text)

    def test_lifetime_currency_comparison_guard(self):
        text = (ROOT / "enercal" / "enercal_reporting.py").read_text()
        self.assertIn("comparable_unit", text)
        self.assertIn("currency_symbol", text)
        self.assertIn("lifetime_years", text)

    def test_evidence_classification_displayed(self):
        text = (ROOT / "enercal" / "enercal_reporting.py").read_text()
        self.assertIn("approval_description", text)
        self.assertIn("source_classification", text)
        self.assertIn("comparison_only", text)
        self.assertIn("real_verified", text)

    def test_admin_guard(self):
        text = (ROOT / "enercal" / "enercal_reporting.py").read_text()
        self.assertIn('st.session_state.get("role") != "Admin"', text)


if __name__ == "__main__":
    unittest.main()

class _StopPage(Exception):
    pass


class FakeStreamlit(ModuleType):
    def __init__(self, role="Admin"):
        super().__init__("streamlit")
        self.session_state = {"role": role}
        self.messages = []
        self.tables = []
        self.metrics = []
        self.downloads = []
        self.choices = []

    def subheader(self, message):
        self.messages.append(("subheader", message))

    def caption(self, message):
        self.messages.append(("caption", message))

    def info(self, message):
        self.messages.append(("info", message))

    def success(self, message):
        self.messages.append(("success", message))

    def warning(self, message):
        self.messages.append(("warning", message))

    def error(self, message):
        self.messages.append(("error", message))

    def stop(self):
        raise _StopPage()

    def columns(self, count):
        return [self] * (count if isinstance(count, int) else len(count))

    def metric(self, label, value):
        self.metrics.append((label, value))

    def selectbox(self, label, options, **kwargs):
        self.choices.append(label)
        return list(options)[0]

    def dataframe(self, rows, **kwargs):
        self.tables.append(rows)

    def write(self, message):
        self.messages.append(("write", message))

    def download_button(self, label, data, file_name, mime):
        self.downloads.append((label, data, file_name, mime))


def render_page(role="Admin", statuses=None, reports=None, iso=None):
    from unittest.mock import patch
    ui = FakeStreamlit(role)
    cases = statuses if statuses is not None else [{
        "case_ref": "TEST-MOD-REV-001", "case_status": "pending",
        "document_id": 7, "filename": "EnerCal.pdf",
        "source_classification": "test_fixture", "revision_no": 2,
        "review_scope": "comparison_only", "review_status": "approved",
    }]
    if reports is None:
        reports, _ = collect_mod()
    iso = [] if iso is None else iso
    with (patch.dict(sys.modules, {"streamlit": ui}),
          patch.object(repo, "load_case_statuses", return_value=cases) as cases_call,
          patch.object(repo, "load_reports", return_value=reports) as reports_call,
          patch.object(repo, "load_iso_reports", return_value=iso) as iso_call):
        spec = importlib.util.spec_from_file_location(
            "test_unified_reporting_page_instance", ROOT / "enercal" / "enercal_reporting.py"
        )
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except _StopPage:
            pass
    return ui, cases_call, reports_call, iso_call


class TestPageRuntime(unittest.TestCase):
    def test_non_admin_has_no_access_or_database_read(self):
        ui, cases, reports, iso = render_page(role="Requester")
        self.assertIn(("error", "Admin access is required"), ui.messages)
        cases.assert_not_called()
        reports.assert_not_called()
        iso.assert_not_called()

    def test_test_fixture_is_explicitly_warned(self):
        ui, _, _, _ = render_page()
        self.assertTrue(any(kind == "warning" and "Test Fixture" in message
                            for kind, message in ui.messages))
        self.assertTrue(any(label == "Approved MOD estimates" and value == 1
                            for label, value in ui.metrics))
        self.assertTrue(any("0 have real, fully source-verified evidence" in message
                            for kind, message in ui.messages if kind == "caption"))

    def test_approved_csv_contains_evidence_label(self):
        ui, _, _, _ = render_page()
        self.assertEqual(len(ui.downloads), 1)
        _, data, filename, mime = ui.downloads[0]
        self.assertEqual(filename, "unified_approved_mod_estimates.csv")
        self.assertEqual(mime, "text/csv")
        self.assertIn(b"evidence_label", data)
        self.assertIn(b"test_fixture", data)

    def test_empty_reports_show_info_not_fake_success(self):
        ui, _, _, _ = render_page(reports=[])
        self.assertTrue(any("No complete" in message
                            for kind, message in ui.messages if kind == "info"))
        self.assertEqual(ui.downloads, [])

    def test_iso_section_displays_workbook_results(self):
        iso = [{
            "case_ref": "TEST-MOD-REV-001", "document_id": 12,
            "filename": "ISO.xlsx", "lift_id": "TEST-LIFT-001",
            "review_status": "approved", "review_scope": "comparison_only",
            "source_classification": "test_fixture",
            "values": {"before_annual_energy_kwh": 10000, "after_annual_energy_kwh": 5000},
        }]
        ui, _, _, _ = render_page(iso=iso)
        self.assertTrue(any("Latest ISO Excel" in message
                            for kind, message in ui.messages if kind == "subheader"))
        self.assertEqual(len(ui.downloads), 2)
        self.assertTrue(any("ISO estimated saving (%)" in str(table) for table in ui.tables))

    def test_admin_failure_is_caught(self):
        from unittest.mock import patch
        ui = FakeStreamlit()
        with patch.dict(sys.modules, {"streamlit": ui}), patch.object(
                repo, "load_case_statuses", side_effect=RuntimeError("DB offline")):
            spec = importlib.util.spec_from_file_location(
                "test_reporting_error_instance", ROOT / "enercal" / "enercal_reporting.py"
            )
            module = importlib.util.module_from_spec(spec)
            with self.assertRaises(_StopPage):
                spec.loader.exec_module(module)
        self.assertTrue(any("DB offline" in message
                            for kind, message in ui.messages if kind == "error"))

    def test_comparison_of_two_reports_renders(self):
        reports, _ = collect_mod()
        second = dict(reports[0], document_id=8, case_ref="TEST2")
        ui, _, _, _ = render_page(reports=[reports[0], second])
        self.assertIn("KPI to compare", ui.choices)
        self.assertTrue(any("Existing (kWh/year)" in str(table)
                            for table in ui.tables))
