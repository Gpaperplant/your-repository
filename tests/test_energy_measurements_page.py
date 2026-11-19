"""ISO-only upload page tests using an isolated Streamlit stub."""

import importlib.util
import json
import sys
import unittest
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import ModuleType

PAGE = Path(__file__).resolve().parents[1] / "enercal" / "enercal_measurements.py"


class FakeFile:
    def __init__(self, name="OP13.xlsx", content=b"PK\x03\x04example"):
        self.name, self.content = name, content

    def getvalue(self):
        return self.content


class FakeUI(ModuleType):
    def __init__(self, file, role="Admin", clicked=False, state=None):
        super().__init__("streamlit")
        self.file = file
        self.session_state = state if state is not None else {}
        self.session_state["role"] = role
        self.clicked = clicked
        self.messages, self.metrics, self.tables, self.exports = [], {}, [], []
        self.upload_types = []

    def title(self, msg): self.messages.append(("title", msg))
    def caption(self, msg): self.messages.append(("caption", msg))
    def subheader(self, msg): self.messages.append(("subheader", msg))
    def write(self, msg): self.messages.append(("write", msg))
    def error(self, msg): self.messages.append(("error", msg))
    def warning(self, msg): self.messages.append(("warning", msg))
    def metric(self, label, value): self.metrics[label] = value
    def dataframe(self, rows, **kwargs): self.tables.append(rows)
    def file_uploader(self, label, **kwargs):
        self.upload_types.append(kwargs["type"])
        return self.file
    def download_button(self, label, data, **kwargs): self.exports.append(data)
    def selectbox(self, label, values, **kwargs): return values[0]
    def button(self, label, **kwargs): return self.clicked and not kwargs.get("disabled", False)
    def spinner(self, msg): return nullcontext()


@contextmanager
def override(modules):
    before = {key: sys.modules.get(key) for key in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


RECORDS = [
    {"field": "lift_id", "value": "OP13 (L)", "kind": "identity", "sheet": "Lift parameters", "cell": "C2"},
    {"field": "before_annual_energy_kwh", "value": 41933.05, "kind": "calculated", "unit": "kWh/year", "sheet": "Classification", "cell": "C16"},
]


def render(file=None, role="Admin", clicked=False, state=None, cases=None, save=None, extract=None):
    ui = FakeUI(FakeFile() if file is None else (None if file is False else file), role, clicked, state)
    calls = {"extract": 0, "save": 0}
    extractor = ModuleType("services.iso_excel_extractor")
    importer = ModuleType("services.iso_import_service")
    repo = ModuleType("services.iso_repository")

    def extract_file(data):
        calls["extract"] += 1
        return extract(data) if extract else RECORDS

    def save_file(*args, **kwargs):
        calls["save"] += 1
        return save(*args, **kwargs) if save else {
            "created": True, "assessment_id": 7, "lift_id": "OP13 (L)", "excel_fields": 2,
        }

    extractor.extract_iso_excel = extract_file
    importer.MAX_FILE_BYTES = 15 * 1024 * 1024
    importer.process_iso_excel = save_file
    repo.get_cases = lambda: ["MOD1007"] if cases is None else cases
    spec = importlib.util.spec_from_file_location("_isolated_iso_upload", PAGE)
    module = importlib.util.module_from_spec(spec)
    with override({"streamlit": ui, "services.iso_excel_extractor": extractor,
                   "services.iso_import_service": importer, "services.iso_repository": repo}):
        spec.loader.exec_module(module)
    return ui, calls


class ISOExcelPageTests(unittest.TestCase):
    def test_missing_upload(self):
        ui, calls = render(file=False)
        self.assertEqual(ui.upload_types, [["xlsx"]])
        self.assertEqual(calls, {"extract": 0, "save": 0})

    def test_bad_signature(self):
        ui, calls = render(file=FakeFile(content=b"bad"), clicked=True)
        self.assertEqual(calls["save"], 0)
        self.assertTrue(any("valid ISO" in m for t, m in ui.messages if t == "error"))

    def test_wrong_extension(self):
        ui, calls = render(file=FakeFile(name="OP13.pdf"), clicked=True)
        self.assertEqual(calls["save"], 0)
        self.assertEqual(calls["extract"], 0)

    def test_oversize_blocked(self):
        ui, calls = render(file=FakeFile(content=b"PK" + b"a" * (15 * 1024 * 1024)), clicked=True)
        self.assertEqual(calls["extract"], 0)
        self.assertEqual(calls["save"], 0)

    def test_preview_only_excel_values(self):
        ui, calls = render()
        self.assertEqual(ui.metrics["Extracted fields"], 2)
        self.assertEqual(ui.tables[0][1]["Excel cell"], "C16")
        self.assertEqual(json.loads(ui.exports[0])["lift_id"], "OP13 (L)")
        self.assertEqual(calls["save"], 0)

    def test_preview_value_column_is_text_without_changing_json(self):
        examples = [
            RECORDS[0], RECORDS[1],
            {"field": "before_efficiency_class", "value": "C", "kind": "calculated", "sheet": "Classification", "cell": "C7"},
            {"field": "not_calculated", "value": None, "kind": "calculated", "sheet": "Calculation", "cell": "C15"},
        ]
        ui, _ = render(extract=lambda _: examples)
        self.assertEqual([row["Value"] for row in ui.tables[0]], ["OP13 (L)", "41933.05", "C", ""])
        self.assertTrue(all(isinstance(row["Value"], str) for row in ui.tables[0]))
        exported = json.loads(ui.exports[0])
        self.assertEqual(exported["fields"][1]["value"], 41933.05)
        self.assertIsNone(exported["fields"][-1]["value"])

    def test_save_success_and_no_filled_message(self):
        ui, calls = render(clicked=True)
        self.assertEqual(calls, {"extract": 1, "save": 1})
        self.assertEqual(ui.metrics["Excel fields"], 2)
        self.assertTrue(any(":green[ISO Excel saved" in m for t, m in ui.messages if t == "write"))

    def test_duplicate_save(self):
        ui, calls = render(clicked=True, save=lambda *_: {
            "created": False, "assessment_id": 2, "lift_id": "OP13 (L)", "excel_fields": 2,
        })
        self.assertEqual(calls["save"], 1)
        self.assertTrue(any("already saved" in m for t, m in ui.messages if t == "write"))

    def test_non_admin_cannot_save(self):
        ui, calls = render(role="Requester", clicked=True)
        self.assertEqual(calls["save"], 0)
        self.assertEqual(len(ui.exports), 1)

    def test_no_cases(self):
        ui, calls = render(clicked=True, cases=[])
        self.assertEqual(calls["save"], 0)

    def test_extraction_failure(self):
        def invalid(_): raise ValueError("corrupt workbook")
        ui, calls = render(extract=invalid, clicked=True)
        self.assertEqual(calls["save"], 0)
        self.assertTrue(any("corrupt workbook" in m for t, m in ui.messages if t == "error"))

    def test_cache_reuse_and_change(self):
        state = {"role": "Admin"}
        _, first = render(state=state)
        _, second = render(state=state)
        _, third = render(state=state, file=FakeFile(content=b"PK\x03\x04changed"))
        self.assertEqual([first["extract"], second["extract"], third["extract"]], [1, 0, 1])

    def test_database_failure(self):
        def invalid(*args): raise ValueError("Case elevator does not match")
        ui, calls = render(clicked=True, save=invalid)
        self.assertEqual(calls["save"], 1)
        self.assertTrue(any("does not match" in m for t, m in ui.messages if t == "error"))


if __name__ == "__main__":
    unittest.main()
