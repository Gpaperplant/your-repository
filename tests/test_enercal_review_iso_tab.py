"""Offline regression tests for saved ISO Excel evidence in Case workspace."""

import importlib.util
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pandas as pd  # Import compiled dependencies before temporary module overrides.


PAGE = Path(__file__).resolve().parents[1] / "enercal" / "enercal_review.py"


class Stopped(Exception):
    pass


@contextmanager
def override_modules(replacements):
    old = {key: sys.modules.get(key) for key in replacements}
    try:
        sys.modules.update(replacements)
        yield
    finally:
        for key, prior in old.items():
            if prior is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = prior


class FakeStreamlit(ModuleType):
    def __init__(self, role="Admin", selected_filter="ISO Excel"):
        super().__init__("streamlit")
        self.session_state = {"role": role}
        self.selected_filter = selected_filter
        self.messages = []
        self.downloads = []
        self.tables = []
        self.tabs_seen = []
        self.metrics = []
        self.column_config = SimpleNamespace(
            TextColumn=lambda *a, **k: None,
            NumberColumn=lambda *a, **k: None,
        )
        self.cache_data = lambda *a, **k: (lambda func: func)
        self.iso_decision = "Choose decision"
        self.iso_submit = False
        self.iso_note = ""
        self.iso_selection = None
        self.review_calls = []
        self.radio_choices = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stop(self):
        raise Stopped()

    def rerun(self):
        raise Stopped()

    def error(self, message):
        self.messages.append(("error", str(message)))

    def info(self, message):
        self.messages.append(("info", str(message)))

    def warning(self, message):
        self.messages.append(("warning", str(message)))

    def success(self, message):
        self.messages.append(("success", str(message)))

    def caption(self, message):
        self.messages.append(("caption", str(message)))

    def subheader(self, message):
        self.messages.append(("subheader", str(message)))

    def write(self, *args, **kwargs):
        self.messages.append(("write", " ".join(map(str, args))))

    def markdown(self, *args, **kwargs):
        pass

    def bar_chart(self, *args, **kwargs):
        pass

    def divider(self, *args, **kwargs):
        pass

    def metric(self, name, value, **kwargs):
        self.metrics.append((name, value))

    def columns(self, sizes, **kwargs):
        return [self] * (sizes if isinstance(sizes, int) else len(sizes))

    def container(self, **kwargs):
        return self

    def expander(self, *args, **kwargs):
        return self

    def form(self, *args, **kwargs):
        return self

    def radio(self, label, choices, **kwargs):
        self.radio_choices.append((label, list(choices)))
        return choices[0]

    def text_input(self, *args, **kwargs):
        return self.iso_note

    def form_submit_button(self, *args, **kwargs):
        return False

    def tabs(self, titles):
        self.tabs_seen.append(titles)
        return [self] * len(titles)

    def selectbox(self, label, options, **kwargs):
        if label == "ISO review decision":
            return self.iso_decision
        if label == "Saved assessment" and self.iso_selection is not None:
            return self.iso_selection
        if label == "Show evidence":
            return self.selected_filter
        return list(options)[0]

    def button(self, label, **kwargs):
        return self.iso_submit if label == "Submit ISO review" else False

    def checkbox(self, *args, **kwargs):
        return False

    def download_button(self, label, data, file_name, mime, **kwargs):
        self.downloads.append({"label": label, "data": data, "filename": file_name, "mime": mime})

    def dataframe(self, data, **kwargs):
        self.tables.append(data)


class FakeCursor:
    def __init__(self, cases, docs, values, fail=False):
        self.cases = cases
        self.docs = docs
        self.values = values
        self.fail = fail
        self.requests = []
        self.description = []
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        self.requests.append((query, params))
        if self.fail:
            raise RuntimeError("Database unavailable")
        normalized = " ".join(query.lower().split())
        if "from iso_assessments a" in normalized and "join iso_documents" not in normalized:
            rows = self.cases if params == ("MOD1007",) else []
        elif "from iso_documents d" in normalized:
            rows = self.docs if params == (2, "MOD1007") else []
        elif "from iso_extracted_values v" in normalized:
            rows = self.values if params == (2, "MOD1007") else []
        else:
            raise AssertionError("Unexpected SQL in ISO evidence tab")
        self.description = [SimpleNamespace(name=key) for key in rows[0]] if rows else []
        self.rows = [tuple(row.values()) for row in rows]

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self, cursor):
        self.cur = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cur


