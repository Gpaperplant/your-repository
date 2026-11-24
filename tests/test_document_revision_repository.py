import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


FILE = Path(__file__).resolve().parents[1] / "services/document_revision_repository.py"


class FakeCursor:
    def __init__(self, rows=(), history=()):
        self.rows = list(rows)
        self.history = list(history)
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.queries.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return self.history


class FakeConnection:
    def __init__(self, rows=(), history=()):
        self.cursor_obj = FakeCursor(rows, history)
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, error, *_):
        self.committed = error is None
        self.rolled_back = error is not None
        return False

    def cursor(self):
        return self.cursor_obj


@pytest.fixture
def load_repository(monkeypatch):
    def load(rows=(), history=()):
        connection = FakeConnection(rows, history)
        db = ModuleType("database")
        db.get_connection = lambda: connection
        monkeypatch.setitem(sys.modules, "database", db)
        spec = importlib.util.spec_from_file_location("_revision_under_test", FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, connection
    return load


def test_first_revision_is_pending(load_repository):
    repo, conn = load_repository(rows=[(8,), None, (21,)])
    result = repo.create_revision(10)
    assert result == {
        "revision_id": 21, "document_id": 10,
        "revision_no": 1, "review_status": "pending",
    }
    assert "FOR UPDATE OF m" in conn.cursor_obj.queries[0][0]
    assert conn.cursor_obj.queries[-1][1] == (10, 1, "full_values")
    assert conn.committed


def test_second_revision_is_numbered_consecutively(load_repository):
    repo, conn = load_repository(rows=[(8,), (1, "changes_requested"), (22,)])
    result = repo.create_revision(10, review_scope="comparison_only")
    assert result["revision_no"] == 2
    assert conn.cursor_obj.queries[-1][1] == (10, 2, "comparison_only")


@pytest.mark.parametrize("status", ["approved", "rejected", "changes_requested", "further_verification_required"])
def test_reviewed_revision_can_be_followed(load_repository, status):
    repo, _ = load_repository(rows=[(8,), (3, status), (24,)])
    assert repo.create_revision(10)["revision_no"] == 4


def test_pending_revision_blocks_another(load_repository):
    repo, conn = load_repository(rows=[(8,), (1, "pending")])
    with pytest.raises(ValueError, match="pending revision"):
        repo.create_revision(10)
    assert conn.rolled_back
    assert len(conn.cursor_obj.queries) == 2


def test_missing_document_rejected(load_repository):
    repo, conn = load_repository(rows=[None])
    with pytest.raises(ValueError, match="Document not found"):
        repo.create_revision(99)
    assert conn.rolled_back


@pytest.mark.parametrize("value", [0, -1, True, "7", None])
def test_invalid_document_id_rejected(load_repository, value):
    repo, conn = load_repository()
    with pytest.raises(ValueError, match="positive integer"):
        repo.create_revision(value)
    assert not conn.cursor_obj.queries


def test_invalid_review_scope_rejected(load_repository):
    repo, conn = load_repository()
    with pytest.raises(ValueError, match="review scope"):
        repo.create_revision(10, "partial")
    assert not conn.cursor_obj.queries


def test_cursor_api_can_join_upload_transaction(load_repository):
    repo, conn = load_repository(rows=[(8,), None, (21,)])
    result = repo.insert_revision(conn.cursor(), 10)
    assert result["revision_id"] == 21
    assert not conn.committed


@pytest.mark.parametrize("decision", ["approved", "rejected", "changes_requested", "further_verification_required"])
def test_review_uses_database_review_function(load_repository, decision):
    repo, conn = load_repository(rows=[(71,)])
    reason = None if decision == "approved" else "Verify against PDF"
    result = repo.review_revision(21, decision, reason=reason, reviewer_id="Engineer 1")
    assert result == {"revision_id": 21, "review_id": 71, "review_status": decision}
    sql, params = conn.cursor_obj.queries[0]
    assert "review_document_revision" in sql
    assert "FALSE" in sql
    assert params == (21, decision, reason, "Engineer 1")
    assert conn.committed


@pytest.mark.parametrize("decision", ["rejected", "changes_requested", "further_verification_required"])
def test_nonapproval_requires_reason(load_repository, decision):
    repo, conn = load_repository()
    with pytest.raises(ValueError, match="reason"):
        repo.review_revision(21, decision, reason="   ")
    assert not conn.cursor_obj.queries


def test_invalid_decision_rejected(load_repository):
    repo, conn = load_repository()
    with pytest.raises(ValueError, match="decision"):
        repo.review_revision(21, "pending")
    assert not conn.cursor_obj.queries


def test_review_missing_event_rejected(load_repository):
    repo, conn = load_repository(rows=[(None,)])
    with pytest.raises(RuntimeError, match="not created"):
        repo.review_revision(21, "approved")
    assert conn.rolled_back


def test_latest_revision_returns_named_fields(load_repository):
    row = (21, 10, 2, "full_values", "approved", "checked", "Engineer", "created", "reviewed")
    repo, conn = load_repository(rows=[row])
    result = repo.get_latest_revision(10)
    assert result["revision_no"] == 2
    assert result["review_status"] == "approved"
    assert conn.cursor_obj.queries[0][1] == (10,)


def test_latest_revision_absent(load_repository):
    repo, _ = load_repository(rows=[None])
    assert repo.get_latest_revision(10) is None


def test_review_history_returns_events(load_repository):
    history = [
        (1, 21, "revision_submitted", None, "pending", None, None, False, "t1"),
        (2, 21, "approved", "pending", "approved", None, "Engineer", False, "t2"),
    ]
    repo, conn = load_repository(history=history)
    events = repo.get_review_history(21)
    assert [row["action"] for row in events] == ["revision_submitted", "approved"]
    assert events[1]["reviewer_authenticated"] is False
    assert conn.cursor_obj.queries[0][1] == (21,)


def test_review_history_empty(load_repository):
    repo, _ = load_repository(history=[])
    assert repo.get_review_history(21) == []
