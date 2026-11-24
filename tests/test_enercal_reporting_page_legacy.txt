"""Regression tests for the FIVE-TABLE Approved Cases Overview.

This replaces the retired legacy-reporting-page tests. It continues to protect
KPI units, review status, privacy, ISO calculations, and evidence labelling,
without making queries to old MOD/ISO tables or requiring a live database.
"""

import importlib.util
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

# Pandas imports compiled NumPy dependencies. Import both before temporarily
# substituting a lightweight Streamlit module (the same precaution as the
# working unified dashboard tests).
import numpy as np  # noqa: F401
import pandas as pd  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PAGE = ROOT / "enercal" / "enercal_reporting.py"

# Local fixture runner compatibility; in Codespaces, database.py exists.
if "database" not in sys.modules:
    try:
        import database  # noqa: F401
    except ModuleNotFoundError:
        db_stub = ModuleType("database")
        db_stub.get_connection = lambda: (_ for _ in ()).throw(RuntimeError("No live DB"))
        sys.modules["database"] = db_stub

from services import unified_kpi_reporting as repo


class PageStopped(Exception):
    """Simulate Streamlit's st.stop without terminating the test process."""


class FakeStreamlit(ModuleType):
    def __init__(self, role="Admin", selections=None):
        super().__init__("streamlit")
        self.session_state = {"role": role}
        self.selections = selections or {}
        self.messages = []
        self.metrics = {}
        self.tables = []
        self.downloads = []
        self.choices = []
        self.charts = []

    def stop(self):
        raise PageStopped()

    def subheader(self, message):
        self.messages.append(("subheader", str(message)))

    def caption(self, message):
        self.messages.append(("caption", str(message)))

    def write(self, message):
        self.messages.append(("write", str(message)))

    def error(self, message):
        self.messages.append(("error", str(message)))

    def warning(self, message):
        self.messages.append(("warning", str(message)))

    def info(self, message):
        self.messages.append(("info", str(message)))

    def success(self, message):
        self.messages.append(("success", str(message)))

    def columns(self, count):
        return [self] * (count if isinstance(count, int) else len(count))

    def metric(self, label, value, **kwargs):
        self.metrics[label] = value

    def dataframe(self, data, **kwargs):
        self.tables.append(data)

    def selectbox(self, label, options, **kwargs):
        options = list(options)
        self.choices.append((label, options))
        selected = self.selections.get(label, options[0])
        if selected not in options:
            raise AssertionError(f"Selection {selected!r} absent from {label!r}")
        return selected

    def download_button(self, label, data, file_name, mime, **kwargs):
        self.downloads.append({
            "label": label, "data": data, "filename": file_name, "mime": mime,
        })

    def bar_chart(self, *args, **kwargs):
        self.charts.append((args, kwargs))


def status(case="TEST-MOD-REV-001", document_id=7, review="approved", **changes):
    source = {
        "case_ref": case,
        "case_status": "registered",
        "document_id": document_id,
        "filename": "EnerCal.pdf" if document_id is not None else None,
        "source_classification": "test_fixture" if document_id is not None else None,
        "revision_no": 2 if document_id is not None else None,
        "review_scope": "comparison_only" if document_id is not None else None,
        "review_status": review if document_id is not None else None,
    }
    return {**source, **changes}


def report(case="TEST-MOD-REV-001", document_id=7, **changes):
    source = {
        "document_id": document_id, "case_ref": case,
        "filename": "EnerCal.pdf", "revision_id": 9,
        "revision_no": 2, "review_scope": "comparison_only",
        "source_classification": "test_fixture",
        "real_verified": False, "fully_source_verified": False,
        "country": "Singapore", "report_date": "2026-11-17",
        "lifetime_years": 25, "currency_symbol": "$",
        "electricity_price": Decimal("0.2"),
        "electricity_price_unit": "$/kWh",
        "reported_saving": Decimal("44.8"),
        "calculated_saving": Decimal("45"),
        "existing_technology": "Old", "proposed_technology": "New",
        "existing_energy": Decimal("10000"), "proposed_energy": Decimal("5500"),
        "existing_regen": Decimal("100"), "proposed_regen": Decimal("200"),
        "existing_annual_cost": Decimal("2000"),
        "proposed_annual_cost": Decimal("1100"),
        "existing_lifetime_cost": Decimal("50000"),
        "proposed_lifetime_cost": Decimal("27500"),
        "existing_operational_co2e": Decimal("30000"),
        "proposed_operational_co2e": Decimal("17000"),
        "existing_embodied_co2e": Decimal("4000"),
        "proposed_embodied_co2e": Decimal("5000"),
    }
    return {**source, **changes}


