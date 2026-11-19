"""Test the EnerCal page with simulated Streamlit and database results."""

import importlib.util
import sys
import unittest
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

# The real page remains a single file; isolate the database with a test stub.
_fake_db = ModuleType("database")
_fake_db.get_connection = lambda: None

PAGE_PATH = Path(__file__).resolve().parents[1] / "enercal" / "enercal_reporting.py"


@contextmanager
def temporary_modules(replacements):
    # Restore only the mocked modules, preserving imported dependencies.
    missing = object()
    originals = {name: sys.modules.get(name, missing) for name in replacements}
    sys.modules.update(replacements)
    try:
        yield
    finally:
        for name, original in originals.items():
            if original is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class PageStopped(Exception):
    pass


class FakeStreamlit(ModuleType):
    def __init__(self, role="Admin", selections=None):
        super().__init__("streamlit")
        self.session_state = {"role": role}
        self.selections = selections or {}
        self.messages = []
        self.metrics = {}
        self.tables = []
        self.charts = []
        self.downloads = []
        self.spacers = []

    def stop(self):
        raise PageStopped()

    def error(self, message):
        self.messages.append(("error", str(message)))

    def info(self, message):
        self.messages.append(("info", str(message)))

    def caption(self, message):
        self.messages.append(("caption", str(message)))

    def write(self, message):
        self.messages.append(("write", str(message)))

    def subheader(self, message):
        self.messages.append(("subheader", str(message)))

    def metric(self, label, value, **kwargs):
        self.metrics[label] = value

    def columns(self, count):
        return [self] * count

    def dataframe(self, data, **kwargs):
        self.tables.append(data)

    def bar_chart(self, data, **kwargs):
        self.charts.append((data, kwargs))

    def markdown(self, body, **kwargs):
        self.spacers.append((body, kwargs))

    def selectbox(self, label, options, **kwargs):
        selection = self.selections.get(label, options[0])
        if selection not in options:
            raise AssertionError(f"Selection {selection!r} unavailable in {label!r}")
        return selection

    def download_button(self, label, data, file_name, mime, **kwargs):
        self.downloads.append({
            "label": label, "data": data, "filename": file_name,
        })


class FakeCursor:
    def __init__(self, statuses, approved, error=None, iso=()):
        self.statuses = statuses
        self.approved = approved
        self.iso = iso
        self.error = error
        self.current_rows = []
        self.description = []
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.queries.append((query, params))
        if self.error is not None:
            raise self.error
        normalized = " ".join(query.lower().split())
        if "from modernisation_cases m" in normalized:
            rows = self.statuses
        elif "from enercal_approved_kpis_view" in normalized:
            rows = self.approved
        elif "from iso_assessments a" in normalized:
            rows = self.iso
        else:
            raise AssertionError(f"Unexpected SQL: {query}")

        self.description = [SimpleNamespace(name=n) for n in rows[0]] if rows else []
        self.current_rows = [tuple(row.values()) for row in rows]

    def fetchall(self):
        return list(self.current_rows)


class FakeConnection:
    def __init__(self, cursor):
        self.cur = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self.cur


def status(case, document=None, review=None):
    return {
        "case_ref": case,
        "case_status": "registered",
        "document_id": document,
        "filename": f"EnerCal_{case}.pdf" if document is not None else None,
        "revision_no": 1 if document is not None else None,
        "review_status": review,
    }


def approved_role(case, document, role, saving=Decimal("45")):
    before = role == "existing"
    return {
        "document_id": document,
        "case_ref": case,
        "filename": f"EnerCal_{case}.pdf",
        "revision_no": 1,
        "country": "Singapore",
        "report_date": "2026-11-01",
        "currency_symbol": "$",
        "lifetime_years": 25,
        "electricity_price_per_kwh": Decimal("0.20"),
        "electricity_price_unit": "$/kWh",
        "reported_energy_saving_percent": saving,
        "calculated_energy_saving_percent": saving,
        "technology_role": role,
        "elevator_technology": "Old" if before else "New",
        "annual_energy_kwh_year": Decimal("10000" if before else "5500"),
        "regenerative_energy_kwh_year": Decimal("100" if before else "200"),
        "annual_electricity_cost_per_year": Decimal("2000" if before else "1100"),
        "lifetime_electricity_cost": Decimal("50000" if before else "27500"),
        "operational_co2e_kg_lifetime": Decimal("30000" if before else "17000"),
        "embodied_co2e_kg": Decimal("4000" if before else "5000"),
    }


