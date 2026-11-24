"""Regression tests for ISO evidence in the unified five-table Case Workspace."""

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pandas as pd  # Load compiled packages before replacing Streamlit.
import pytest

PAGE = Path(__file__).resolve().parents[1] / "enercal" / "enercal_review.py"


class Stopped(Exception):
    pass


@contextmanager
def override_modules(replacements):
    previous = {name: sys.modules.get(name) for name in replacements}
    try:
        sys.modules.update(replacements)
        yield
    finally:
        for name, original in previous.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


class FakeStreamlit(ModuleType):
    def __init__(self, role="Admin", decision="Choose decision", submit=False,
                 checked=False, note="", revision_selection=None):
        super().__init__("streamlit")
        self.session_state = {"role": role}
        self.decision = decision
        self.submit = submit
        self.checked = checked
        self.note = note
        self.revision_selection = revision_selection
        self.messages = []
        self.tables = []
        self.downloads = []
        self.metrics = []
        self.tabs_seen = []
        self.controls = []
        self.cache_data = lambda *args, **kwargs: (lambda function: function)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def stop(self):
        raise Stopped()

    def rerun(self):
        raise Stopped()

    def _message(self, category, text):
        self.messages.append((category, str(text)))

    def error(self, text):
        self._message("error", text)

    def info(self, text):
        self._message("info", text)

    def warning(self, text):
        self._message("warning", text)

    def success(self, text):
        self._message("success", text)

    def caption(self, text):
        self._message("caption", text)

    def subheader(self, text):
        self._message("subheader", text)

    def write(self, *args, **kwargs):
        self._message("write", " ".join(str(x) for x in args))

    def metric(self, label, value, **kwargs):
        self.metrics.append((label, value))

    def columns(self, spec, **kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        return [self] * count

    def dataframe(self, data, **kwargs):
        self.tables.append(data)

    def tabs(self, labels):
        self.tabs_seen.append(list(labels))
        return [self] * len(labels)

    def selectbox(self, label, options, **kwargs):
        self.controls.append((label, list(options)))
        if label == "Decision":
            return self.decision
        if label == "Revision" and self.revision_selection is not None:
            return self.revision_selection
        return list(options)[0]

    def form(self, *args, **kwargs):
        return self

    def form_submit_button(self, label, **kwargs):
        self.controls.append(("form_submit_button", label))
        return self.submit

    def text_area(self, *args, **kwargs):
        return self.note

    def checkbox(self, label, **kwargs):
        return self.checked if label == "I checked the comparison against the original source" else False

    def download_button(self, label, data, file_name, mime, **kwargs):
        self.downloads.append({"label": label, "data": data, "filename": file_name, "mime": mime})


ISO_DOCUMENT = {
    "document_id": 5,
    "case_id": 8,
    "case_ref": "TEST-MOD-REV-001",
    "document_type": "iso_excel",
    "source_classification": "test_fixture",
    "filename": "OP13.xlsx",
}


def _revision(revision_id=40, revision_no=1, status="pending", scope="comparison_only"):
    return {
        "revision_id": revision_id,
        "document_id": 5,
        "revision_no": revision_no,
        "review_scope": scope,
        "review_status": status,
        "document_type": "iso_excel",
        "source_classification": "test_fixture",
        "filename": "OP13.xlsx",
        "case_ref": "TEST-MOD-REV-001",
        "value_count": 1,
        "verified_count": 0,
        "corrected_count": 0,
        "review_event_count": 1,
        "created_at": "2026-11-24",
    }


def _value():
    return {
        "phase": "existing",
        "field_name": "rated_load_kg",
        "numeric_value": 1635,
        "text_value": None,
        "raw_source_value": "1635",
        "unit": "kg",
        "source_page": None,
        "excel_sheet": "Lift parameters",
        "excel_cell": "C5",
        "excel_formula": "=1000+635",
        "source_verified": False,
        "is_corrected": False,
        "correction_reason": None,
    }


def render(*, role="Admin", status="pending", decision="Choose decision", submit=False,
           checked=False, note="", revisions=None, revision_selection=None,
           documents=None, values=None, file_error=False, load_error=False):
    ui = FakeStreamlit(role, decision, submit, checked, note, revision_selection)
    revisions = revisions if revisions is not None else [_revision(status=status)]
    documents = documents if documents is not None else [dict(ISO_DOCUMENT)]
    values = values if values is not None else [_value()]
    calls = {"cases": 0, "documents": 0, "snapshot": [], "reviews": [], "files": []}

    workspace = ModuleType("services.unified_case_workspace_repository")

    def list_cases():
        calls["cases"] += 1
        if load_error:
            raise RuntimeError("Database unavailable")
        return [{"case_ref": "TEST-MOD-REV-001"}] if documents else []

    def list_documents(case_ref):
        calls["documents"] += 1
        assert case_ref == "TEST-MOD-REV-001"
        return documents

    def list_revisions(document_id):
        assert document_id == 5
        return revisions

    def get_snapshot(revision_id):
        calls["snapshot"].append(revision_id)
        selected = next(r for r in revisions if r["revision_id"] == revision_id)
        return {"revision": dict(selected), "values": values, "reviews": [{
            "action": "revision_submitted",
            "previous_status": None,
            "new_status": "pending",
            "reason": "Uploaded",
            "created_at": "2026-11-24",
        }]}

    def get_file(document_id):
        calls["files"].append(document_id)
        if file_error:
            raise RuntimeError("File not available")
        return {
            "file_content": b"PK\x03\x04 source",
            "filename": "OP13.xlsx",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }

    workspace.list_workspace_cases = list_cases
    workspace.list_case_documents = list_documents
    workspace.list_document_revisions = list_revisions
    workspace.get_revision_snapshot = get_snapshot
    workspace.get_document_file = get_file
    workspace.mod_comparison_from_snapshot = lambda _: (_ for _ in ()).throw(AssertionError("MOD logic should not run for ISO"))

    actions = ModuleType("services.unified_review_actions")
    actions.CALCULATED_FIELD = "calculated_energy_saving_percent"
    actions.FIELD_LABELS = {}

    def submit_review(document_id, status, reason):
        calls["reviews"].append((document_id, status, reason))
        return {"revision_no": 2}

    actions.submit_comparison_review = submit_review
    actions.submit_mod_correction = lambda *args: (_ for _ in ()).throw(AssertionError("ISO cannot use MOD correction"))

    replacement = {
        "streamlit": ui,
        "services.unified_case_workspace_repository": workspace,
        "services.unified_review_actions": actions,
    }
    with override_modules(replacement):
        spec = importlib.util.spec_from_file_location("_unified_iso_test_page", PAGE)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Stopped:
            pass
    return ui, calls


def messages(ui, category):
    return [text for kind, text in ui.messages if kind == category]


def test_iso_document_uses_unified_sections():
    ui, calls = render()
    assert ui.tabs_seen == [["Overview", "ISO Excel data", "Engineering review", "History"]]
    assert calls["documents"] == 1
    assert calls["snapshot"] == [40]
    assert any("Case Workspace" in text for text in messages(ui, "subheader"))


def test_download_original_iso_excel():
    ui, calls = render()
    assert calls["files"] == [5]
    assert ui.downloads == [{
        "label": "Download ISO Excel", "data": b"PK\x03\x04 source",
        "filename": "OP13.xlsx",
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }]


def test_iso_values_keep_source_cell_and_formula():
    ui, _ = render()
    rows = next(table for table in ui.tables if table and isinstance(table[0], dict)
                and "Original PDF/Excel text" in table[0])
    assert rows[0]["Field"] == "rated_load_kg"
    assert rows[0]["Value"] == "1,635"
    assert rows[0]["Excel sheet"] == "Lift parameters"
    assert rows[0]["Excel cell"] == "C5"
    assert rows[0]["Formula"] == "=1000+635"
    assert rows[0]["Source verified"] is False


def test_pending_iso_has_review_options():
    ui, _ = render()
    assert ("Decision", ["Choose decision", "Approve comparison", "Reject", "Request changes"]) in ui.controls
    assert "Engineering comparison review" in messages(ui, "subheader")


@pytest.mark.parametrize("choice,status", [
    ("Approve comparison", "approved"),
    ("Reject", "rejected"),
    ("Request changes", "changes_requested"),
])
def test_iso_decision_calls_unified_review_service(choice, status):
    ui, calls = render(decision=choice, submit=True, checked=True, note="Reviewed test workbook")
    assert calls["reviews"] == [(5, status, "Reviewed test workbook")]
    assert ui.session_state["unified_review_notice"] == f"Revision 2 {status.replace('_',' ')}."


def test_iso_review_requires_source_confirmation():
    ui, calls = render(decision="Approve comparison", submit=True, checked=False)
    assert calls["reviews"] == []
    assert any("checked the original source" in msg for msg in messages(ui, "error"))


def test_iso_review_requires_decision():
    ui, calls = render(decision="Choose decision", submit=True, checked=True)
    assert calls["reviews"] == []
    assert any("Choose a review decision" in msg for msg in messages(ui, "error"))


@pytest.mark.parametrize("status", ["approved", "rejected", "changes_requested"])
def test_nonpending_iso_cannot_be_reviewed_again(status):
    ui, calls = render(status=status)
    assert calls["reviews"] == []
    assert ("Decision", ["Choose decision", "Approve comparison", "Reject", "Request changes"]) not in ui.controls
    assert any("cannot receive another review" in msg for msg in messages(ui, "info"))


def test_historical_iso_revision_is_read_only():
    revisions = [_revision(revision_id=41, revision_no=2), _revision(revision_id=40, revision_no=1)]
    ui, calls = render(revisions=revisions, revision_selection=40)
    assert calls["snapshot"] == [40]
    assert not calls["reviews"]
    assert any("Historical revisions are read-only" in msg for msg in messages(ui, "info"))


def test_empty_workspace_shows_information():
    ui, calls = render(documents=[])
    assert calls["cases"] == 1
    assert calls["documents"] == 0
    assert any("No documents have been saved" in msg for msg in messages(ui, "info"))


def test_file_error_preserves_revision_history():
    ui, calls = render(file_error=True)
    assert any("Could not load source document" in msg for msg in messages(ui, "error"))
    assert "Revision history" in messages(ui, "subheader")
    assert calls["reviews"] == []


def test_workspace_query_error_is_reported():
    ui, _ = render(load_error=True)
    assert any("Could not load unified workspace" in msg for msg in messages(ui, "error"))


def test_nonadmin_cannot_query_or_download():
    ui, calls = render(role="Requester")
    assert "Admin access is required" in messages(ui, "error")
    assert calls["cases"] == 0
    assert calls["files"] == []
    assert calls["reviews"] == []


def test_page_has_no_legacy_iso_dependencies():
    content = PAGE.read_text()
    for table in ("iso_assessments", "iso_documents", "iso_extracted_values"):
        assert table not in content
    for old_api in ("review_iso_assessment", "_saved_iso_assessments", "_saved_iso_details"):
        assert old_api not in content