def iso(case="TEST-MOD-REV-001", **changes):
    source = {
        "case_ref": case, "document_id": 12, "filename": "ISO.xlsx",
        "lift_id": "TEST-LIFT-001", "imported_at": "2026-11-17",
        "review_status": "approved", "review_scope": "comparison_only",
        "source_classification": "test_fixture", "revision_no": 1,
        "values": {
            "before_annual_energy_kwh": Decimal("41933.0484"),
            "after_annual_energy_kwh": Decimal("22084.06994"),
            "before_efficiency_class": "C", "after_efficiency_class": "A",
        },
    }
    return {**source, **changes}


def render(statuses=None, reports=None, iso_reports=None, role="Admin", selections=None,
           fail_at=None):
    """Render the actual page; stub ONLY repository I/O and Streamlit output."""
    cases = [status()] if statuses is None else statuses
    mods = [report()] if reports is None else reports
    workbooks = [] if iso_reports is None else iso_reports
    ui = FakeStreamlit(role, selections)
    calls = {}
    with patch.dict(sys.modules, {"streamlit": ui}), \
         patch.object(repo, "load_case_statuses", side_effect=(
             RuntimeError("DB unavailable") if fail_at == "statuses" else None
         ), return_value=cases) as case_call, \
         patch.object(repo, "load_reports", side_effect=(
             RuntimeError("DB unavailable") if fail_at == "reports" else None
         ), return_value=mods) as mod_call, \
         patch.object(repo, "load_iso_reports", side_effect=(
             RuntimeError("DB unavailable") if fail_at == "iso" else None
         ), return_value=workbooks) as iso_call:
        instance = importlib.util.spec_from_file_location("_isolated_unified_kpi_page", PAGE)
        page = importlib.util.module_from_spec(instance)
        try:
            instance.loader.exec_module(page)
        except PageStopped:
            pass
        calls = {"statuses": case_call, "reports": mod_call, "iso": iso_call}
    return page, ui, calls