def render(statuses=(), approved=(), role="Admin", selections=None,
           database_error=None, iso=()):
    """Execute the real page with stubbed I/O and optional dropdown selections."""
    ui = FakeStreamlit(role, selections)
    cursor = FakeCursor(list(statuses), list(approved), database_error, iso)
    spec = importlib.util.spec_from_file_location("_test_enercal_reporting_page", PAGE_PATH)
    page = importlib.util.module_from_spec(spec)
    with temporary_modules({
        "streamlit": ui,
        "database": _fake_db,
    }), patch.object(_fake_db, "get_connection", lambda: FakeConnection(cursor)):
        try:
            spec.loader.exec_module(page)
        except PageStopped:
            pass
    return page, ui, cursor


class ReportingPageTests(unittest.TestCase):
    def setUp(self):
        self.statuses = [
            status("MOD1001", 53, "approved"),
            status("MOD1002", 54, "approved"),
            status("MOD1003", 55, "pending"),
            status("MOD1004", 56, "pending"),
            status("MOD1005", 57, "pending"),
            status("MOD1006"),
        ]

    def test_default_overview_and_counts(self):
        _, ui, _ = render(self.statuses)
        self.assertEqual(ui.metrics["Modernisation cases"], 6)
        self.assertEqual(ui.metrics["Without PDF"], 1)
        self.assertEqual(ui.metrics["Pending reports"], 3)
        self.assertEqual(ui.metrics["Approved KPI reports"], 0)
        self.assertEqual(len(ui.tables[0]), 6)
        self.assertEqual(ui.tables[0][-1]["Engineering status"], "No PDF uploaded")

    def test_filter_pending(self):
        _, ui, _ = render(self.statuses, selections={
            "Filter engineering review status": "Pending",
        })
        self.assertEqual(
            [x["Case"] for x in ui.tables[0]],
            ["MOD1003", "MOD1004", "MOD1005"],
        )
        self.assertEqual(ui.metrics["Modernisation cases"], 6)
        self.assertEqual(ui.metrics["Pending reports"], 3)

    def test_filter_one_case_and_approved(self):
        _, ui, _ = render(self.statuses, selections={
            "Filter cases by reference": "MOD1001",
            "Filter engineering review status": "Approved (KPI data unavailable)",
        })
        self.assertEqual(len(ui.tables[0]), 1)
        self.assertEqual(ui.tables[0][0]["Document ID"], 53)

    def test_filter_approved_with_complete_kpi(self):
        approved = [
            approved_role("MOD1001", 53, role)
            for role in ("existing", "proposed")
        ]
        _, ui, _ = render(self.statuses, approved, selections={
            "Filter cases by reference": "MOD1001",
            "Filter engineering review status": "Approved",
        })
        self.assertEqual(len(ui.tables[0]), 1)
        self.assertEqual(ui.tables[0][0]["Case"], "MOD1001")
        self.assertEqual(ui.tables[0][0]["Engineering status"], "Approved")
        self.assertEqual(ui.metrics["Approved KPI reports"], 1)

    def test_multiple_pdfs_same_case_still_one_case_metric(self):
        repeated = [
            status("MOD1001", 53, "pending"),
            status("MOD1001", 54, "pending"),
            status("MOD1002", 55, "pending"),
        ]
        _, ui, _ = render(repeated)
        self.assertEqual(ui.metrics["Modernisation cases"], 2)
        self.assertEqual(ui.metrics["Pending reports"], 3)
        self.assertEqual(len(ui.tables[0]), 3)

    def test_no_matches(self):
        _, ui, _ = render(self.statuses, selections={
            "Filter cases by reference": "MOD1001",
            "Filter engineering review status": "Pending",
        })
        self.assertFalse(ui.tables)
        self.assertIn(
            ("write", "No cases match those status filters."), ui.messages
        )
        self.assertEqual(ui.metrics["Modernisation cases"], 6)

    def test_no_pdf_filter(self):
        _, ui, _ = render(self.statuses, selections={
            "Filter engineering review status": "No PDF uploaded",
        })
        self.assertEqual(len(ui.tables[0]), 1)
        self.assertEqual(ui.tables[0][0]["Case"], "MOD1006")

    def test_empty_database(self):
        _, ui, _ = render()
        self.assertEqual(ui.metrics["Modernisation cases"], 0)
        self.assertTrue(any("No modernisation cases" in m for _, m in ui.messages))
        self.assertFalse(ui.downloads)

    def test_non_admin_blocked_before_database(self):
        _, ui, cursor = render(role="Requester")
        self.assertFalse(cursor.queries)
        self.assertIn(("error", "Admin access is required"), ui.messages)

    def test_approved_reports_have_table_and_safe_export(self):
        rows = [
            approved_role(case, document, role)
            for case, document in (("MOD1001", 53), ("MOD1002", 54))
            for role in ("existing", "proposed")
        ]
        for row in rows:
            row["contact_email"] = "private@example.test"
        _, ui, _ = render(self.statuses, rows)
        self.assertEqual(ui.metrics["Approved KPI reports"], 2)
        self.assertFalse(ui.charts)
        self.assertTrue(any(
            table and isinstance(table[0], dict)
            and "Estimated saving (%)" in table[0] for table in ui.tables
        ))
        self.assertEqual(len(ui.downloads), 1)
        csv_data = ui.downloads[0]["data"].decode("utf-8-sig")
        self.assertIn("MOD1001", csv_data)
        self.assertIn("MOD1002", csv_data)
        self.assertNotIn("private@example.test", csv_data)
        self.assertNotIn("contact_email", csv_data)

    def test_incomplete_approved_report_excluded_from_kpis(self):
        incomplete = [approved_role("MOD1001", 53, "existing")]
        _, ui, _ = render(self.statuses, incomplete)
        self.assertEqual(ui.metrics["Approved KPI reports"], 0)
        self.assertEqual(
            ui.tables[0][0]["Engineering status"],
            "Approved (KPI data unavailable)",
        )

    def test_incomplete_approval_skips_specification_query(self):
        _, _, cursor = render(
            [status("MOD1001", 53, "approved")],
            [approved_role("MOD1001", 53, "existing")],
        )
        self.assertFalse(any(
            "from enercal_specifications" in query.lower()
            for query, _ in cursor.queries
        ))

    def test_unknown_review_status_label(self):
        _, ui, _ = render([status("MOD1007", 60)])
        self.assertEqual(ui.tables[0][0]["Engineering status"], "Extraction missing")

    def test_case_filter_without_status_filter(self):
        _, ui, _ = render(self.statuses, selections={
            "Filter cases by reference": "MOD1004",
        })
        self.assertEqual([row["Case"] for row in ui.tables[0]], ["MOD1004"])
        self.assertEqual(ui.metrics["Modernisation cases"], 6)

    def test_profile_and_contact_queries_are_removed(self):
        rows = [approved_role("MOD1001", 53, role) for role in ("existing", "proposed")]
        for row in rows:
            row["contact_email"] = "private@example.test"
            row["project_name"] = "Example Project"
        _, ui, cursor = render([status("MOD1001", 53, "approved")], rows)
        self.assertEqual(ui.metrics["Approved KPI reports"], 1)
        self.assertEqual(len(cursor.queries), 3)
        self.assertFalse(any(
            "enercal_specifications" in sql.lower()
            for sql, _ in cursor.queries
        ))
        export = ui.downloads[0]["data"].decode("utf-8-sig")
        self.assertNotIn("private@example.test", export)
        self.assertNotIn("contact_email", export)
        self.assertNotIn("Example Project", export)
        self.assertNotIn("project_name", export)

    def test_single_report_selector_replaces_two_dropdowns(self):
        rows = [approved_role(case, doc, role)
                for case, doc in (("MOD1001", 53), ("MOD1002", 54))
                for role in ("existing", "proposed")]
        _, ui, _ = render(self.statuses, rows, selections={
            "Choose an approved report": 54,
        })
        self.assertEqual(ui.metrics["Approved KPI reports"], 2)
        self.assertEqual(len(ui.downloads), 1)
        export = ui.downloads[0]["data"].decode("utf-8-sig")
        self.assertIn("MOD1001", export)
        self.assertIn("MOD1002", export)

    def test_mixed_currency_is_not_compared(self):
        page, _, _ = render()
        comparable, message = page.comparable_unit("annual_cost", [
            {"currency_symbol": "$", "lifetime_years": 25},
            {"currency_symbol": "€", "lifetime_years": 25},
        ])
        self.assertFalse(comparable)
        self.assertIn("different currencies", message)

    def test_verified_measurement_section_is_removed(self):
        rows = [approved_role("MOD1001", 53, role)
                for role in ("existing", "proposed")]
        _, ui, cursor = render(self.statuses, rows)
        self.assertNotIn("Eligible verified measurement pairs", ui.metrics)
        self.assertFalse(any(
            "Measured vs estimated energy savings" in message
            for _, message in ui.messages
        ))
        self.assertFalse(any(
            table and isinstance(table[0], dict) and "Pair ID" in table[0]
            for table in ui.tables
        ))
        self.assertFalse(any(
            "verified_measurement_pairs" in query.lower()
            for query, _ in cursor.queries
        ))

    def test_section_spacing_for_approved_reports(self):
        rows = [approved_role(case, doc, role)
                for case, doc in (("MOD1001", 53), ("MOD1002", 54))
                for role in ("existing", "proposed")]
        _, ui, _ = render(self.statuses, rows)
        self.assertEqual(len(ui.spacers), 2)
        for html, props in ui.spacers:
            self.assertIn("height: 2rem", html)
            self.assertTrue(props["unsafe_allow_html"])

    def test_single_report_has_one_section_gap(self):
        rows = [approved_role("MOD1001", 53, role)
                for role in ("existing", "proposed")]
        _, ui, _ = render(self.statuses, rows)
        self.assertEqual(len(ui.spacers), 1)

    def test_units_and_missing_values_are_preserved(self):
        page, _, _ = render()
        self.assertEqual(
            page.metric_unit("annual_cost", {"currency_symbol": "$", "lifetime_years": 25}),
            "$/year",
        )
        self.assertEqual(
            page.metric_unit("operational_co2e", {"currency_symbol": "$", "lifetime_years": 25}),
            "kg CO₂e over 25 years",
        )
        self.assertEqual(page.display(None), "N/A")
        self.assertIsNone(page.difference(None, Decimal("12")))
        self.assertEqual(page.display(Decimal("4213.200")), "4,213.2")



    def test_unreviewed_pdf_has_extraction_missing_status(self):
        _, ui, _ = render([status("MOD1007", 58)])
        self.assertEqual(ui.tables[0][0]["Engineering status"], "Extraction missing")

    def test_nonfinite_values_are_not_displayed_as_numbers(self):
        page, _, _ = render()
        self.assertEqual(page.display(Decimal("NaN")), "N/A")
        self.assertEqual(page.display(float("inf")), "N/A")
        self.assertEqual(page.display(Decimal("-Infinity")), "N/A")

    def test_lifetime_cost_requires_matching_years(self):
        page, _, _ = render()
        comparable, reason = page.comparable_unit("lifetime_cost", [
            {"currency_symbol": "$", "lifetime_years": 20},
            {"currency_symbol": "$", "lifetime_years": 25},
        ])
        self.assertFalse(comparable)
        self.assertTrue(reason)

    def test_missing_lifetime_blocks_lifetime_cost_comparison(self):
        page, _, _ = render()
        comparable, reason = page.comparable_unit("lifetime_cost", [
            {"currency_symbol": "$", "lifetime_years": None},
            {"currency_symbol": "$", "lifetime_years": None},
        ])
        self.assertFalse(comparable)
        self.assertTrue(reason)

    def test_missing_currency_blocks_cost_comparison(self):
        page, _, _ = render()
        comparable, reason = page.comparable_unit("annual_cost", [
            {"currency_symbol": "", "lifetime_years": 20},
            {"currency_symbol": "", "lifetime_years": 20},
        ])
        self.assertFalse(comparable)
        self.assertTrue(reason)

    def test_unexpected_technology_role_is_ignored(self):
        roles = [
            approved_role("MOD1001", 53, role)
            for role in ("existing", "proposed", "unknown")
        ]
        _, ui, _ = render(self.statuses, roles)
        self.assertEqual(ui.metrics["Approved KPI reports"], 1)

    def test_mixed_currency_does_not_create_cost_chart(self):
        rows = [
            approved_role(case, doc, role)
            for case, doc in (("MOD1001", 53), ("MOD1002", 54))
            for role in ("existing", "proposed")
        ]
        for row in rows:
            if row["document_id"] == 54:
                row["currency_symbol"] = "€"
        _, ui, _ = render(self.statuses, rows, selections={
            "Choose a sustainability KPI": "Annual electricity cost",
        })
        self.assertFalse(ui.charts)
        self.assertTrue(any("different currencies" in msg for kind, msg in ui.messages if kind == "write"))

    def test_missing_comparison_values_do_not_create_kpi_chart(self):
        rows = [
            approved_role(case, doc, role)
            for case, doc in (("MOD1001", 53), ("MOD1002", 54))
            for role in ("existing", "proposed")
        ]
        for row in rows:
            row["annual_energy_kwh_year"] = None
        _, ui, _ = render(self.statuses, rows)
        self.assertFalse(ui.charts)
        self.assertTrue(any("No usable values" in msg for kind, msg in ui.messages if kind == "write"))

    def test_no_reports_is_not_comparable(self):
        page, _, _ = render()
        self.assertEqual(page.comparable_unit("energy", []), (False, ""))

    def test_database_error_does_not_show_partial_results(self):
        _, ui, _ = render(self.statuses, database_error=RuntimeError("database unavailable"))
        self.assertTrue(any("database unavailable" in msg for kind, msg in ui.messages if kind == "error"))
        self.assertFalse(ui.tables)

    def test_valid_kpis_keep_units(self):
        rows = [approved_role("MOD1001", 53, role) for role in ("existing", "proposed")]
        _, ui, _ = render(self.statuses, rows)
        self.assertEqual(ui.metrics["Calculated energy saving"], "45%")
        self.assertEqual(ui.metrics["Annual energy reduction"], "4,500 kWh/year")
        self.assertEqual(ui.metrics["Annual electricity cost reduction"], "900 $")

    def test_missing_savings_are_not_charted(self):
        rows = [
            approved_role(case, doc, role)
            for case, doc in (("MOD1001", 53), ("MOD1002", 54))
            for role in ("existing", "proposed")
        ]
        for row in rows:
            row["calculated_energy_saving_percent"] = None
        _, ui, _ = render(self.statuses, rows)
        self.assertFalse(ui.charts)

    def test_missing_numeric_estimates_show_na_without_units(self):
        rows = [approved_role("MOD1001", 53, role) for role in ("existing", "proposed")]
        for row in rows:
            row["calculated_energy_saving_percent"] = None
            row["annual_energy_kwh_year"] = None
            row["annual_electricity_cost_per_year"] = None
        _, ui, _ = render(self.statuses, rows)
        self.assertEqual(ui.metrics["Calculated energy saving"], "N/A")
        self.assertEqual(ui.metrics["Annual energy reduction"], "N/A")
        self.assertEqual(ui.metrics["Annual electricity cost reduction"], "N/A")
        self.assertFalse(ui.charts)

    def test_reporting_queries_keep_missing_cases_and_approved_view(self):
        _, _, cursor = render([status("MOD1006")])
        queries = "\n".join(sql.lower() for sql, _ in cursor.queries)
        self.assertIn("left join source_documents", queries)
        self.assertIn("left join lateral", queries)
        self.assertIn("enercal_approved_kpis_view", queries)
        self.assertNotIn("from enercal_specifications", queries)


    def test_comparison_table_preserves_report_values_and_units(self):
        rows = [
            approved_role(case, doc, role)
            for case, doc in (("MOD1001", 53), ("MOD1002", 54))
            for role in ("existing", "proposed")
        ]
        _, ui, _ = render(self.statuses, rows)
        comparisons = [
            table for table in ui.tables
            if table and "Estimated saving (%)" in table[0]
        ]
        self.assertEqual(len(comparisons), 1)
        self.assertEqual([row["Case"] for row in comparisons[0]], ["MOD1001", "MOD1002"])
        self.assertEqual(comparisons[0][0]["Existing (kWh/year)"], "10,000")
        self.assertEqual(comparisons[0][0]["Proposed (kWh/year)"], "5,500")
        self.assertEqual(comparisons[0][0]["Difference (kWh/year)"], "4,500")
        self.assertFalse(ui.charts)

    def test_mixed_currency_only_compares_saving_percentages(self):
        rows = [
            approved_role(case, doc, role)
            for case, doc in (("MOD1001", 53), ("MOD1002", 54))
            for role in ("existing", "proposed")
        ]
        for row in rows:
            if row["document_id"] == 54:
                row["currency_symbol"] = "€"
        _, ui, _ = render(self.statuses, rows, selections={
            "Choose a sustainability KPI": "Annual electricity cost",
        })
        comparisons = [
            table for table in ui.tables
            if table and "Estimated saving (%)" in table[0]
        ]
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(set(comparisons[0][0]), {"Case", "Report", "Estimated saving (%)"})
        self.assertTrue(any("different currencies" in msg for kind, msg in ui.messages if kind == "write"))
        self.assertFalse(ui.charts)




