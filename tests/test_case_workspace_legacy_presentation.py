"""Legacy-friendly Case Workspace presentation, backed by five-table snapshots.

The test compiles only pure view helpers so it does not require a Streamlit
server or a PostgreSQL connection. Existing review-action tests cover writes.
"""

import ast
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

PAGE = Path(__file__).resolve().parents[1] / "enercal" / "enercal_review.py"


class FakeStreamlit:
    def __init__(self):
        self.tables = []
        self.metrics = []
        self.charts = []
        self.messages = []
        self.selects = []
        self.downloads = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def subheader(self, value):
        self.messages.append(("subheader", value))

    def caption(self, value):
        self.messages.append(("caption", value))

    def write(self, value):
        self.messages.append(("write", value))

    def warning(self, value):
        self.messages.append(("warning", value))

    def columns(self, n, **kwargs):
        return [self] * (n if isinstance(n, int) else len(n))

    def metric(self, label, value, **kwargs):
        self.metrics.append((label, value))

    def dataframe(self, rows, **kwargs):
        self.tables.append(rows)

    def selectbox(self, label, options, **kwargs):
        self.selects.append((label, list(options)))
        return list(options)[0]

    def bar_chart(self, data, **kwargs):
        self.charts.append((data, kwargs))

    def expander(self, *args, **kwargs):
        return self

    def download_button(self, label, data, file_name, mime, **kwargs):
        self.downloads.append((label, file_name, mime, data))

    def checkbox(self, *args, **kwargs):
        return False


def load_helpers():
    tree = ast.parse(PAGE.read_text(encoding="utf-8"), filename=str(PAGE))
    names = {
        "_format", "_value", "_field_label", "_comparison", "_iso_energy",
        "_iso_overview", "_iso_friendly_values", "_mod_specifications",
        "_mod_overview", "_show_source", "_summary",
    }
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    ui = FakeStreamlit()
    scope = {"st": ui, "Decimal": Decimal, "InvalidOperation": InvalidOperation,
             "pd": pd, "MOD_SPEC_LABELS": {"p5_energy_without_regen_kwh": "Energy without regeneration"},
             "MOD_PAGE_LABELS": {},
             "ISO_ENERGY_FIELDS": {
                 "annual_energy_kwh", "annual_energy_wh", "efficiency_class",
                 "daily_running_energy_wh", "reference_energy_wh",
             },
             "DOCUMENT_LABELS": {"mod_pdf": "EnerCal MOD PDF", "iso_excel": "ISO Excel workbook"},
             "STATUS_LABELS": {"pending": "Pending review", "approved": "Approved"}}
    scope["mod_comparison_from_snapshot"] = lambda snapshot: snapshot["comparison"]
    scope["get_document_file"] = lambda doc_id: {
        "file_content": b"source", "filename": "report.pdf" if doc_id == 6 else "iso.xlsx",
        "mime_type": "application/pdf" if doc_id == 6 else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 str(PAGE), "exec"), scope)
    return SimpleNamespace(**{name: scope[name] for name in names}), ui


def record(phase, field, value, *, unit=None, page=None, sheet=None, cell=None, formula=None):
    return {"phase": phase, "field_name": field,
            "numeric_value": value if isinstance(value, (Decimal, int, float)) else None,
            "text_value": value if isinstance(value, str) else None,
            "raw_source_value": str(value) if value is not None else formula,
            "unit": unit, "source_page": page, "excel_sheet": sheet,
            "excel_cell": cell, "excel_formula": formula,
            "source_verified": False, "is_corrected": False}


def iso_snapshot():
    return {"revision": {"revision_id": 41, "revision_no": 1,
                         "document_type": "iso_excel", "review_status": "pending",
                         "review_scope": "comparison_only", "source_classification": "test_fixture"},
            "values": [
                record("existing", "annual_energy_kwh", Decimal("41933.0484"), unit="kWh/year", sheet="Calculation", cell="C20", formula="=C11*365"),
                record("proposed", "annual_energy_kwh", Decimal("22084.06994"), unit="kWh/year", sheet="Calculation", cell="D20"),
                record("existing", "efficiency_class", "C", sheet="Classification", cell="C18"),
                record("proposed", "efficiency_class", "A", sheet="Classification", cell="D18"),
                record("common", "rated_load_kg", 1600, unit="kg", sheet="Lift parameters", cell="C5"),
            ]}


