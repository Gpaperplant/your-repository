"""Regression: unified MOD comparison adapters use raw_value, not raw_source_value.

Exercise the real Case Workspace display functions without PostgreSQL or Streamlit.
"""

import ast
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest


PAGE = Path(__file__).resolve().parents[1] / "enercal" / "enercal_review.py"


class FakeStreamlit:
    def __init__(self):
        self.tables = []
        self.warnings = []

    def dataframe(self, data, **kwargs):
        self.tables.append(data)

    def warning(self, message):
        self.warnings.append(message)

    def selectbox(self, label, options, **kwargs):
        return options[0]


def load_page_functions():
    """Compile the actual display functions without executing the Streamlit page."""
    source = ast.parse(PAGE.read_text(encoding="utf-8"), filename=str(PAGE))
    selected = {"_format", "_value", "_comparison", "_show_values"}
    nodes = [node for node in source.body if isinstance(node, ast.FunctionDef)
             and node.name in selected]
    assert {node.name for node in nodes} == selected
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    st = FakeStreamlit()
    namespace = {"st": st, "Decimal": Decimal}
    exec(compile(module, str(PAGE), "exec"), namespace)
    return SimpleNamespace(**{name: namespace[name] for name in selected}), st, namespace


def adapter(raw, number=None, text=None):
    return {"raw_value": raw, "numeric_value": number, "text_value": text,
            "unit": "kWh/year"}


def complete_mod_comparison():
    comparison = {}
    for name in (
        "elevator_technology", "annual_energy_consumption",
        "annual_regenerative_energy", "annual_electricity_expenditure",
        "lifetime_electricity_expenditure", "operational_co2e", "embodied_co2e"
    ):
        for phase in ("existing", "proposed"):
            comparison[f"{phase}_{name}"] = adapter("10", Decimal("10"))
    comparison["existing_elevator_technology"] = adapter("Old traction", text="Old traction")
    comparison["proposed_elevator_technology"] = adapter("New traction", text="New traction")
    comparison["existing_annual_energy_consumption"] = adapter("4213", Decimal("4213"))
    comparison["proposed_annual_energy_consumption"] = adapter("1993", Decimal("1993"))
    comparison["existing_annual_regenerative_energy"] = adapter("N/A")
    comparison["proposed_annual_regenerative_energy"] = adapter("450", Decimal("450"))
    comparison["reported_energy_saving_percent"] = adapter("52.69", Decimal("52.69"))
    comparison["calculated_energy_saving_percent"] = adapter("52.69", Decimal("52.69"))
    assert len(comparison) == 16
    return comparison


@pytest.mark.parametrize("value, expected", [
    ({"numeric_value": Decimal("42.2"), "text_value": None, "raw_value": "42.2"}, Decimal("42.2")),
    ({"numeric_value": None, "text_value": "Regenerative", "raw_value": "N/A"}, "Regenerative"),
    ({"numeric_value": None, "text_value": None, "raw_value": "N/A"}, "N/A"),
    ({"numeric_value": None, "text_value": None, "raw_source_value": "source PDF text"}, "source PDF text"),
    ({"numeric_value": None, "text_value": None, "raw_source_value": None, "raw_value": "N/A"}, "N/A"),
    ({"numeric_value": None, "text_value": None}, None),
])
def test_value_handles_both_source_key_shapes(value, expected):
    page, _, _ = load_page_functions()
    assert page._value(value) == expected


def test_mod_overview_handles_16_comparison_fields_including_missing_regen():
    page, st, globals_ = load_page_functions()
    comparison = complete_mod_comparison()
    globals_["mod_comparison_from_snapshot"] = lambda snapshot: comparison
    returned = page._comparison({"revision": {"document_type": "mod_pdf"}})
    assert returned is comparison
    assert not st.warnings
    assert len(st.tables) == 1
    assert len(st.tables[0]) == 7
    assert st.tables[0][0]["Existing"] == "Old traction"
    assert st.tables[0][1]["Existing"] == "4,213"
    assert st.tables[0][1]["Proposed"] == "1,993"
    assert st.tables[0][2]["Existing"] == "N/A"
    assert st.tables[0][2]["Proposed"] == "450"


def test_full_engineering_snapshot_still_displays_source_and_excel_details():
    page, st, _ = load_page_functions()
    row = {
        "phase": "existing", "field_name": "rated_load_kg",
        "numeric_value": Decimal("1635"), "text_value": None,
        "raw_source_value": "1,635", "unit": "kg", "source_page": None,
        "excel_sheet": "Lift parameters", "excel_cell": "C5", "excel_formula": "=1000+635",
        "source_verified": False, "is_corrected": False, "correction_reason": None,
    }
    page._show_values({"values": [row]})
    assert st.tables[0][0]["Value"] == "1,635"
    assert st.tables[0][0]["Original PDF/Excel text"] == "1,635"
    assert st.tables[0][0]["Excel cell"] == "C5"
    assert st.tables[0][0]["Formula"] == "=1000+635"