class TestOverview(unittest.TestCase):
    def test_default_overview_and_counts(self):
        _, ui, _ = render()
        self.assertEqual(ui.metrics["Registered cases"], 1)
        self.assertEqual(ui.metrics["Approved MOD estimates"], 1)
        self.assertEqual(ui.metrics["Without unified MOD"], 0)
        self.assertEqual(ui.metrics["Pending MOD reviews"], 0)
        self.assertEqual(ui.metrics["Cases with ISO Excel"], 0)
        self.assertEqual(ui.tables[0][0]["Case"], "TEST-MOD-REV-001")

    def test_no_unified_mod_is_visible_as_registered_case(self):
        _, ui, _ = render(statuses=[status(document_id=None)], reports=[])
        self.assertEqual(ui.metrics["Without unified MOD"], 1)
        self.assertEqual(ui.tables[0][0]["MOD review"], "No unified MOD PDF")

    def test_pending_mod_count(self):
        cases = [status("A", 1, "pending"), status("B", 2, "approved")]
        _, ui, _ = render(statuses=cases, reports=[])
        self.assertEqual(ui.metrics["Pending MOD reviews"], 1)

    def test_case_filter(self):
        cases = [status("A", 1), status("B", 2)]
        _, ui, _ = render(statuses=cases, reports=[], selections={"Filter cases": "B"})
        self.assertEqual([r["Case"] for r in ui.tables[0]], ["B"])

    def test_review_status_filter(self):
        cases = [status("A", 1, "pending"), status("B", 2)]
        _, ui, _ = render(statuses=cases, reports=[], selections={"Filter MOD review status": "Pending"})
        self.assertEqual([r["Case"] for r in ui.tables[0]], ["A"])

    def test_no_matches_keeps_unfiltered_summary(self):
        cases = [status("A", 1, "pending"), status("B", 2)]
        _, ui, _ = render(statuses=cases, reports=[], selections={
            "Filter cases": "B", "Filter MOD review status": "Pending"})
        self.assertEqual(ui.tables[0], [])
        self.assertEqual(ui.metrics["Registered cases"], 2)

    def test_no_unified_mod_filter(self):
        cases = [status("A", None), status("B", 2)]
        _, ui, _ = render(statuses=cases, reports=[], selections={
            "Filter MOD review status": "No unified MOD PDF"})
        self.assertEqual([r["Case"] for r in ui.tables[0]], ["A"])

    def test_approved_with_incomplete_kpi_is_labelled(self):
        _, ui, _ = render(reports=[])
        self.assertEqual(ui.tables[0][0]["MOD review"],
                         "Approved (KPI data incomplete or inconsistent)")

    def test_pending_without_extraction_is_labelled(self):
        _, ui, _ = render(statuses=[status(review=None)], reports=[])
        self.assertEqual(ui.tables[0][0]["MOD review"], "Extraction missing")

    def test_comparison_approval_is_not_full_approval(self):
        _, ui, _ = render()
        self.assertEqual(ui.tables[0][0]["MOD review"], "Approved comparison")

    def test_full_approval_label(self):
        _, ui, _ = render(statuses=[status(review_scope="full_values")])
        self.assertEqual(ui.tables[0][0]["MOD review"], "Approved")

    def test_empty_database(self):
        _, ui, _ = render(statuses=[], reports=[])
        self.assertEqual(ui.metrics["Registered cases"], 0)
        self.assertTrue(any("No modernisation cases" in m for k, m in ui.messages))
        self.assertEqual(ui.downloads, [])

    def test_no_case_records_does_not_fabricate_kpis(self):
        _, ui, _ = render(statuses=[], reports=[])
        self.assertEqual(ui.metrics["Approved MOD estimates"], 0)
        self.assertTrue(any(k == "info" and "No complete" in m for k, m in ui.messages))

    def test_iso_status_visible_in_case_overview(self):
        _, ui, _ = render(iso_reports=[iso(review_status="pending")])
        self.assertEqual(ui.tables[0][0]["ISO review"], "Pending")
        self.assertEqual(ui.metrics["Cases with ISO Excel"], 1)

    def test_both_comparisons_approved_is_not_full_verification(self):
        _, ui, _ = render(iso_reports=[iso()])
        self.assertEqual(ui.metrics["Both comparisons approved"], 1)
        self.assertEqual(ui.tables[0][0]["Both comparisons"], "Approved")
        self.assertTrue(any("does not establish source verification" in m
                            for k, m in ui.messages if k == "caption"))

    def test_iso_only_approval_is_not_ready(self):
        _, ui, _ = render(statuses=[status(review="pending")], reports=[], iso_reports=[iso()])
        self.assertEqual(ui.metrics["Both comparisons approved"], 0)

    def test_mod_only_approval_is_not_ready(self):
        _, ui, _ = render()
        self.assertEqual(ui.metrics["Both comparisons approved"], 0)

    def test_case_status_not_missing(self):
        _, ui, _ = render(statuses=[status(case_status="on_hold")])
        self.assertEqual(ui.tables[0][0]["Case status"], "on_hold")