class ISOReportingTests(unittest.TestCase):
    def setUp(self):
        self.statuses = [status("MOD1001", 53, "approved"), status("MOD1002")]

    @staticmethod
    def iso_field(case="MOD1001", field=None, numeric=None, text=None, assessment=4, review="pending"):
        return {
            "case_ref": case,
            "assessment_id": assessment,
            "lift_id": "OP13 (L)",
            "imported_at": "2026-11-19",
            "review_status": review,
            "filename": "ISO calculation 21Apr25-lot 91.xlsx",
            "field_name": field,
            "numeric_value": numeric,
            "value_text": text,
        }

    def rows(self):
        return [
            self.iso_field(field="before_annual_energy_kwh", numeric=Decimal("41933.0484")),
            self.iso_field(field="after_annual_energy_kwh", numeric=Decimal("22084.06994")),
            self.iso_field(field="before_efficiency_class", text="C"),
            self.iso_field(field="after_efficiency_class", text="A"),
        ]

    def test_iso_upload_is_visible_in_case_overview(self):
        _, ui, _ = render(self.statuses, iso=self.rows())
        self.assertEqual(ui.metrics["Cases with ISO Excel"], 1)
        self.assertIn("ISO calculation", ui.tables[0][0]["ISO Excel"])
        self.assertEqual(ui.tables[0][0]["ISO status"], "Pending")
        self.assertEqual(ui.tables[0][1]["ISO status"], "Not uploaded")
        self.assertEqual(ui.tables[0][0]["Engineering status"], "Approved (KPI data unavailable)")

    def test_iso_section_works_without_approved_enercal(self):
        _, ui, _ = render(self.statuses, iso=self.rows())
        self.assertEqual(len(ui.tables), 2)
        self.assertEqual(ui.tables[1][0]["Case"], "MOD1001")
        self.assertTrue(any("ISO Excel energy assessments" == m for k, m in ui.messages if k == "subheader"))
        self.assertTrue(any("No complete engineering-approved EnerCal" in m for k, m in ui.messages))
        self.assertEqual(len(ui.spacers), 1)

    def test_iso_energy_classes_saving_and_csv(self):
        _, ui, _ = render(self.statuses, iso=self.rows())
        entry = ui.tables[1][0]
        self.assertEqual(entry["Before (kWh/year)"], "41,933.05")
        self.assertEqual(entry["After (kWh/year)"], "22,084.07")
        self.assertEqual(entry["Before class"], "C")
        self.assertEqual(entry["After class"], "A")
        self.assertEqual(entry["ISO saving (%)"], "47.33")
        self.assertIn("MOD1001", ui.downloads[0]["data"].decode("utf-8-sig"))
        self.assertEqual(ui.downloads[0]["filename"], "iso_excel_assessments.csv")

    def test_iso_without_before_after_shows_no_saving(self):
        _, ui, _ = render(self.statuses, iso=[self.iso_field(field="annual_energy_wh", numeric=Decimal("4000000"))])
        entry = ui.tables[1][0]
        self.assertEqual(entry["ISO saving (%)"], "N/A")
        self.assertEqual(entry["Single-column energy (kWh/year)"], "4,000.00")

    def test_missing_or_zero_before_does_not_divide_by_zero(self):
        _, ui, _ = render(self.statuses, iso=[
            self.iso_field(field="before_annual_energy_kwh", numeric=Decimal("0")),
            self.iso_field(field="after_annual_energy_kwh", numeric=Decimal("120")),
        ])
        self.assertEqual(ui.tables[1][0]["ISO saving (%)"], "N/A")

    def test_fallback_to_wh_when_kwh_missing(self):
        _, ui, _ = render(self.statuses, iso=[
            self.iso_field(field="before_annual_energy_wh", numeric=Decimal("1000000")),
            self.iso_field(field="after_annual_energy_wh", numeric=Decimal("700000")),
        ])
        entry = ui.tables[1][0]
        self.assertEqual(entry["Before (kWh/year)"], "1,000.00")
        self.assertEqual(entry["After (kWh/year)"], "700.00")
        self.assertEqual(entry["ISO saving (%)"], "30.00")

    def test_iso_is_not_labeled_approved_and_enercal_csv_stays_separate(self):
        ener = [approved_role("MOD1001", 53, role) for role in ("existing", "proposed")]
        _, ui, _ = render(self.statuses, ener, iso=self.rows())
        self.assertEqual(ui.metrics["Approved KPI reports"], 1)
        self.assertEqual(ui.metrics["Cases with ISO Excel"], 1)
        self.assertEqual(len(ui.downloads), 2)
        self.assertEqual(ui.downloads[0]["filename"], "enercal_approved_kpis.csv")
        self.assertEqual(ui.downloads[1]["filename"], "iso_excel_assessments.csv")
        self.assertEqual(ui.tables[-1][0]["Evidence status"], "Pending")
        self.assertEqual(len(ui.spacers), 2)

    def test_sql_uses_latest_assessment_and_scoped_fields(self):
        _, _, cursor = render(self.statuses, iso=self.rows())
        sql, params = cursor.queries[-1]
        self.assertIn("latest.case_id = a.case_id", sql)
        self.assertIn("ORDER BY latest.imported_at DESC, latest.assessment_id DESC", sql)
        self.assertIn("d.document_type = 'iso_excel'", sql)
        self.assertIn("before_annual_energy_kwh", params[0])
        self.assertNotIn("pdf_sha256", sql)

    def test_one_iso_assessment_per_case_not_per_field(self):
        rows = self.rows() + [self.iso_field(case="MOD1002", field="annual_energy_kwh", numeric=Decimal("200"), assessment=5)]
        _, ui, _ = render(self.statuses, iso=rows)
        self.assertEqual(ui.metrics["Cases with ISO Excel"], 2)
        self.assertEqual(len(ui.tables[1]), 2)

    def test_pdf_status_filter_does_not_hide_iso_summary(self):
        _, ui, _ = render(self.statuses, iso=self.rows(), selections={
            "Filter engineering review status": "No PDF uploaded",
        })
        self.assertEqual([r["Case"] for r in ui.tables[0]], ["MOD1002"])
        self.assertEqual(ui.tables[1][0]["Case"], "MOD1001")

    def test_ready_requires_both_approvals_for_same_case(self):
        approved_iso = [{**row, "review_status": "approved"} for row in self.rows()]
        _, ui, _ = render(self.statuses, iso=approved_iso)
        self.assertEqual(ui.metrics["Ready cases"], 1)
        self.assertEqual(ui.tables[0][0]["Case readiness"], "Ready")
        self.assertEqual(ui.tables[0][1]["Case readiness"], "Not ready")
        self.assertEqual(ui.tables[0][0]["ISO status"], "Approved")
        self.assertEqual(ui.tables[-1][0]["Evidence status"], "Approved")
        self.assertEqual(ui.tables[-1][0]["Case readiness"], "Ready")

    def test_only_mod_approved_is_not_ready(self):
        _, ui, _ = render(self.statuses, iso=self.rows())
        self.assertEqual(ui.metrics["Ready cases"], 0)
        self.assertEqual(ui.tables[0][0]["Case readiness"], "Not ready")

    def test_only_iso_approved_is_not_ready(self):
        pending_mod = [status("MOD1001", 53, "pending")]
        approved_iso = [{**row, "review_status": "approved"} for row in self.rows()]
        _, ui, _ = render(pending_mod, iso=approved_iso)
        self.assertEqual(ui.metrics["Ready cases"], 0)
        self.assertEqual(ui.tables[0][0]["Case readiness"], "Not ready")

    def test_newer_pending_mod_overrides_older_approved_mod(self):
        newer = [status("MOD1001", 53, "approved"), status("MOD1001", 54, "pending")]
        approved_iso = [{**row, "review_status": "approved"} for row in self.rows()]
        _, ui, _ = render(newer, iso=approved_iso)
        self.assertEqual(ui.metrics["Ready cases"], 0)
        self.assertEqual({r["Case readiness"] for r in ui.tables[0]}, {"Not ready"})

    def test_newer_pending_iso_resets_ready_case(self):
        _, ui, _ = render(self.statuses, iso=self.rows())
        self.assertEqual(ui.metrics["Ready cases"], 0)
        self.assertEqual(ui.tables[0][0]["ISO status"], "Pending")

    def test_rejected_iso_does_not_grant_ready(self):
        rejected = [{**row, "review_status": "rejected"} for row in self.rows()]
        _, ui, _ = render(self.statuses, iso=rejected)
        self.assertEqual(ui.metrics["Ready cases"], 0)
        self.assertEqual(ui.tables[0][0]["ISO status"], "Rejected")

    def test_no_iso_never_ready(self):
        _, ui, _ = render(self.statuses)
        self.assertEqual(ui.metrics["Ready cases"], 0)
        self.assertEqual(ui.tables[0][0]["Case readiness"], "Not ready")

    def test_query_failure_blocks_partial_display(self):
        _, ui, _ = render(self.statuses, iso=self.rows(), database_error=RuntimeError("ISO database offline"))
        self.assertFalse(ui.tables)
        self.assertTrue(any("ISO database offline" in m for k, m in ui.messages if k == "error"))


if __name__ == "__main__":
    unittest.main()
