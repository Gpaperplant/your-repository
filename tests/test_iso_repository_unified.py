"""ISO-only storage regression tests. These never connect to PostgreSQL."""

import hashlib
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
RECORDS = [
    {"field": "lift_id", "value": "OP13 (L)", "kind": "identity",
     "sheet": "Lift parameters", "cell": "C2"},
    {"field": "rated_load_kg", "value": 1635, "kind": "specification",
     "unit": "kg", "sheet": "Lift parameters", "cell": "C5"},
    {"field": "before_annual_energy_kwh", "value": 41933.05,
     "kind": "calculated", "sheet": "Classification", "cell": "C16", "formula": "=C15/1000"},
    {"field": "after_annual_energy_kwh", "value": 22084.07,
     "kind": "calculated", "sheet": "Classification", "cell": "D16", "formula": "=D15/1000"},
]
FILE = b"PK\x03\x04sample-content"
CHECKSUM = hashlib.sha256(FILE).hexdigest()


class FakeCursor:
    def __init__(self):
        self.case = ("MOD1007", "OP13(L)")
        self.latest_ref = None
        self.existing_revision = (401, 1, 4)
        self.fail_on = None
        self.queries = []
        self.next_row = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, parameters=()):
        sql = " ".join(statement.split())
        self.queries.append((sql, parameters))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("Simulated database failure")
        if sql.startswith("SELECT m.case_ref, e.elevator_ref"):
            self.next_row = self.case
        elif sql.startswith("SELECT reported_elevator_ref FROM project_documents"):
            self.next_row = (self.latest_ref,) if self.latest_ref is not None else None
        elif sql.startswith("SELECT r.revision_id, r.revision_no"):
            self.next_row = self.existing_revision
        else:
            raise AssertionError(f"Unexpected SQL in ISO upload: {sql}")

    def fetchone(self):
        return self.next_row


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()
        self.committed = False
        self.rolled_back = False
        self.entries = 0

    def __enter__(self):
        self.entries += 1
        return self

    def __exit__(self, exc_type, *_):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False

    def cursor(self):
        return self.cursor_instance


@pytest.fixture
def setup(monkeypatch):
    connection = FakeConnection()
    database = ModuleType("database")
    database.get_connection = lambda: connection
    monkeypatch.setitem(sys.modules, "database", database)
    spec = importlib.util.spec_from_file_location(
        "_isolated_iso_only_repository", ROOT / "services/iso_repository.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.insert_project_document = Mock(return_value={
        "document_id": 301, "case_id": 7, "created": True,
    })
    module.insert_revision = Mock(return_value={
        "revision_id": 401, "document_id": 301, "revision_no": 1,
    })
    module.insert_engineering_values = Mock(return_value={
        "values_saved": len(RECORDS),
    })
    return module, connection


def save(module, records=None, checksum=CHECKSUM, content=FILE, lift="OP13 (L)"):
    return module.save_iso_excel_assessment(
        7, lift, "OP13.xlsx", content,
        RECORDS if records is None else records, checksum,
    )


def _statements(conn):
    return [sql for sql, _ in conn.cursor_instance.queries]


def test_iso_upload_uses_only_unified_storage(setup):
    module, conn = setup
    assert save(module) == {
        "document_id": 301, "revision_id": 401,
        "revision_no": 1, "values_saved": 4, "created": True,
    }
    assert conn.committed
    assert conn.entries == 1
    assert not any("iso_assessments" in sql or "iso_documents" in sql
                   or "iso_extracted_values" in sql for sql in _statements(conn))
    module.insert_project_document.assert_called_once()
    module.insert_revision.assert_called_once_with(
        conn.cursor_instance, 301, "comparison_only"
    )
    module.insert_engineering_values.assert_called_once()


def test_same_cursor_one_transaction(setup):
    module, conn = setup
    save(module)
    cursor = conn.cursor_instance
    assert module.insert_project_document.call_args.args[0] is cursor
    assert module.insert_revision.call_args.args[0] is cursor
    assert module.insert_engineering_values.call_args.args[0] is cursor


def test_preserves_bytes_formulas_and_source_locations(setup):
    module, _ = setup
    save(module)
    kwargs = module.insert_project_document.call_args.kwargs
    assert kwargs["document_type"] == "iso_excel"
    assert kwargs["case_ref"] == "MOD1007"
    assert kwargs["reported_elevator_ref"] == "OP13 (L)"
    assert kwargs["file_bytes"] == FILE
    assert kwargs["source_classification"] == "unknown"
    values = module.insert_engineering_values.call_args.args[2]
    lookup = {(v["phase"], v["field_name"]): v for v in values}
    assert lookup[("existing", "annual_energy_kwh")]["numeric_value"] == Decimal("41933.05")
    assert lookup[("existing", "annual_energy_kwh")]["excel_formula"] == "=C15/1000"
    assert lookup[("proposed", "annual_energy_kwh")]["excel_cell"] == "D16"
    assert all(v["source_verified"] is False for v in values)