class TestKPIPage(unittest.TestCase):
    def test_estimated_kpi_metrics_preserve_units(self):
        _, ui, _ = render()
        self.assertEqual(ui.metrics["Estimated saving"], "45%")
        self.assertEqual(ui.metrics["Estimated annual energy reduction"], "4,500 kWh/year")
        self.assertEqual(ui.metrics["Estimated annual cost reduction"], "900 $")

    def test_approved_csv_contains_evidence_label(self):
        _, ui, _ = render()
        self.assertIn("evidence_label", ui.downloads[0]["data"].decode("utf-8-sig"))
        self.assertEqual(ui.downloads[0]["filename"], "unified_approved_mod_estimates.csv")

    def test_private_contact_not_exported(self):
        r = report()
        r["contact_email"] = "private@example.test"
        r["contact_phone"] = "+65 1234 5678"
        _, ui, _ = render(reports=[r])
        csv = ui.downloads[0]["data"].decode("utf-8-sig")
        self.assertNotIn("private@example.test", csv)
        self.assertNotIn("1234 5678", csv)
        self.assertNotIn("contact_email", csv)

    def test_test_fixture_warning_visible(self):
        _, ui, _ = render()
        self.assertTrue(any(k == "warning" and "Test Fixture" in m for k, m in ui.messages))
        self.assertIn("0 have real, fully source-verified evidence", " ".join(
            m for k, m in ui.messages if k == "caption"))

    def test_real_verified_evidence_has_success(self):
        real = report(real_verified=True, source_classification="real",
                      fully_source_verified=True, review_scope="full_values")
        _, ui, _ = render(reports=[real])
        self.assertTrue(any(k == "success" and "Real evidence" in m for k, m in ui.messages))

    def test_real_comparison_only_is_not_verified(self):
        real = report(source_classification="real", real_verified=False)
        _, ui, _ = render(reports=[real])
        self.assertTrue(any(k == "warning" and "not source-verified" in m
                            for k, m in ui.messages))

    def test_no_approved_kpi_does_not_show_success(self):
        _, ui, _ = render(reports=[])
        self.assertFalse(any(k == "success" for k, _ in ui.messages))
        self.assertTrue(any(k == "info" and "No complete" in m for k, m in ui.messages))

    def test_two_report_comparison_preserves_energy_units(self):
        r1, r2 = report(), report(case="SECOND", document_id=8)
        _, ui, _ = render(reports=[r1, r2])
        comparison = next(t for t in ui.tables if t and "Estimated saving (%)" in t[0])
        self.assertEqual(comparison[0]["Existing (kWh/year)"], "10,000")
        self.assertEqual(comparison[0]["Proposed (kWh/year)"], "5,500")
        self.assertEqual(comparison[0]["Difference (kWh/year)"], "4,500")

    def test_multiple_reports_remain_separate(self):
        _, ui, _ = render(reports=[report(), report(case="SECOND", document_id=8)])
        comparison = next(t for t in ui.tables if t and "Estimated saving (%)" in t[0])
        self.assertEqual(len(comparison), 2)
        self.assertEqual({r["Case"] for r in comparison}, {"SECOND", "TEST-MOD-REV-001"})

    def test_mixed_currency_does_not_compare_costs(self):
        a, b = report(), report(case="SECOND", document_id=8, currency_symbol="€")
        _, ui, _ = render(reports=[a, b], selections={"KPI to compare": "Annual electricity cost"})
        comparison = next(t for t in ui.tables if t and "Estimated saving (%)" in t[0])
        self.assertEqual(set(comparison[0]), {"Case", "Document", "Evidence", "Estimated saving (%)"})
        self.assertTrue(any(k == "warning" and "different currencies" in m
                            for k, m in ui.messages))

    def test_matching_currency_compares_annual_cost(self):
        _, ui, _ = render(reports=[report(), report(case="SECOND", document_id=8)],
                          selections={"KPI to compare": "Annual electricity cost"})
        comparison = next(t for t in ui.tables if t and "Estimated saving (%)" in t[0])
        self.assertEqual(comparison[0]["Difference ($/year)"], "900")

    def test_missing_currency_blocks_comparison(self):
        page, _, _ = render(reports=[])
        good = report(currency_symbol="")
        self.assertFalse(page.comparable_unit("annual_cost", [good])[0])

    def test_mismatched_lifetimes_block_lifetime_costs(self):
        page, _, _ = render(reports=[])
        ok, _ = page.comparable_unit("lifetime_cost", [report(), report(lifetime_years=30)])
        self.assertFalse(ok)

    def test_missing_lifetime_blocks_lifetime_costs(self):
        page, _, _ = render(reports=[])
        self.assertFalse(page.comparable_unit("lifetime_cost", [report(lifetime_years=None)])[0])

    def test_different_lifetimes_block_operational_carbon(self):
        page, _, _ = render(reports=[])
        self.assertFalse(page.comparable_unit("operational_co2e", [
            report(), report(lifetime_years=50)])[0])

    def test_missing_comparison_values_display_na(self):
        a = report(existing_regen=None)
        b = report(case="SECOND", document_id=8)
        _, ui, _ = render(reports=[a, b], selections={"KPI to compare": "Regenerative energy"})
        comparison = next(t for t in ui.tables if t and "Estimated saving (%)" in t[0])
        self.assertEqual(comparison[0]["Existing (kWh/year)"], "N/A")
        self.assertEqual(comparison[0]["Difference (kWh/year)"], "N/A")

    def test_missing_details_values_display_na(self):
        _, ui, _ = render(reports=[report(existing_energy=None, proposed_energy=None,
                                          existing_annual_cost=None, proposed_annual_cost=None,
                                          calculated_saving=None)])
        self.assertTrue(ui.metrics["Estimated saving"].startswith("N/A"))
        self.assertIn("N/A", ui.metrics["Estimated annual energy reduction"])
        self.assertIn("N/A", ui.metrics["Estimated annual cost reduction"])

    def test_nonfinite_display_is_na(self):
        page, _, _ = render(reports=[])
        for item in (Decimal("NaN"), Decimal("Infinity"), float("inf")):
            self.assertEqual(page.display(item), "N/A")

    def test_difference_requires_both_values(self):
        page, _, _ = render(reports=[])
        self.assertIsNone(page.difference(None, 1))
        self.assertEqual(page.difference(5, 3), 2)

    def test_decimal_display_retains_precision(self):
        page, _, _ = render(reports=[])
        self.assertEqual(page.display(Decimal("4213.200")), "4,213.2")

    def test_no_comparison_chart_or_summation(self):
        _, ui, _ = render(reports=[report(), report(case="SECOND", document_id=8)])
        self.assertEqual(ui.charts, [])
        self.assertTrue(any("not added together" in m for k, m in ui.messages if k == "caption"))

    def test_iso_csv_separate_from_mod_csv(self):
        _, ui, _ = render(iso_reports=[iso()])
        self.assertEqual([d["filename"] for d in ui.downloads],
                         ["unified_approved_mod_estimates.csv", "unified_iso_excel_summary.csv"])

    def test_no_iso_export_when_missing(self):
        _, ui, _ = render()
        self.assertEqual(len(ui.downloads), 1)

    def test_export_contains_both_reports(self):
        _, ui, _ = render(reports=[report(), report(case="SECOND", document_id=8)])
        csv = ui.downloads[0]["data"].decode("utf-8-sig")
        self.assertIn("TEST-MOD-REV-001", csv)
        self.assertIn("SECOND", csv)

    def test_metric_unit_labels(self):
        page, _, _ = render(reports=[])
        example = report()
        self.assertEqual(page.metric_unit("annual_cost", example), "$/year")
        self.assertEqual(page.metric_unit("operational_co2e", example), "kg CO₂e over 25 years")


