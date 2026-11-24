"""Read-only regression checks for the unified Case Workspace repository."""

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "services/unified_case_workspace_repository.py"


def reply(contains, columns=(), rows=(), params=None):
    return {"contains": contains, "columns": columns, "rows": list(rows), "params": params}


class FakeCursor:
    def __init__(self, replies):
        self.remaining = list(replies)
        self.queries = []
        self.description = []
        self.current = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        statement = " ".join(sql.split())
        self.queries.append((statement, params))
        assert self.remaining, f"Unexpected SQL: {statement}"
        answer = self.remaining.pop(0)
        assert answer["contains"] in statement
        if answer["params"] is not None:
            assert params == answer["params"]
        self.description = [SimpleNamespace(name=name) for name in answer["columns"]]
        self.current = list(answer["rows"])

    def fetchall(self):
        result = list(self.current)
        self.current.clear()
        return result

    def fetchone(self):
        return self.current.pop(0) if self.current else None


class FakeConnection:
    def __init__(self, replies):
        self.cursor_obj = FakeCursor(replies)
        self.closed = False
        self.failed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        self.closed = True
        self.failed = exc_type is not None
        return False

    def cursor(self):
        return self.cursor_obj


@pytest.fixture
def load_repo(monkeypatch):
    def load(*answers):
        conn = FakeConnection(answers)
        db = ModuleType("database")
        db.get_connection = lambda: conn
        monkeypatch.setitem(sys.modules, "database", db)
        spec = importlib.util.spec_from_file_location("_workspace_under_test", FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, conn
    return load


def test_list_workspace_cases_uses_unified_documents(load_repo):
    repo, conn = load_repo(reply(
        "JOIN project_documents d ON d.case_id = m.case_id",
        ("case_id", "case_ref", "case_status", "elevator_ref", "document_count"),
        [(11, "TEST-MOD-REV-001", "registered", "TEST-LIFT-001", 1)],
    ))
    cases = repo.list_workspace_cases()
    assert cases == [{
        "case_id": 11, "case_ref": "TEST-MOD-REV-001", "case_status": "registered",
        "elevator_ref": "TEST-LIFT-001", "document_count": 1,
    }]
    assert "ORDER BY m.case_ref" in conn.cursor_obj.queries[0][0]
    assert conn.closed


def test_list_workspace_cases_empty(load_repo):
    repo, _ = load_repo(reply("FROM modernisation_cases", ("case_ref",), []))
    assert repo.list_workspace_cases() == []


def test_list_case_documents_returns_newest_and_latest_revision(load_repo):
    cols = (
        "document_id", "case_id", "case_ref", "elevator_ref", "document_type",
        "source_classification", "reported_elevator_ref", "filename", "file_sha256",
        "uploaded_at", "latest_revision_id", "latest_revision_no",
        "latest_review_scope", "latest_review_status",
    )
    rows = [
        (8, 11, "CASE-X", "TEST-LIFT-001", "iso_excel", "test_fixture", "OP13 (L)",
         "iso.xlsx", "b" * 64, "2026-11-24", 5, 1, "comparison_only", "rejected"),
        (7, 11, "CASE-X", "TEST-LIFT-001", "mod_pdf", "test_fixture", None,
         "mod.pdf", "a" * 64, "2026-11-23", 4, 2, "comparison_only", "approved"),
    ]
    repo, conn = load_repo(reply(
        "LEFT JOIN LATERAL", cols, rows, ("CASE-X",)
    ))
    actual = repo.list_case_documents("  CASE-X  ")
    assert [x["document_type"] for x in actual] == ["iso_excel", "mod_pdf"]
    assert actual[1]["latest_revision_no"] == 2
    query = conn.cursor_obj.queries[0][0]
    assert "ORDER BY d.document_id DESC" in query
    assert "file_content" not in query


def test_list_case_documents_without_revisions(load_repo):
    cols = ("document_id", "latest_revision_id", "latest_review_status")
    repo, _ = load_repo(reply("LEFT JOIN LATERAL", cols, [(7, None, None)]))
    assert repo.list_case_documents("CASE-X") == [{
        "document_id": 7, "latest_revision_id": None,
        "latest_review_status": None,
    }]


@pytest.mark.parametrize("bad", [None, "", "  ", 6, False, [], {}])
def test_invalid_case_ref_never_queries(load_repo, bad):
    repo, conn = load_repo()
    with pytest.raises(ValueError, match="Case reference"):
        repo.list_case_documents(bad)
    assert conn.cursor_obj.queries == []


def test_case_ref_is_parameterised(load_repo):
    malicious = "CASE-X'; DROP TABLE project_documents; --"
    repo, conn = load_repo(reply("WHERE m.case_ref = %s", (), [], (malicious,)))
    assert repo.list_case_documents(malicious) == []
    assert malicious not in conn.cursor_obj.queries[0][0]


@pytest.mark.parametrize("doc_type,mime", [
    ("mod_pdf", "application/pdf"),
    ("iso_excel", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("iso_test_pdf", "application/pdf"),
    ("meter_csv", "text/csv"),
])
def test_document_download_returns_original_bytes(load_repo, doc_type, mime):
    original = memoryview(b"the-original-file")
    repo, conn = load_repo(reply(
        "WHERE d.document_id = %s",
        ("document_id", "case_ref", "document_type", "filename", "file_sha256", "file_content"),
        [(7, "CASE-X", doc_type, "source.bin", "c" * 64, original)], (7,),
    ))
    result = repo.get_document_file(7)
    assert result["file_content"] == b"the-original-file"
    assert type(result["file_content"]) is bytes
    assert result["mime_type"] == mime
    assert "JOIN modernisation_cases" in conn.cursor_obj.queries[0][0]


def test_document_download_missing(load_repo):
    repo, conn = load_repo(reply("WHERE d.document_id = %s", ("document_id",), [], (7,)))
    with pytest.raises(ValueError, match="Document not found"):
        repo.get_document_file(7)
    assert conn.closed


def test_revisions_report_counts_and_history(load_repo):
    cols = (
        "revision_id", "document_id", "revision_no", "review_scope", "review_status",
        "review_note", "reviewed_by", "reviewer_authenticated", "created_at", "reviewed_at",
        "value_count", "verified_count", "corrected_count", "review_event_count",
    )
    rows = [
        (22, 7, 2, "comparison_only", "approved", "test", None, False, "t2", "r2", 63, 0, 1, 2),
        (21, 7, 1, "full_values", "changes_requested", "test", None, False, "t1", "r1", 63, 0, 0, 2),
    ]
    repo, conn = load_repo(reply("FROM document_revisions r", cols, rows, (7,)))
    revisions = repo.list_document_revisions(7)
    assert [item["revision_no"] for item in revisions] == [2, 1]
    assert revisions[0]["corrected_count"] == 1
    assert revisions[0]["verified_count"] == 0
    assert revisions[0]["review_event_count"] == 2
    sql = conn.cursor_obj.queries[0][0]
    assert "ORDER BY r.revision_no DESC" in sql
    assert "document_reviews h" in sql


def test_document_revisions_empty(load_repo):
    repo, _ = load_repo(reply("FROM document_revisions r", ("revision_id",), []))
    assert repo.list_document_revisions(7) == []


@pytest.mark.parametrize("bad", [None, 0, -1, True, False, "5", Decimal("5")])
@pytest.mark.parametrize("method,label", [
    ("get_document_file", "Document ID"),
    ("list_document_revisions", "Document ID"),
    ("get_revision_snapshot", "Revision ID"),
])
def test_invalid_identifiers_rejected_before_database(load_repo, bad, method, label):
    repo, conn = load_repo()
    with pytest.raises(ValueError, match=f"{label} must be a positive integer"):
        getattr(repo, method)(bad)
    assert conn.cursor_obj.queries == []


def test_revision_snapshot_includes_values_and_events(load_repo):
    revision_columns = (
        "revision_id", "revision_no", "review_scope", "review_status", "review_note",
        "reviewed_by", "reviewer_authenticated", "created_at", "reviewed_at",
        "document_id", "document_type", "filename", "file_sha256", "source_classification",
        "reported_elevator_ref", "uploaded_at", "case_id", "case_ref", "case_status",
        "elevator_ref",
    )
    revision = (
        22, 2, "comparison_only", "approved", "test", None, False, "created", "reviewed",
        7, "mod_pdf", "source.pdf", "a" * 64, "test_fixture", None, "uploaded",
        11, "TEST-MOD-REV-001", "registered", "TEST-LIFT-001",
    )
    repo, conn = load_repo(
        reply("WHERE r.revision_id = %s", revision_columns, [revision], (22,)),
        reply("FROM engineering_values WHERE revision_id = %s",
              ("value_id", "phase", "field_name", "source_page", "source_verified"),
              [(91, "existing", "annual_electricity_expenditure", 4, False)], (22,)),
        reply("FROM document_reviews WHERE revision_id = %s",
              ("review_id", "action", "previous_status", "new_status", "reason"),
              [(7, "approved", "pending", "approved", "test")], (22,)),
    )
    result = repo.get_revision_snapshot(22)
    assert result["revision"]["case_ref"] == "TEST-MOD-REV-001"
    assert result["revision"]["source_classification"] == "test_fixture"
    assert result["values"][0]["source_verified"] is False
    assert result["reviews"][0]["action"] == "approved"
    assert len(conn.cursor_obj.queries) == 3
    assert conn.closed


def test_revision_snapshot_missing(load_repo):
    repo, conn = load_repo(reply("WHERE r.revision_id = %s", ("revision_id",), [], (999,)))
    assert repo.get_revision_snapshot(999) is None
    assert len(conn.cursor_obj.queries) == 1


def test_revision_snapshot_preserves_iso_excel_formula(load_repo):
    rev = (31, "iso_excel", "test_fixture")
    repo, _ = load_repo(
        reply("WHERE r.revision_id = %s", ("revision_id", "document_type", "source_classification"), [rev]),
        reply("FROM engineering_values WHERE revision_id = %s",
              ("field_name", "phase", "numeric_value", "excel_sheet", "excel_cell", "excel_formula"),
              [("annual_energy_kwh", "existing", Decimal("41933.05"), "Classification", "C6", "=C5*365")]),
        reply("FROM document_reviews WHERE revision_id = %s", ("review_id",), []),
    )
    result = repo.get_revision_snapshot(31)
    assert result["values"][0]["excel_cell"] == "C6"
    assert result["values"][0]["excel_formula"] == "=C5*365"


def _sample_mod_snapshot():
    fields = ["annual_energy_consumption", "annual_regenerative_energy",
              "annual_electricity_expenditure", "lifetime_electricity_expenditure",
              "operational_co2e", "embodied_co2e"]
    values = []
    for phase in ("existing", "proposed"):
        values.append({
            "phase": phase, "field_name": "elevator_technology",
            "raw_source_value": "Typical Traction Elevator", "numeric_value": None,
            "text_value": "Typical Traction Elevator", "unit": None,
            "source_page": 4, "source_verified": False, "is_corrected": False,
        })
        for field in fields:
            values.append({
                "phase": phase, "field_name": field, "raw_source_value": "1138",
                "numeric_value": Decimal("1138"), "text_value": None,
                "unit": "$/year", "source_page": 4, "source_verified": False,
                "is_corrected": False,
            })
    for field in ("reported_energy_saving_percent", "calculated_energy_saving_percent"):
        values.append({
            "phase": "common", "field_name": field, "raw_source_value": "52.69",
            "numeric_value": Decimal("52.69"), "text_value": None,
            "unit": "%", "source_page": 4, "source_verified": False,
            "is_corrected": False,
        })
    return {"revision": {"document_type": "mod_pdf"}, "values": values, "reviews": []}


def test_comparison_adapter_exactly_sixteen_fields(load_repo):
    repo, _ = load_repo()
    comparison = repo.mod_comparison_from_snapshot(_sample_mod_snapshot())
    assert len(comparison) == 16
    assert comparison["existing_elevator_technology"]["text_value"] == "Typical Traction Elevator"
    assert comparison["existing_annual_electricity_expenditure"]["numeric_value"] == 1138
    assert comparison["calculated_energy_saving_percent"]["numeric_value"] == Decimal("52.69")


def test_comparison_adapter_preserves_corrections_without_approving(load_repo):
    repo, _ = load_repo()
    snapshot = _sample_mod_snapshot()
    target = next(x for x in snapshot["values"] if x["phase"] == "existing" and
                  x["field_name"] == "annual_electricity_expenditure")
    target["numeric_value"] = Decimal("1139")
    target["is_corrected"] = True
    target["source_verified"] = False
    values = repo.mod_comparison_from_snapshot(snapshot)
    assert values["existing_annual_electricity_expenditure"]["numeric_value"] == 1139
    assert values["existing_annual_electricity_expenditure"]["is_corrected"] is True
    assert values["existing_annual_electricity_expenditure"]["source_verified"] is False


def test_comparison_adapter_missing_required_field_fails(load_repo):
    repo, _ = load_repo()
    snapshot = _sample_mod_snapshot()
    snapshot["values"] = [r for r in snapshot["values"] if not (
        r["phase"] == "proposed" and r["field_name"] == "annual_energy_consumption")]
    with pytest.raises(ValueError, match="Missing MOD comparison field: proposed.annual_energy_consumption"):
        repo.mod_comparison_from_snapshot(snapshot)


def test_comparison_adapter_duplicate_field_fails(load_repo):
    repo, _ = load_repo()
    snapshot = _sample_mod_snapshot()
    snapshot["values"].append(dict(snapshot["values"][0]))
    with pytest.raises(ValueError, match="Duplicate MOD engineering"):
        repo.mod_comparison_from_snapshot(snapshot)


def test_comparison_adapter_wrong_document_type(load_repo):
    repo, _ = load_repo()
    snapshot = _sample_mod_snapshot()
    snapshot["revision"]["document_type"] = "iso_excel"
    with pytest.raises(ValueError, match="MOD document"):
        repo.mod_comparison_from_snapshot(snapshot)


@pytest.mark.parametrize("bad", [None, [], "revision", {}, {"revision": None}])
def test_comparison_adapter_requires_snapshot(load_repo, bad):
    repo, _ = load_repo()
    with pytest.raises(ValueError, match="revision snapshot"):
        repo.mod_comparison_from_snapshot(bad)


def test_repository_reads_only_unified_tables(load_repo):
    source = FILE.read_text()
    assert all(legacy not in source for legacy in (
        "source_documents", "extraction_revisions", "extraction_candidates",
        "iso_assessments", "iso_documents", "iso_extracted_values",
        "enercal_approved_kpis_view",
    ))
    repo, conn = load_repo(reply("FROM modernisation_cases", ("case_ref",), []))
    repo.list_workspace_cases()
    assert all(not any(keyword in sql for keyword in (
        "INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE ", "FOR UPDATE"
    )) for sql, _ in conn.cursor_obj.queries)


def test_repository_no_database_migration_required(load_repo):
    source = FILE.read_text()
    for name in ("modernisation_cases", "elevators", "project_documents",
                 "document_revisions", "engineering_values", "document_reviews"):
        assert name in source


def test_db_error_does_not_return_partial_snapshot(load_repo):
    repo, conn = load_repo(reply("WHERE r.revision_id = %s", ("revision_id",), [(4,)]))
    with pytest.raises(AssertionError, match="Unexpected SQL"):
        repo.get_revision_snapshot(4)
    assert conn.failed