def mod_snapshot():
    fields = (
        "elevator_technology", "annual_energy_consumption",
        "annual_regenerative_energy", "annual_electricity_expenditure",
        "lifetime_electricity_expenditure", "operational_co2e", "embodied_co2e",
    )
    comparison = {}
    for phase in ("existing", "proposed"):
        for field in fields:
            number = 4213 if phase == "existing" else 1993
            comparison[f"{phase}_{field}"] = {"numeric_value": number,
                "text_value": None, "raw_value": str(number), "unit": "kWh/year"}
    comparison["existing_elevator_technology"]["text_value"] = "Old traction"
    comparison["proposed_elevator_technology"]["text_value"] = "New traction"
    comparison["reported_energy_saving_percent"] = {"numeric_value": 50, "raw_value": "50", "unit": "%"}
    comparison["calculated_energy_saving_percent"] = {"numeric_value": Decimal("52.69"), "raw_value": "52.69", "unit": "%"}
    return {"comparison": comparison, "revision": {
        "revision_id": 42, "revision_no": 1, "document_type": "mod_pdf",
        "review_status": "pending", "review_scope": "full_values",
        "source_classification": "test_fixture"}, "values": [
        record("common", "site", "Singapore", page=8),
        record("existing", "p5_energy_without_regen_kwh", Decimal("1381"), page=5),
        record("common", "report_date", "2026-11-09", page=2),
    ]}


def test_iso_overview_shows_before_after_energy_and_savings():
    helpers, ui = load_helpers()
    helpers._iso_overview(iso_snapshot())
    assert ("Before annual energy", "41,933.05 kWh/year") in ui.metrics
    assert ("After annual energy", "22,084.07 kWh/year") in ui.metrics
    assert any(name == "Estimated ISO saving" and value == "47.33%" for name, value in ui.metrics)
    assert any("C → A" in text for name, text in ui.messages if name == "caption")


def test_iso_friendly_table_omits_formula_and_verification_columns():
    helpers, ui = load_helpers()
    helpers._iso_friendly_values(iso_snapshot())
    assert ui.selects[0][0] == "Information section"
    rows = ui.tables[0]
    assert len(rows) == 4
    assert {row["Configuration"] for row in rows} == {"Before", "After"}
    assert set(rows[0]) == {"Configuration", "Field", "Value", "Unit", "Excel sheet", "Excel cell"}
    assert all("=" not in str(row["Value"]) for row in rows)
    assert rows[0]["Field"] == "Annual energy kwh"


@pytest.mark.parametrize("before,after,expected", [
    (None, Decimal("220"), None), (Decimal(0), Decimal(20), None),
    (Decimal("1000"), Decimal("500"), Decimal("50")),
])
def test_iso_savings_do_not_invent_missing_or_divide_by_zero(before, after, expected):
    helpers, ui = load_helpers()
    values = [record("proposed", "annual_energy_kwh", after)]
    if before is not None:
        values.append(record("existing", "annual_energy_kwh", before))
    helpers._iso_overview({"values": values})
    actual = dict(ui.metrics)["Estimated ISO saving"]
    assert actual == (f"{expected:.2f}%" if expected is not None else "N/A")


def test_iso_wh_is_converted_to_kwh_in_summary():
    helpers, _ = load_helpers()
    assert helpers._iso_energy([record("existing", "annual_energy_wh", Decimal("1234000"))], "existing") == Decimal("1234")


def test_mod_chart_and_comparison_restore_old_layout():
    helpers, ui = load_helpers()
    helpers._mod_overview(mod_snapshot())
    assert len(ui.charts) == 1
    assert ("Existing energy", "4,213 kWh/year") in ui.metrics
    assert ("Proposed energy", "1,993 kWh/year") in ui.metrics
    assert len(ui.tables) == 1
    assert len(ui.tables[0]) == 7
    assert any("EnerCal reported saving" in msg for label, msg in ui.messages if label == "write")


def test_mod_specifications_are_grouped_as_before():
    helpers, ui = load_helpers()
    helpers._mod_specifications(mod_snapshot())
    assert ui.selects[0][1] == ["Report information", "Energy breakdown", "Engineering inputs", "All information"]
    assert len(ui.tables[0]) == 1
    assert ui.tables[0][0]["Field"] == "Report date"
    assert ui.tables[0][0]["Source page"] == 2


def test_familiar_download_labels_use_unified_source():
    helpers, ui = load_helpers()
    helpers._show_source({"document_id": 6, "document_type": "mod_pdf"})
    helpers._show_source({"document_id": 7, "document_type": "iso_excel"})
    assert [row[0] for row in ui.downloads] == ["Download source PDF", "Download ISO Excel"]


def test_presentation_keeps_legacy_tables_out_of_new_page():
    source = PAGE.read_text()
    assert '"PDF and data"' in source and '"ISO Excel data"' in source
    assert "Advanced: full engineering values and source provenance" in source
    assert "Profile and engineering information" in source
    for old in ("source_documents", "iso_assessments", "extraction_candidates"):
        assert old not in source