def test_test_lift_classified_as_test_fixture(setup):
    module, conn = setup
    conn.cursor_instance.case = ("TEST-MOD-UNIFIED-003", "TEST-LIFT-001")
    save(module)
    assert module.insert_project_document.call_args.kwargs["source_classification"] == "test_fixture"


def test_duplicate_returns_same_unified_ids_without_revision(setup):
    module, conn = setup
    module.insert_project_document.return_value["created"] = False
    assert save(module) == {
        "document_id": 301, "revision_id": 401,
        "revision_no": 1, "values_saved": 4, "created": False,
    }
    assert conn.committed
    module.insert_revision.assert_not_called()
    module.insert_engineering_values.assert_not_called()


def test_duplicate_without_revision_rejected(setup):
    module, conn = setup
    module.insert_project_document.return_value["created"] = False
    conn.cursor_instance.existing_revision = None
    with pytest.raises(RuntimeError, match="no revision"):
        save(module)
    assert conn.rolled_back


def test_missing_case_rolls_back(setup):
    module, conn = setup
    conn.cursor_instance.case = None
    with pytest.raises(ValueError, match="case not found"):
        save(module)
    assert conn.rolled_back


def test_previous_workbook_different_lift_rolls_back(setup):
    module, conn = setup
    conn.cursor_instance.latest_ref = "OTHER-LIFT"
    with pytest.raises(ValueError, match="already contains ISO data"):
        save(module)
    assert conn.rolled_back
    module.insert_project_document.assert_not_called()


def test_formatted_same_lift_allowed(setup):
    module, conn = setup
    conn.cursor_instance.latest_ref = "OP13(L)"
    assert save(module)["created"] is True


def test_unified_duplicate_rolls_back_on_repository_failure(setup):
    module, conn = setup
    module.insert_project_document.side_effect = RuntimeError("Save failed")
    with pytest.raises(RuntimeError, match="Save failed"):
        save(module)
    assert conn.rolled_back


@pytest.mark.parametrize("operation", ["insert_revision", "insert_engineering_values"])
def test_storage_failure_rolls_back(setup, operation):
    module, conn = setup
    getattr(module, operation).side_effect = RuntimeError("Save failed")
    with pytest.raises(RuntimeError, match="Save failed"):
        save(module)
    assert conn.rolled_back


def test_partial_snapshot_rolls_back(setup):
    module, conn = setup
    module.insert_engineering_values.return_value = {"values_saved": 3}
    with pytest.raises(RuntimeError, match="Incomplete"):
        save(module)
    assert conn.rolled_back


def test_invalid_checksum_before_connection(setup):
    module, conn = setup
    with pytest.raises(ValueError, match="checksum"):
        save(module, checksum="a" * 64)
    assert conn.entries == 0


def test_missing_lift_before_connection(setup):
    module, conn = setup
    with pytest.raises(ValueError, match="lift reference"):
        save(module, lift="")
    assert conn.entries == 0


def test_extracted_lift_mismatch_before_connection(setup):
    module, conn = setup
    with pytest.raises(ValueError, match="extracted lift reference"):
        save(module, lift="DIFFERENT")
    assert conn.entries == 0


def test_duplicate_fields_before_connection(setup):
    module, conn = setup
    with pytest.raises(ValueError, match="Duplicate"):
        save(module, records=RECORDS + RECORDS[:1])
    assert conn.entries == 0


def test_invalid_case_id_before_connection(setup):
    module, conn = setup
    with pytest.raises(ValueError, match="case ID"):
        module.save_iso_excel_assessment(0, "OP13", "a.xlsx", FILE, RECORDS, CHECKSUM)
    assert conn.entries == 0


def test_invalid_iso_decision_still_rejected(setup):
    module, conn = setup
    with pytest.raises(ValueError, match="decision"):
        module.review_iso_assessment("MOD1007", 15, "no")
    assert conn.entries == 0


def test_numeric_parser_preserves_missing_text(setup):
    module, _ = setup
    assert module._number("1,635") == 1635
    assert module._number("invalid") is None
    assert module._number(False) is None
