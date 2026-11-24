"""Offline ISO Excel upload page tests for unified document/revision IDs."""

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

PAGE = Path(__file__).resolve().parents[1] / "enercal/enercal_measurements.py"
FILE = b"PK\x03\x04placeholder"
RECORDS = [
    {"field": "lift_id", "value": "OP13 (L)", "kind": "identity", "sheet": "Lift", "cell": "C2"},
    {"field": "before_annual_energy_kwh", "value": 100.0, "kind": "calculated", "sheet": "ISO", "cell": "C16"},
]


class FakeFile:
    name = "OP13.xlsx"

    def getvalue(self):
        return FILE


class FakeUI(ModuleType):
    def __init__(self, *, role="Admin", upload=True, click=True, case="TEST-MOD-UNIFIED-003"):
        super().__init__("streamlit")
        self.session_state = {"role": role}
        self.upload = upload
        self.click = click
        self.case = case
        self.messages = []
        self.metrics = []
        self.table = None
        self.cache_data = lambda **kwargs: (lambda f: f)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def title(self, message):
        self.messages.append(("title", message))

    def subheader(self, message):
        self.messages.append(("subheader", message))

    def caption(self, message):
        self.messages.append(("caption", message))

    def write(self, message):
        self.messages.append(("write", message))

    def error(self, message):
        self.messages.append(("error", message))

    def success(self, message):
        self.messages.append(("success", message))

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))

    def metric(self, label, value):
        self.metrics.append((label, value))

    def dataframe(self, data, **kwargs):
        self.table = data

    def file_uploader(self, *args, **kwargs):
        return FakeFile() if self.upload else None

    def selectbox(self, *args, **kwargs):
        return self.case

    def button(self, *args, **kwargs):
        return self.click

    def spinner(self, *args, **kwargs):
        return self

    def download_button(self, *args, **kwargs):
        return False


@contextmanager
def patch_modules(replacements):
    prior = {name: sys.modules.get(name) for name in replacements}
    try:
        sys.modules.update(replacements)
        yield
    finally:
        for name, old in prior.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def render(*, saved=None, role="Admin", upload=True, click=True,
           cases=None, failure=None):
    ui = FakeUI(role=role, upload=upload, click=click)
    extractor = ModuleType("services.iso_excel_extractor")
    extractor.extract_iso_excel = Mock(return_value=RECORDS)
    importer = ModuleType("services.iso_import_service")
    importer.MAX_FILE_BYTES = 15 * 1024 * 1024
    importer.process_iso_excel = Mock(
        side_effect=failure,
        return_value=saved if saved is not None else {
            "created": True, "document_id": 15, "revision_id": 20,
            "revision_no": 1, "values_saved": 2,
            "lift_id": "OP13 (L)", "excel_fields": 2,
        },
    )
    repo = ModuleType("services.iso_repository")
    repo.get_cases = Mock(return_value=cases if cases is not None
                          else ["TEST-MOD-UNIFIED-003"])
    with patch_modules({"streamlit": ui,
                        "services.iso_excel_extractor": extractor,
                        "services.iso_import_service": importer,
                        "services.iso_repository": repo}):
        spec = importlib.util.spec_from_file_location("_isolated_iso_upload", PAGE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return ui, importer, extractor, repo


def test_saved_uses_unified_document_and_revision_ids():
    ui, importer, *_ = render()
    success = [s for label, s in ui.messages if label == "success"]
    assert len(success) == 1
    assert "document 15" in success[0]
    assert "revision 1" in success[0]
    assert "2 engineering values" in success[0]
    assert "Assessment ID" not in str(ui.messages)
    importer.process_iso_excel.assert_called_once_with(
        "TEST-MOD-UNIFIED-003", "OP13.xlsx", FILE,
    )


def test_duplicate_displays_existing_unified_ids():
    saved = {"created": False, "document_id": 15, "revision_id": 20,
             "revision_no": 2, "values_saved": 2,
             "lift_id": "OP13 (L)", "excel_fields": 2}
    ui, *_ = render(saved=saved)
    notes = [s for label, s in ui.messages if label == "info"]
    assert len(notes) == 1
    assert "document 15" in notes[0] and "revision 2" in notes[0]


def test_non_admin_cannot_save():
    ui, importer, *_ = render(role="Requester")
    importer.process_iso_excel.assert_not_called()
    assert any("Admin access" in str(msg) for _, msg in ui.messages)


def test_missing_file_does_not_extract_or_save():
    ui, importer, extractor, *_ = render(upload=False)
    importer.process_iso_excel.assert_not_called()
    extractor.extract_iso_excel.assert_not_called()


def test_no_click_does_not_save():
    _, importer, *_ = render(click=False)
    importer.process_iso_excel.assert_not_called()


def test_no_cases_cannot_save():
    ui, importer, *_ = render(cases=[])
    assert any(label == "warning" for label, _ in ui.messages)
    importer.process_iso_excel.assert_not_called()


def test_save_error_is_shown_not_reported_as_success():
    ui, importer, *_ = render(failure=ValueError("Simulated database failure"))
    assert any("Simulated database failure" in s
               for label, s in ui.messages if label == "error")
    assert not [s for label, s in ui.messages if label == "success"]


def test_stored_data_preserves_iso_fields():
    ui, _, extractor, _ = render()
    assert ui.table[0]["Field"] == "lift_id"
    assert ui.table[1]["Excel cell"] == "C16"
    extractor.extract_iso_excel.assert_called_once_with(FILE)
