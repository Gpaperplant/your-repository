import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]


class Cursor:
    def __init__(self):
        self.queries = []
        self.batches = []
        self.next_row = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, parameters=()):
        statement = " ".join(sql.split())
        self.queries.append((statement, parameters))
        if "INSERT INTO extraction_revisions" in statement:
            self.next_row = (202,)
        elif "SELECT EXISTS" in statement:
            self.next_row = (True,)
        else:
            self.next_row = None

    def fetchone(self):
        return self.next_row

    def executemany(self, sql, records):
        self.batches.append((" ".join(sql.split()), list(records)))


class Connection:
    def __init__(self):
        self.cursor_instance = Cursor()
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, kind, *_):
        self.committed = kind is None
        self.rolled_back = kind is not None
        return False

    def cursor(self):
        return self.cursor_instance


@pytest.fixture
def setup(monkeypatch):
    connection = Connection()
    database = ModuleType("database")
    database.get_connection = lambda: connection
    revisions = ModuleType("services.document_revision_repository")
    revisions.insert_revision = Mock(return_value={
        "revision_id": 303, "document_id": 20,
        "revision_no": 2, "review_status": "pending",
    })
    values = ModuleType("services.engineering_value_repository")
    values.insert_engineering_values = Mock(return_value={"values_saved": 16})
    monkeypatch.setitem(sys.modules, "database", database)
    monkeypatch.setitem(sys.modules, "services.document_revision_repository", revisions)
    monkeypatch.setitem(sys.modules, "services.engineering_value_repository", values)
    spec = importlib.util.spec_from_file_location(
        "_review_unified_under_test", ROOT / "services/enercal_review_repository.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, connection, revisions, values


@pytest.fixture
def candidates(setup):
    module = setup[0]
    records = []
    for field in module.FIELD_LABELS:
        tech = field in module.TECHNOLOGY_FIELDS
        value = "A" if tech else "100"
        records.append({
            "field_name": field, "raw_value": value,
            "numeric_value": None if tech else Decimal("100"),
            "page_id": 44, "revision_no": 1, "unit": "kWh",
        })
    for record in records:
        if record["field_name"] == "existing_annual_energy_consumption":
            record.update(raw_value="4213", numeric_value=Decimal("4213"))
        if record["field_name"] == "proposed_annual_energy_consumption":
            record.update(raw_value="1993", numeric_value=Decimal("1993"))
        if record["field_name"] == "calculated_energy_saving_percent":
            record.update(raw_value="52.69", numeric_value=Decimal("52.69"))
    return records


def revision(status="pending"):
    return {"revision_id": 100, "revision_no": 1, "review_status": status}


def unified(status="pending", scope="full_values"):
    return {
        "document_id": 20, "revision_id": 30,
        "revision_no": 1, "review_status": status, "review_scope": scope,
    }


def test_legacy_approval_synchronizes_before_commit(setup):
    module, connection, _, _ = setup
    module._validated_revision = Mock(return_value=revision())
    module._sync_review = Mock()
    module._event = Mock()
    assert module.review_revision(10, "approved") == 1
    assert connection.committed
    module._sync_review.assert_called_once_with(
        connection.cursor_instance, 10, "approved", ""
    )
    assert any("UPDATE extraction_revisions" in sql for sql, _ in connection.cursor_instance.queries)


def test_unified_review_failure_rolls_back_legacy_approval(setup):
    module, connection, _, _ = setup
    module._validated_revision = Mock(return_value=revision())
    module._sync_review = Mock(side_effect=ValueError("Unified approval failed"))
    with pytest.raises(ValueError, match="Unified approval failed"):
        module.review_revision(10, "approved")
    assert connection.rolled_back
    assert not any("UPDATE extraction_revisions" in sql for sql, _ in connection.cursor_instance.queries)


@pytest.mark.parametrize("decision", ["rejected", "changes_requested", "further_verification_required"])
def test_review_reason_is_required(setup, decision):
    module, connection, _, _ = setup
    with pytest.raises(ValueError, match="reason"):
        module.review_revision(10, decision)
    assert not connection.cursor_instance.queries


def test_review_rejects_unknown_decision(setup):
    module, _, _, _ = setup
    with pytest.raises(ValueError, match="Unknown"):
        module.review_revision(10, "ready")


def test_review_requires_pending_legacy_revision(setup):
    module, connection, _, _ = setup
    module._validated_revision = Mock(return_value=revision("approved"))
    with pytest.raises(ValueError, match="Only pending"):
        module.review_revision(10, "approved")
    assert connection.rolled_back


def test_review_detects_concurrent_legacy_change(setup):
    module, connection, _, _ = setup
    module._validated_revision = Mock(return_value=revision())
    module._sync_review = Mock()
    connection.cursor_instance.rowcount = 0
    with pytest.raises(RuntimeError, match="changed"):
        module.review_revision(10, "approved")
    assert connection.rolled_back


def test_comparison_approval_does_not_mark_full_values_verified(setup):
    module, connection, _, _ = setup
    cursor = connection.cursor_instance
    module._unified_revision = Mock(return_value=unified())
    module._review_unified = Mock()
    module._clone_unified = Mock(return_value={**unified(scope="comparison_only"), "revision_id": 303})
    module._sync_review(cursor, 10, "approved", "")
    assert module._review_unified.call_count == 2
    assert module._review_unified.call_args_list[0].args[2] == "further_verification_required"
    assert module._review_unified.call_args_list[1].args[1:3] == (303, "approved")
    assert module._clone_unified.call_args.kwargs["scope"] == "comparison_only"


def test_unified_comparison_approval_uses_existing_revision(setup):
    module, connection, _, _ = setup
    module._unified_revision = Mock(return_value=unified(scope="comparison_only"))
    module._review_unified = Mock()
    module._clone_unified = Mock()
    module._sync_review(connection.cursor_instance, 10, "approved", "Reviewed")
    module._review_unified.assert_called_once_with(
        connection.cursor_instance, 30, "approved", "Reviewed"
    )
    module._clone_unified.assert_not_called()


def test_fully_verified_scope_does_not_create_extra_revision(setup):
    module, connection, _, _ = setup
    module._unified_revision = Mock(return_value=unified())
    module._review_unified = Mock()
    module._clone_unified = Mock()
    cursor = connection.cursor_instance
    original_execute = cursor.execute

    def execute(sql, params=()):
        original_execute(sql, params)
        if "SELECT EXISTS" in sql:
            cursor.next_row = (False,)

    cursor.execute = execute
    module._sync_review(cursor, 10, "approved", "")
    module._review_unified.assert_called_once_with(cursor, 30, "approved", None)
    module._clone_unified.assert_not_called()


def test_unmatched_historical_document_stays_legacy_only(setup):
    module, connection, _, _ = setup
    module._unified_revision = Mock(return_value=None)
    module._review_unified = Mock()
    module._sync_review(connection.cursor_instance, 10, "approved", "")
    module._review_unified.assert_not_called()


def test_unified_nonpending_revision_cannot_be_approved_again(setup):
    module, connection, _, _ = setup
    module._unified_revision = Mock(return_value=unified(status="approved"))
    with pytest.raises(ValueError, match="not pending"):
        module._sync_review(connection.cursor_instance, 10, "approved", "")


def test_clarification_creates_new_unified_snapshot(setup):
    module, connection, _, _ = setup
    module._validated_revision = Mock(return_value=revision("changes_requested"))
    module._unified_revision = Mock(return_value=unified(status="changes_requested"))
    module._clone_unified = Mock()
    module._event = Mock()
    assert module.submit_verification(10, "Checked source") == 1
    module._clone_unified.assert_called_once_with(
        connection.cursor_instance, unified(status="changes_requested"),
        scope="comparison_only",
    )
    assert connection.committed
    assert any("UPDATE extraction_revisions" in sql for sql, _ in connection.cursor_instance.queries)


def test_clarification_must_have_reason(setup):
    module, _, _, _ = setup
    with pytest.raises(ValueError, match="reason"):
        module.submit_verification(10, " ")


def test_clarification_fails_if_mirror_cannot_reopen(setup):
    module, connection, _, _ = setup
    module._validated_revision = Mock(return_value=revision("changes_requested"))
    module._unified_revision = Mock(return_value=unified(status="pending"))
    with pytest.raises(ValueError, match="cannot be reopened"):
        module.submit_verification(10, "Correct details")
    assert connection.rolled_back


def test_resubmission_copies_both_structures_in_one_transaction(setup, candidates):
    module, connection, _, _ = setup
    module._validated_revision = Mock(return_value=revision("changes_requested"))
    module._candidates = Mock(return_value=candidates)
    module._sync_correction = Mock()
    module._event = Mock()
    corrected = {row["field_name"]: row["raw_value"] for row in candidates}
    corrected["proposed_annual_energy_consumption"] = "1900"
    corrected["reported_energy_saving_percent"] = "55"
    assert module.resubmit_revision(10, corrected, "Correction checked") == 2
    module._sync_correction.assert_called_once()
    updates = module._sync_correction.call_args.args[2]
    assert updates["proposed_annual_energy_consumption"][1] == Decimal("1900")
    assert updates["calculated_energy_saving_percent"][1] == Decimal("54.90")
    assert len(connection.cursor_instance.batches[0][1]) == 16
    assert connection.committed


def test_resubmission_rolls_back_if_unified_correction_fails(setup, candidates):
    module, connection, _, _ = setup
    module._validated_revision = Mock(return_value=revision("changes_requested"))
    module._candidates = Mock(return_value=candidates)
    module._sync_correction = Mock(side_effect=ValueError("Missing snapshot"))
    corrected = {row["field_name"]: row["raw_value"] for row in candidates}
    corrected["proposed_annual_energy_consumption"] = "1900"
    with pytest.raises(ValueError, match="Missing snapshot"):
        module.resubmit_revision(10, corrected, "Fix")
    assert connection.rolled_back
    assert not connection.cursor_instance.queries


def test_resubmission_rejects_no_changes(setup, candidates):
    module, _, _, _ = setup
    module._validated_revision = Mock(return_value=revision("changes_requested"))
    module._candidates = Mock(return_value=candidates)
    corrected = {row["field_name"]: row["raw_value"] for row in candidates}
    with pytest.raises(ValueError, match="No source values changed"):
        module.resubmit_revision(10, corrected, "No edits")


def test_resubmission_rejects_incomplete_comparison(setup):
    module, _, _, _ = setup
    with pytest.raises(ValueError, match="all 16"):
        module.resubmit_revision(10, {"test": "1"}, "Explain")


def test_resubmission_rejects_approved_legacy_revision(setup, candidates):
    module, connection, _, _ = setup
    module._validated_revision = Mock(return_value=revision("approved"))
    module._candidates = Mock(return_value=candidates)
    corrected = {row["field_name"]: row["raw_value"] for row in candidates}
    corrected["proposed_annual_energy_consumption"] = "1900"
    with pytest.raises(ValueError, match="awaiting review"):
        module.resubmit_revision(10, corrected, "Fix")
    assert connection.rolled_back


def test_pending_correction_marks_prior_unified_revision_superseded(setup):
    module, connection, _, _ = setup
    module._unified_revision = Mock(return_value=unified())
    module._review_unified = Mock()
    module._clone_unified = Mock()
    module._sync_correction(connection.cursor_instance, 10, {"field": ("1", 1)}, "Fix")
    module._review_unified.assert_called_once()
    assert module._review_unified.call_args.args[2] == "changes_requested"
    assert module._clone_unified.call_args.kwargs["scope"] == "comparison_only"


def test_clone_preserves_original_value_and_marks_only_corrected(setup):
    module, connection, revisions, values = setup
    original = [
        {
            "field_name": "annual_energy_consumption", "phase": "proposed",
            "value_kind": "calculated_estimate", "raw_source_value": "1993",
            "numeric_value": Decimal("1993"), "text_value": None,
            "unit": "kWh/year", "source_page": 4,
            "source_verified": False, "is_corrected": False,
            "correction_reason": None,
        },
        {
            "field_name": "nominal_speed_m_s", "phase": "existing",
            "value_kind": "engineering_specification", "raw_source_value": "1",
            "numeric_value": Decimal("1"), "text_value": None,
            "unit": "m/s", "source_page": 8,
            "source_verified": True, "is_corrected": False,
            "correction_reason": None,
        },
    ]
    module._unified_values = Mock(return_value=original)
    module._clone_unified(
        connection.cursor_instance, unified(status="changes_requested"),
        corrections={"proposed_annual_energy_consumption": ("1900", Decimal("1900"))},
        reason="Engineer correction",
    )
    saved = values.insert_engineering_values.call_args.args[2]
    assert saved[0]["raw_source_value"] == "1993"
    assert saved[0]["numeric_value"] == Decimal("1900")
    assert saved[0]["is_corrected"] is True
    assert saved[0]["source_verified"] is False
    assert saved[0]["correction_reason"] == "Engineer correction"
    assert saved[1]["numeric_value"] == Decimal("1")
    assert saved[1]["source_verified"] is True
    revisions.insert_revision.assert_called_once_with(
        connection.cursor_instance, 20, "full_values"
    )


def test_clone_requires_complete_source_snapshot(setup):
    module, connection, _, _ = setup
    module._unified_values = Mock(return_value=[])
    with pytest.raises(ValueError, match="no engineering values"):
        module._clone_unified(connection.cursor_instance, unified())


def test_validation_rejects_duplicate_or_missing_legacy_fields(setup, candidates):
    module, _, _, _ = setup
    with pytest.raises(ValueError, match="exactly 16"):
        module._validate(candidates[:-1], 1)
    with pytest.raises(ValueError, match="exactly 16"):
        module._validate(candidates[:-1] + candidates[:1], 1)


def test_cloned_text_field_keeps_source_and_new_value(setup):
    module, connection, _, values = setup
    module._unified_values = Mock(return_value=[{
        "field_name": "elevator_technology", "phase": "existing",
        "raw_source_value": "Original", "numeric_value": None,
        "text_value": "Original", "source_verified": False,
        "is_corrected": False, "correction_reason": None,
    }])
    module._clone_unified(
        connection.cursor_instance, unified(status="changes_requested"),
        corrections={"existing_elevator_technology": ("Updated", None)},
        reason="Technology verified",
    )
    item = values.insert_engineering_values.call_args.args[2][0]
    assert item["raw_source_value"] == "Original"
    assert item["text_value"] == "Updated"
    assert item["is_corrected"] is True