class TestISOPage(unittest.TestCase):
    def test_iso_is_visible_without_mod_estimate(self):
        _, ui, _ = render(reports=[], iso_reports=[iso()])
        self.assertEqual(ui.metrics["Cases with ISO Excel"], 1)
        self.assertTrue(any(k == "subheader" and "ISO Excel" in m for k, m in ui.messages))
        self.assertEqual(len(ui.downloads), 1)

    def test_iso_before_after_energy_and_class(self):
        _, ui, _ = render(iso_reports=[iso()])
        row = ui.tables[-1][0]
        self.assertEqual(row["Before (kWh/year)"], "41,933.05")
        self.assertEqual(row["After (kWh/year)"], "22,084.07")
        self.assertEqual(row["Before class"], "C")
        self.assertEqual(row["After class"], "A")

    def test_iso_saving_is_estimated_not_metered(self):
        _, ui, _ = render(iso_reports=[iso()])
        self.assertEqual(ui.tables[-1][0]["ISO estimated saving (%)"], "47.33")
        self.assertTrue(any("not verified meter readings" in m
                            for k, m in ui.messages if k == "caption"))

    def test_iso_missing_after_energy_is_na(self):
        _, ui, _ = render(iso_reports=[iso(values={"before_annual_energy_kwh": 100})])
        self.assertEqual(ui.tables[-1][0]["ISO estimated saving (%)"], "N/A")

    def test_iso_zero_before_is_na(self):
        _, ui, _ = render(iso_reports=[iso(values={
            "before_annual_energy_kwh": Decimal("0"),
            "after_annual_energy_kwh": Decimal("60")})])
        self.assertEqual(ui.tables[-1][0]["ISO estimated saving (%)"], "N/A")

    def test_iso_wh_converts_to_kwh(self):
        _, ui, _ = render(iso_reports=[iso(values={
            "before_annual_energy_wh": Decimal("1000000"),
            "after_annual_energy_wh": Decimal("700000")})])
        row = ui.tables[-1][0]
        self.assertEqual(row["Before (kWh/year)"], "1,000.00")
        self.assertEqual(row["After (kWh/year)"], "700.00")
        self.assertEqual(row["ISO estimated saving (%)"], "30.00")

    def test_iso_pending_is_not_approved(self):
        _, ui, _ = render(iso_reports=[iso(review_status="pending")])
        self.assertEqual(ui.tables[-1][0]["Review status"], "Pending")
        self.assertEqual(ui.metrics["Both comparisons approved"], 0)

    def test_iso_rejected_is_not_approved(self):
        _, ui, _ = render(iso_reports=[iso(review_status="rejected")])
        self.assertEqual(ui.tables[-1][0]["Review status"], "Rejected")
        self.assertEqual(ui.metrics["Both comparisons approved"], 0)

    def test_iso_comparison_approval_remains_distinct(self):
        _, ui, _ = render(iso_reports=[iso()])
        self.assertEqual(ui.tables[-1][0]["Review scope"], "comparison_only")
        self.assertEqual(ui.tables[-1][0]["Evidence class"], "test_fixture")

    def test_two_iso_workbooks_one_row_per_case(self):
        _, ui, _ = render(statuses=[status("A", 1), status("B", 2)],
                          reports=[], iso_reports=[iso("A"), iso("B", document_id=13)])
        self.assertEqual(ui.metrics["Cases with ISO Excel"], 2)
        self.assertEqual(len(ui.tables[-1]), 2)

    def test_iso_summary_csv_keeps_case_label(self):
        _, ui, _ = render(iso_reports=[iso()])
        exported = ui.downloads[-1]["data"].decode("utf-8-sig")
        self.assertIn("TEST-MOD-REV-001", exported)
        self.assertIn("Evidence class", exported)

    def test_iso_status_filter_does_not_hide_summary(self):
        _, ui, _ = render(statuses=[status("A", 1, "pending"), status("B", None)],
                          reports=[], iso_reports=[iso("A")], selections={
                              "Filter MOD review status": "No unified MOD PDF"})
        self.assertEqual([r["Case"] for r in ui.tables[0]], ["B"])
        self.assertEqual(ui.tables[-1][0]["Case"], "A")

    def test_iso_sql_latest_and_source_scoped(self):
        sql = repo.ISO_SQL.lower()
        self.assertIn("max(newest.document_id)", sql)
        self.assertIn("order by revision_no desc limit 1", sql)
        self.assertIn("d.document_type = 'iso_excel'", sql)
        self.assertIn("v.field_name = any(%s)", sql)