def fake_candidates():
    pairs = [
        ("elevator_technology", None, "Old", "New"),
        ("annual_energy_consumption", "kWh/year", "10000", "5500"),
        ("annual_regenerative_energy", "kWh/year", "100", "200"),
        ("annual_electricity_expenditure", "$/year", "2000", "1100"),
        ("lifetime_electricity_expenditure", "$", "50000", "27500"),
        ("operational_co2e", "kg CO2e", "30000", "17000"),
        ("embodied_co2e", "kg CO2e", "4000", "5000"),
    ]
    result = {}
    for field, unit, before, after in pairs:
        for role, value in (("existing", before), ("proposed", after)):
            numeric = float(value) if field != "elevator_technology" else None
            result[f"{role}_{field}"] = {"numeric_value": numeric, "raw_value": value, "unit": unit}
    result["reported_energy_saving_percent"] = {"numeric_value": 45.0, "raw_value": "45", "unit": "%"}
    result["calculated_energy_saving_percent"] = {"numeric_value": 45.0, "raw_value": "45", "unit": "%"}
    return result


def sample_data():
    cases = [{"assessment_id": 2, "lift_id": "OP13 (L)", "imported_at": "2026-11-17", "review_status": "pending", "reviewed_at": None, "review_note": ""}]
    docs = [{"document_id": 12, "filename": "OP13.xlsx", "content": memoryview(b"PK\x03\x04 original")}]
    values = [
        {"field_name": "rated_load_kg", "value_text": "1635", "unit": "kg",
         "excel_sheet": "Lift parameters", "excel_cell": "C5"},
    ]
    return cases, docs, values


def render(role="Admin", selected_filter="ISO Excel", cases=None, docs=None, values=None,
           fail=False, status="approved", decision=None, note="", submit=False, selected_iso=None):
    base_cases, base_docs, base_values = sample_data()
    cursor = FakeCursor(base_cases if cases is None else cases, base_docs if docs is None else docs, base_values if values is None else values, fail=fail)
    ui = FakeStreamlit(role, selected_filter)
    ui.iso_decision = decision or "Choose decision"
    ui.iso_note = note
    ui.iso_submit = submit
    ui.iso_selection = selected_iso
    db = ModuleType("database")
    db.get_connection = lambda: FakeConnection(cursor)
    repo = ModuleType("services.enercal_review_repository")
    candidates = fake_candidates()
    repo.CALCULATED_FIELD = "calculated_energy_saving_percent"
    repo.EDITABLE_STATUSES = {"pending", "changes_requested", "further_verification_required"}
    repo.EXPECTED_FIELDS = set(candidates)
    repo.FIELD_LABELS = {key: key for key in candidates}
    repo.get_review_documents = lambda: [(63, "MOD1007")]
    repo.get_review_details = lambda _: {
        "candidates": [{"field_name": key, **value} for key, value in candidates.items()],
        "revision_no": 2, "status": status, "pdf_bytes": b"original report", "filename": "Enercal_MOD1007.pdf",
        "history": [], "events": [],
    }
    repo.get_specifications = lambda _: []
    repo.resubmit_revision = lambda *args: None
    repo.review_revision = lambda *args: None
    repo.submit_verification = lambda *args: None
    iso_repo = ModuleType("services.iso_repository")
    def review_iso(*args):
        ui.review_calls.append(args)
        return args[2]

    iso_repo.review_iso_assessment = review_iso
    mapping = {
        "services.iso_repository": iso_repo,
        "streamlit": ui,
        "database": db,
        "services.enercal_review_repository": repo,
    }
    with override_modules(mapping):
        spec = importlib.util.spec_from_file_location("test_isolated_review_page", PAGE)
        page = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(page)
        except Stopped:
            pass
    return page, ui, cursor


