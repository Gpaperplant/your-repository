import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


class Cursor:
    def __init__(self):
        self.case = (7,)
        self.latest = (15, "pending")
        self.document = (301,)
        self.revision = (401, "pending", "comparison_only")
        self.unverified = False
        self.event = (502,)
        self.legacy_update = (15,)
        self.values = []
        self.fail_on = None
        self.queries = []
        self.next_row = None
        self.next_rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        statement = " ".join(sql.split())
        self.queries.append((statement, params))
        if self.fail_on and self.fail_on in statement:
            raise RuntimeError("Simulated SQL failure")
        if statement.startswith("SELECT case_id FROM modernisation_cases"):
            self.next_row = self.case
        elif statement.startswith("SELECT a.assessment_id, a.review_status"):
            self.next_row = self.latest
        elif statement.startswith("SELECT d.document_id FROM iso_assessments"):
            self.next_row = self.document
        elif statement.startswith("SELECT revision_id, review_status, review_scope"):
            self.next_row = self.revision
        elif statement.startswith("SELECT EXISTS"):
            self.next_row = (self.unverified,)
        elif statement.startswith("SELECT review_document_revision"):
            self.next_row = self.event
        elif statement.startswith("SELECT field_name, phase, value_kind"):
            self.next_rows = self.values
        elif statement.startswith("UPDATE iso_assessments"):
            self.next_row = self.legacy_update
        else:
            raise AssertionError(f"Unexpected SQL: {statement}")

    def fetchone(self):
        return self.next_row

    def fetchall(self):
        return self.next_rows


class Connection:
    def __init__(self):
        self.cursor_instance = Cursor()
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, error, *_):
        self.committed = error is None
        self.rolled_back = error is not None
        return False

    def cursor(self):
        return self.cursor_instance