class TestAccessAndSource(unittest.TestCase):
    def test_non_admin_has_no_repository_calls(self):
        _, ui, calls = render(role="Requester")
        self.assertIn(("error", "Admin access is required"), ui.messages)
        for fn in calls.values():
            fn.assert_not_called()

    def test_status_query_error_prevents_partial_display(self):
        _, ui, _ = render(fail_at="statuses")
        self.assertEqual(ui.tables, [])
        self.assertTrue(any(k == "error" and "DB unavailable" in m for k, m in ui.messages))

    def test_mod_query_error_prevents_partial_display(self):
        _, ui, _ = render(fail_at="reports")
        self.assertEqual(ui.tables, [])
        self.assertTrue(any(k == "error" and "DB unavailable" in m for k, m in ui.messages))

    def test_iso_query_error_prevents_partial_display(self):
        _, ui, _ = render(fail_at="iso")
        self.assertEqual(ui.tables, [])
        self.assertTrue(any(k == "error" and "DB unavailable" in m for k, m in ui.messages))

    def test_page_has_no_legacy_table_imports(self):
        code = PAGE.read_text()
        self.assertIn("services.unified_kpi_reporting", code)
        self.assertNotIn("from database import", code)
        for legacy in ("source_documents", "extraction_revisions", "extraction_candidates",
                       "iso_assessments", "iso_documents", "iso_extracted_values",
                       "enercal_approved_kpis_view"):
            self.assertNotIn(legacy, code)

    def test_repository_has_no_legacy_sources_or_writes(self):
        sql = "\n".join((repo.STATUS_SQL, repo.MOD_SQL, repo.ISO_SQL)).lower()
        for legacy in ("source_documents", "extraction_revisions", "extraction_candidates",
                       "iso_assessments", "iso_documents", "iso_extracted_values",
                       "enercal_approved_kpis_view"):
            self.assertNotIn(legacy, sql)
        for table in ("modernisation_cases", "project_documents",
                      "document_revisions", "engineering_values"):
            self.assertIn(table, sql)
        self.assertNotIn("update ", sql)
        self.assertNotIn("delete ", sql)
        self.assertNotIn("file_content", sql)

    def test_sql_uses_latest_document_and_revision(self):
        self.assertIn("MAX(newest.document_id)", repo.MOD_SQL)
        self.assertIn("MAX(latest.revision_no)", repo.MOD_SQL)
        self.assertIn("r.review_status = 'approved'", repo.MOD_SQL)

    def test_evidence_fields_preserved_in_export(self):
        _, ui, _ = render()
        csv = ui.downloads[0]["data"].decode("utf-8-sig")
        self.assertIn("source_classification", csv)
        self.assertIn("review_scope", csv)
        self.assertIn("real_verified", csv)


if __name__ == "__main__":
    unittest.main()