class TestSavedIsoTab(unittest.TestCase):
    def test_pending_review_still_shows_iso_tab(self):
        _, ui, _ = render(status="pending")
        self.assertEqual(ui.tabs_seen[0][2], "ISO Excel data")
        self.assertEqual(len(ui.downloads), 2)

    def test_tabs_and_report_remain(self):
        _, ui, _ = render()
        self.assertEqual(ui.tabs_seen[0], ["Overview", "PDF and data", "ISO Excel data", "History"])
        self.assertTrue(any(m == "EnerCal review" for t, m in ui.messages if t == "subheader"))

    def test_pdf_review_only_approve_and_correct_values(self):
        _, ui, _ = render(status="pending")
        self.assertIn(
            ("Choose a review action", ["Approve", "Correct values"]),
            ui.radio_choices,
        )
        self.assertFalse(
            any("Other decision" in choices for _, choices in ui.radio_choices)
        )

    def test_pdf_existing_change_request_still_supports_correction(self):
        _, ui, _ = render(status="changes_requested")
        self.assertIn(
            ("Choose a review action", ["Confirm unchanged", "Correct values"]),
            ui.radio_choices,
        )

    def test_saved_excel_download(self):
        _, ui, _ = render()
        self.assertEqual(len(ui.downloads), 2)
        excel = next(item for item in ui.downloads if item["label"] == "Download ISO Excel")
        self.assertEqual(excel["data"], b"PK\x03\x04 original")
        self.assertEqual(excel["filename"], "OP13.xlsx")

    def test_saved_values_and_no_pdf_columns(self):
        _, ui, _ = render()
        rows = next(t for t in ui.tables if t and t[0].get("Field") == "rated_load_kg")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Excel cell"], "C5")
        self.assertEqual(set(rows[0]), {"Field", "Value", "Unit", "Excel sheet", "Excel cell"})
        self.assertIn(("ISO Excel fields", 1), ui.metrics)

    def test_case_and_assessment_scope(self):
        _, _, cursor = render()
        self.assertEqual(len(cursor.requests), 3)
        self.assertEqual(cursor.requests[0][1], ("MOD1007",))
        self.assertEqual(cursor.requests[1][1], (2, "MOD1007"))
        self.assertEqual(cursor.requests[2][1], (2, "MOD1007"))
        self.assertTrue(all("modernisation_cases m" in q for q, _ in cursor.requests))
        self.assertTrue(all(q.strip().upper().startswith("SELECT") for q, _ in cursor.requests))
        self.assertTrue(all("iso_excel" in q for q, _ in cursor.requests))

    def test_pending_iso_has_review_control(self):
        _, ui, _ = render()
        self.assertTrue(any("ISO engineering review" == msg for kind, msg in ui.messages if kind == "subheader"))
        self.assertTrue(any("Pending review" in msg for kind, msg in ui.messages if kind == "write"))

    def test_approved_iso_cannot_be_approved_again(self):
        cases, _, _ = sample_data()
        cases[0]["review_status"] = "approved"
        _, ui, _ = render(cases=cases)
        self.assertFalse(any("ISO engineering review" == msg for kind, msg in ui.messages if kind == "subheader"))
        self.assertTrue(any("Approved" in msg for kind, msg in ui.messages if kind == "write"))

    def test_old_iso_assessment_read_only(self):
        cases, _, _ = sample_data()
        older = {**cases[0], "assessment_id": 1}
        _, ui, _ = render(cases=[cases[0], older], selected_iso=1)
        self.assertTrue(any("An older ISO" in msg for kind, msg in ui.messages if kind == "caption"))
        self.assertFalse(ui.review_calls)

    def test_iso_approve_click_calls_repository(self):
        _, ui, _ = render(decision="Approve", note="Reviewed workbook", submit=True)
        self.assertEqual(ui.review_calls, [("MOD1007", 2, "approved", "Reviewed workbook")])

    def test_iso_reject_click_passes_reason(self):
        _, ui, _ = render(decision="Reject", note="Invalid calculation", submit=True)
        self.assertEqual(ui.review_calls, [("MOD1007", 2, "rejected", "Invalid calculation")])

    def test_no_assessments(self):
        _, ui, cursor = render(cases=[])
        self.assertEqual(len(cursor.requests), 1)
        self.assertTrue(any("No ISO Excel saved" in m for t, m in ui.messages if t == "write"))

    def test_missing_file(self):
        _, ui, _ = render(docs=[])
        self.assertTrue(any("ISO Excel file is unavailable" in m for t, m in ui.messages if t == "warning"))

    def test_db_failure_does_not_hide_history(self):
        _, ui, _ = render(fail=True)
        self.assertTrue(any("Could not load saved ISO Excel data" in m for t, m in ui.messages if t == "error"))
        self.assertTrue(any("Revision history" in m for t, m in ui.messages if t == "subheader"))

    def test_non_admin_does_not_query(self):
        _, ui, cursor = render(role="Requester")
        self.assertEqual(cursor.requests, [])
        self.assertTrue(any("Admin access is required" in m for t, m in ui.messages if t == "error"))
        self.assertEqual(ui.downloads, [])


if __name__ == "__main__":
    unittest.main()