@pytest.fixture
def setup(monkeypatch):
    connection = Connection()
    database = ModuleType("database")
    database.get_connection = lambda: connection
    monkeypatch.setitem(sys.modules, "database", database)
    spec = importlib.util.spec_from_file_location(
        "_iso_review_under_test", ROOT / "services/iso_repository.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.insert_revision = Mock(return_value={"revision_id": 501})
    module.insert_engineering_values = Mock()
    return module, connection


def statements(connection):
    return [sql for sql, _ in connection.cursor_instance.queries]


def reviews(connection):
    return [params for sql, params in connection.cursor_instance.queries
            if sql.startswith("SELECT review_document_revision")]


def sample_snapshot(module):
    return tuple("existing" if key == "phase" else (
        False if key in {"source_verified", "is_corrected"} else "example"
    ) for key in module.VALUE_COLUMNS)


def test_approved_iso_syncs_both_tables(setup):
    module, connection = setup
    assert module.review_iso_assessment("MOD1007", 15, "approved", "Checked") == "approved"
    assert connection.committed
    assert reviews(connection) == [(401, "approved", "Checked", None)]
    assert statements(connection)[-1].startswith("UPDATE iso_assessments")


def test_review_is_not_marked_as_authenticated(setup):
    module, connection = setup
    module.review_iso_assessment("MOD1007", 15, "approved")
    sql = [sql for sql in statements(connection)
           if sql.startswith("SELECT review_document_revision")][0]
    assert sql.endswith("FALSE)")
    assert reviews(connection)[0][3] is None


def test_rejection_syncs_with_reason(setup):
    module, connection = setup
    assert module.review_iso_assessment("MOD1007", 15, "rejected", "Bad formula") == "rejected"
    assert reviews(connection) == [(401, "rejected", "Bad formula", None)]
    assert connection.committed


def test_legacy_only_assessment_still_works(setup):
    module, connection = setup
    connection.cursor_instance.document = None
    assert module.review_iso_assessment("MOD1007", 15, "approved") == "approved"
    assert connection.committed
    assert not reviews(connection)


def test_approved_full_values_stays_full_when_verified(setup):
    module, connection = setup
    connection.cursor_instance.revision = (401, "pending", "full_values")
    module.review_iso_assessment("MOD1007", 15, "approved")
    assert reviews(connection) == [(401, "approved", None, None)]
    module.insert_revision.assert_not_called()


def test_unverified_full_values_creates_comparison_revision(setup):
    module, connection = setup
    cursor = connection.cursor_instance
    cursor.revision = (401, "pending", "full_values")
    cursor.unverified = True
    cursor.values = [sample_snapshot(module)]
    module.review_iso_assessment("MOD1007", 15, "approved", "Checked")
    assert connection.committed
    assert reviews(connection) == [
        (401, "further_verification_required",
         "ISO summary reviewed; field-level verification remains outstanding.", None),
        (501, "approved", "Checked", None),
    ]
    module.insert_revision.assert_called_once_with(cursor, 301, "comparison_only")
    args = module.insert_engineering_values.call_args.args
    assert args[:2] == (cursor, 501)
    assert args[2][0]["source_verified"] is False
    assert len(args[2]) == 1


def test_unverified_full_values_with_no_values_rejects_review(setup):
    module, connection = setup
    cursor = connection.cursor_instance
    cursor.revision = (401, "pending", "full_values")
    cursor.unverified = True
    with pytest.raises(ValueError, match="no engineering values"):
        module.review_iso_assessment("MOD1007", 15, "approved")
    assert connection.rolled_back
    assert not any(sql.startswith("UPDATE iso_assessments") for sql in statements(connection))


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_nonpending_unified_revision_blocks_legacy_change(setup, decision):
    module, connection = setup
    connection.cursor_instance.revision = (401, "approved", "comparison_only")
    with pytest.raises(ValueError, match="not pending"):
        module.review_iso_assessment("MOD1007", 15, decision, "Reason")
    assert connection.rolled_back
    assert not any(sql.startswith("UPDATE iso_assessments") for sql in statements(connection))


def test_unified_document_with_missing_revision_rejects_review(setup):
    module, connection = setup
    connection.cursor_instance.revision = None
    with pytest.raises(ValueError, match="no revision"):
        module.review_iso_assessment("MOD1007", 15, "approved")
    assert connection.rolled_back


@pytest.mark.parametrize("reason", [None, "", " "])
def test_rejection_requires_reason_before_database(setup, reason):
    module, connection = setup
    with pytest.raises(ValueError, match="reason"):
        module.review_iso_assessment("MOD1007", 15, "rejected", reason)
    assert connection.cursor_instance.queries == []


def test_unknown_decision_rejected_before_database(setup):
    module, connection = setup
    with pytest.raises(ValueError, match="decision"):
        module.review_iso_assessment("MOD1007", 15, "changes_requested")
    assert connection.cursor_instance.queries == []


def test_missing_case_rolls_back(setup):
    module, connection = setup
    connection.cursor_instance.case = None
    with pytest.raises(ValueError, match="case not found"):
        module.review_iso_assessment("MISSING", 15, "approved")
    assert connection.rolled_back


def test_stale_iso_assessment_rolls_back(setup):
    module, connection = setup
    connection.cursor_instance.latest = (16, "pending")
    with pytest.raises(ValueError, match="latest"):
        module.review_iso_assessment("MOD1007", 15, "approved")
    assert connection.rolled_back
    assert not any("FROM iso_assessments a JOIN project_documents" in sql
                   for sql in statements(connection))


def test_already_reviewed_legacy_assessment_rejected(setup):
    module, connection = setup
    connection.cursor_instance.latest = (15, "approved")
    with pytest.raises(ValueError, match="pending"):
        module.review_iso_assessment("MOD1007", 15, "approved")
    assert connection.rolled_back


@pytest.mark.parametrize("failed_query", [
    "SELECT review_document_revision", "UPDATE iso_assessments",
    "SELECT revision_id, review_status, review_scope",
])
def test_sql_failure_rolls_back_entire_review(setup, failed_query):
    module, connection = setup
    connection.cursor_instance.fail_on = failed_query
    with pytest.raises(RuntimeError, match="Simulated SQL failure"):
        module.review_iso_assessment("MOD1007", 15, "approved")
    assert connection.rolled_back
    if failed_query != "UPDATE iso_assessments":
        assert not any(sql.startswith("UPDATE iso_assessments")
                       for sql in statements(connection))


def test_missing_unified_event_rolls_back(setup):
    module, connection = setup
    connection.cursor_instance.event = None
    with pytest.raises(RuntimeError, match="review event"):
        module.review_iso_assessment("MOD1007", 15, "approved")
    assert connection.rolled_back


def test_legacy_race_rolls_back_unified_review(setup):
    module, connection = setup
    connection.cursor_instance.legacy_update = None
    with pytest.raises(ValueError, match="modified"):
        module.review_iso_assessment("MOD1007", 15, "approved")
    assert connection.rolled_back
    assert len(reviews(connection)) == 1


def test_missing_verification_query_response_rolls_back(setup):
    module, connection = setup
    cursor = connection.cursor_instance
    cursor.revision = (401, "pending", "full_values")
    original = cursor.fetchone

    def missing():
        if cursor.queries[-1][0].startswith("SELECT EXISTS"):
            return None
        return original()

    cursor.fetchone = missing
    with pytest.raises(RuntimeError, match="Cannot check"):
        module.review_iso_assessment("MOD1007", 15, "approved")
    assert connection.rolled_back


def test_compared_fields_match_database_contract(setup):
    module, _ = setup
    assert "source_verified" in module.VALUE_COLUMNS
    assert "excel_formula" in module.VALUE_COLUMNS
    assert "phase" in module.VALUE_COLUMNS


def test_no_inline_elevator_guessing_in_review(setup):
    module, _ = setup
    assert module.normalise_lift_id("OP13 (L)") == "OP13L"
