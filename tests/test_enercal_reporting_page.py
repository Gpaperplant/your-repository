
"""Test EnerCal reporting with simulated Streamlit and PostgreSQL results."""

import importlib.util
import sys
import unittest
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import pandas as pd


PAGE_PATH = (
    Path(__file__).resolve().parents[1]
    / "enercal"
    / "enercal_reporting.py"
)


class PageStopped(Exception):
    pass


class FakeStreamlit(ModuleType):
    # Record displayed results without starting a Streamlit server.
    def __init__(self, role="Admin"):
        super().__init__("streamlit")
        self.session_state = {"role": role}
        self.messages = []
        self.metrics = {}
        self.tables = []
        self.charts = []
        self.downloads = []

    def stop(self):
        raise PageStopped()

    def error(self, message):
        self.messages.append(("error", str(message)))

    def info(self, message):
        self.messages.append(("info", str(message)))

    def caption(self, message):
        self.messages.append(("caption", str(message)))

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

    def selectbox(self, label, options, **kwargs):
        return options[0]

    def tabs(self, titles):
        return [nullcontext() for _ in titles]

    def expander(self, label):
        return nullcontext()

    def download_button(
        self, label, data, file_name, mime, **kwargs
    ):
        self.downloads.append({
            "label": label,
            "data": data,
            "filename": file_name,
        })


class FakeCursor:
    # Return matching results for the three reporting-page queries.
    def __init__(self, statuses, approved, metadata):
        self.statuses = statuses
        self.approved = approved
        self.metadata = metadata
        self.current_rows = []
        self.description = []
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params=None):
        self.queries.append((query, params))
        normalized = " ".join(query.lower().split())

        if "from modernisation_cases m" in normalized:
            rows = self.statuses

        elif "from enercal_approved_kpis_view" in normalized:
            rows = self.approved

        elif "from enercal_specifications" in normalized:
            ids = set(params[0])
            names = set(params[1])

            self.current_rows = [
                row for row in self.metadata
                if row[0] in ids and row[1] in names
            ]

            self.description = [
                SimpleNamespace(name=n)
                for n in (
                    "document_id",
                    "field_name",
                    "field_value",
                )
            ]
            return

        else:
            raise AssertionError(
                f"Unexpected reporting SQL: {query}"
            )

        self.description = [
            SimpleNamespace(name=n) for n in rows[0]
        ] if rows else []

        self.current_rows = [
            tuple(row.values()) for row in rows
        ]

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
        "filename": (
            f"EnerCal_{case}.pdf"
            if document is not None else None
        ),
        "revision_no": (
            1 if document is not None else None
        ),
        "review_status": review,
    }


def approved_role(
    case, document, role, saving=Decimal("45")
):
    # Use invented report values exclusively within these tests.
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
        "elevator_technology": (
            "Old" if before else "New"
        ),
        "annual_energy_kwh_year": Decimal(
            "10000" if before else "5500"
        ),
        "regenerative_energy_kwh_year": Decimal(
            "100" if before else "200"
        ),
        "annual_electricity_cost_per_year": Decimal(
            "2000" if before else "1100"
        ),
        "lifetime_electricity_cost": Decimal(
            "50000" if before else "27500"
        ),
        "operational_co2e_kg_lifetime": Decimal(
            "30000" if before else "17000"
        ),
        "embodied_co2e_kg": Decimal(
            "4000" if before else "5000"
        ),
    }


def render(
    statuses=(),
    approved=(),
    metadata=(),
    role="Admin",
):
    # Import and run the actual page with in-memory data only.
    ui = FakeStreamlit(role)

    cursor = FakeCursor(
        list(statuses),
        list(approved),
        list(metadata),
    )

    fake_database = ModuleType("database")
    fake_database.get_connection = (
        lambda: FakeConnection(cursor)
    )

    spec = importlib.util.spec_from_file_location(
        "_test_enercal_reporting_page",
        PAGE_PATH,
    )
    page = importlib.util.module_from_spec(spec)

    with patch.dict(
        sys.modules,
        {
            "streamlit": ui,
            "database": fake_database,
        },
    ):
        try:
            spec.loader.exec_module(page)
        except PageStopped:
            pass

    return page, ui, cursor


class ReportingPageTests(unittest.TestCase):

    def test_six_cases_five_pending_and_one_missing(self):
        statuses = [
            status(f"MOD100{i}", 52 + i, "pending")
            for i in range(1, 6)
        ]
        statuses.append(status("MOD1006"))

        _, ui, cursor = render(statuses)

        self.assertEqual(
            ui.metrics["Modernisation cases"], 6
        )
        self.assertEqual(
            ui.metrics["Without PDF"], 1
        )
        self.assertEqual(
            ui.metrics["Pending reports"], 5
        )
        self.assertEqual(
            ui.metrics["Approved KPI reports"], 0
        )

        self.assertEqual(len(ui.tables[0]), 6)
        self.assertEqual(
            ui.tables[0][-1]["Engineering status"],
            "No PDF uploaded",
        )

        self.assertFalse(ui.charts)
        self.assertFalse(ui.downloads)

        self.assertFalse(
            any(
                "enercal_specifications" in sql.lower()
                for sql, _ in cursor.queries
            )
        )

    def test_completely_empty_database_is_handled(self):
        _, ui, _ = render()

        self.assertEqual(
            ui.metrics["Modernisation cases"], 0
        )
        self.assertTrue(
            any(
                "No modernisation cases" in message
                for _, message in ui.messages
            )
        )
        self.assertFalse(ui.downloads)

    def test_non_admin_is_blocked_before_database(self):
        _, ui, cursor = render(role="Requester")

        self.assertFalse(cursor.queries)
        self.assertTrue(
            any(
                kind == "error"
                for kind, _ in ui.messages
            )
        )

    def test_two_approved_reports_have_charts_and_export(self):
        rows = [
            approved_role(case, document, role)
            for case, document in (
                ("MOD1001", 53),
                ("MOD1002", 54),
            )
            for role in ("existing", "proposed")
        ]

        statuses = [
            status("MOD1001", 53, "approved"),
            status("MOD1002", 54, "approved"),
            status("MOD1003", 55, "pending"),
        ]

        _, ui, _ = render(
            statuses,
            rows,
            [
                (
                    53,
                    "contact_email",
                    "private@example.test",
                )
            ],
        )

        self.assertEqual(
            ui.metrics["Approved KPI reports"], 2
        )
        self.assertEqual(len(ui.charts), 2)
        self.assertEqual(len(ui.downloads), 1)

        csv_data = (
            ui.downloads[0]["data"]
            .decode("utf-8-sig")
        )

        self.assertIn("MOD1001", csv_data)
        self.assertIn("MOD1002", csv_data)
        self.assertNotIn("MOD1003", csv_data)
        self.assertNotIn(
            "private@example.test", csv_data
        )
        self.assertNotIn("contact_email", csv_data)

    def test_one_role_does_not_count_as_approved_kpi_report(self):
        incomplete = [
            approved_role("MOD1001", 53, "existing")
        ]

        _, ui, _ = render(
            [status("MOD1001", 53, "approved")],
            incomplete,
        )

        self.assertEqual(
            ui.metrics["Approved KPI reports"], 0
        )
        self.assertEqual(
            ui.tables[0][0]["Engineering status"],
            "Approved (KPI data unavailable)",
        )
        self.assertFalse(ui.downloads)

    def test_report_units_and_lifetime_are_not_hardcoded(self):
        rows = [
            approved_role("MOD1001", 53, role)
            for role in ("existing", "proposed")
        ]

        page, _, _ = render(
            [status("MOD1001", 53, "approved")],
            rows,
        )

        example = {
            "currency_symbol": "$",
            "lifetime_years": 25,
        }

        self.assertEqual(
            page.metric_unit("annual_cost", example),
            "$/year",
        )
        self.assertEqual(
            page.metric_unit("operational_co2e", example),
            "kg CO₂e over 25 years",
        )

    def test_missing_values_are_not_replaced_with_zero(self):
        page, _, _ = render()

        self.assertEqual(
            page.display(None),
            "N/A",
        )
        self.assertIsNone(
            page.difference(None, Decimal("12"))
        )
        self.assertEqual(
            page.display(Decimal("4213.200")),
            "4,213.2",
        )


if __name__ == "__main__":
    unittest.main()
