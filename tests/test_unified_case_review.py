"""Unit and compatibility checks for the five-table Case Workspace."""

import ast
import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services/unified_review_actions.py"
PAGE = ROOT / "enercal/enercal_review.py"

FIELDS = {
    "existing_elevator_technology", "proposed_elevator_technology",
    "existing_annual_energy_consumption", "proposed_annual_energy_consumption",
    "existing_annual_regenerative_energy", "proposed_annual_regenerative_energy",
    "existing_annual_electricity_expenditure", "proposed_annual_electricity_expenditure",
    "existing_lifetime_electricity_expenditure", "proposed_lifetime_electricity_expenditure",
    "existing_operational_co2e", "proposed_operational_co2e",
    "existing_embodied_co2e", "proposed_embodied_co2e",
    "reported_energy_saving_percent", "calculated_energy_saving_percent",
}

VALUE_COLUMNS = (
    "field_name", "phase", "value_kind", "raw_source_value", "numeric_value",
    "text_value", "unit", "source_page", "excel_sheet", "excel_cell",
    "excel_formula", "source_label", "source_note", "measured_on",
    "period_start", "period_end", "source_verified", "is_corrected", "correction_reason",
)


def comparison_values():
    result = []
    for name in sorted(FIELDS):
        phase, field = ("common", name)
        for prefix in ("existing_", "proposed_"):
            if name.startswith(prefix):
                phase, field = prefix[:-1], name[len(prefix):]
                break
        if name.endswith("elevator_technology"):
            value = "Traction"
            num, txt = None, value
        else:
            lookup = {
                "existing_annual_energy_consumption": "4213",
                "proposed_annual_energy_consumption": "1993",
                "existing_annual_electricity_expenditure": "1138",
                "proposed_annual_electricity_expenditure": "538",
                "calculated_energy_saving_percent": "52.69",
                "reported_energy_saving_percent": "52.69",
            }
            value = lookup.get(name, "10")
            num, txt = Decimal(value), None
        result.append({
            "field_name": field, "phase": phase,
            "value_kind": "calculated_estimate", "raw_source_value": value,
            "numeric_value": num, "text_value": txt, "unit": None,
            "source_page": 4, "excel_sheet": None, "excel_cell": None,
            "excel_formula": None, "source_label": name, "source_note": None,
            "measured_on": None, "period_start": None, "period_end": None,
            "source_verified": False, "is_corrected": False, "correction_reason": None,
        })
    return result


class FakeCursor:
    def __init__(self, latest=None, values=None):
        self.latest = latest
        self.values = values if values is not None else comparison_values()
        self.calls = []
        self.last = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        stmt = " ".join(sql.split())
        self.calls.append((stmt, params))
        if "FROM project_documents d JOIN modernisation_cases m" in stmt:
            self.last = [self.latest] if self.latest is not None else []
        elif "FROM engineering_values WHERE revision_id" in stmt:
            self.last = [tuple(row[column] for column in VALUE_COLUMNS) for row in self.values]
        elif "SELECT review_document_revision" in stmt:
            self.last = [(900,)]
        else:
            raise AssertionError(f"Unexpected SQL: {stmt}")

    def fetchone(self):
        return self.last.pop(0) if self.last else None

    def fetchall(self):
        result = list(self.last)
        self.last.clear()
        return result


class FakeConnection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, error_type, *_):
        self.rolled_back = error_type is not None
        self.committed = error_type is None
        return False

    def cursor(self):
        return self.cursor_obj


@pytest.fixture
def load_service(monkeypatch):
    def factory(*, status="pending", scope="full_values", kind="mod_pdf", superseded=False, values=None):
        latest = (kind, "test_fixture", 17, 1, scope, status, superseded)
        cursor = FakeCursor(latest, values)
        connection = FakeConnection(cursor)
        calls = {"inserted": [], "saved": []}
        database = ModuleType("database")
        database.get_connection = lambda: connection
        revisions = ModuleType("services.document_revision_repository")

        def insert_revision(cur, document_id, review_scope):
            calls["inserted"].append((cur, document_id, review_scope))
            return {"revision_id": 18, "revision_no": 2}

        revisions.insert_revision = insert_revision
        values_repo = ModuleType("services.engineering_value_repository")
        values_repo.VALUE_COLUMNS = VALUE_COLUMNS

        def insert_values(cur, revision_id, rows):
            calls["saved"].append((cur, revision_id, rows))
            return {"values_saved": len(rows)}

        values_repo.insert_engineering_values = insert_values
        legacy = ModuleType("services.enercal_review_repository")
        legacy.CALCULATED_FIELD = "calculated_energy_saving_percent"
        legacy.EXPECTED_FIELDS = FIELDS
        legacy.FIELD_LABELS = {name: name for name in FIELDS}
        legacy.OPTIONAL_NUMERIC_FIELDS = {
            "existing_annual_regenerative_energy", "proposed_annual_regenerative_energy",
            "existing_embodied_co2e", "proposed_embodied_co2e",
        }
        legacy.TECHNOLOGY_FIELDS = {
            "existing_elevator_technology", "proposed_elevator_technology"
        }
        for name, module in {
            "database": database,
            "services.document_revision_repository": revisions,
            "services.engineering_value_repository": values_repo,
            "services.enercal_review_repository": legacy,
        }.items():
            monkeypatch.setitem(sys.modules, name, module)
        spec = importlib.util.spec_from_file_location("_unified_review_test", SERVICE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, connection, calls
    return factory


def test_approve_full_values_forks_to_comparison_only(load_service):
    service, conn, actions = load_service()
    result = service.submit_comparison_review(7, "approved", "Checked comparison")
    assert result == {
        "document_id": 7, "revision_id": 18, "revision_no": 2,
        "review_status": "approved", "review_id": 900,
    }
    assert actions["inserted"][0][2] == "comparison_only"
    assert len(actions["saved"][0][2]) == 16
    reviews = [params for sql, params in conn.cursor_obj.calls if "review_document_revision" in sql]
    assert reviews[0][1] == "further_verification_required"
    assert reviews[1][1] == "approved"
    assert conn.committed


def test_approve_already_comparison_only_no_new_revision(load_service):
    service, conn, actions = load_service(scope="comparison_only")
    result = service.submit_comparison_review(7, "approved", "Checked")
    assert result["revision_no"] == 1
    assert actions["inserted"] == []
    assert actions["saved"] == []
    assert conn.committed


def test_iso_rejection_separates_full_values(load_service):
    service, conn, actions = load_service(kind="iso_excel")
    result = service.submit_comparison_review(7, "rejected", "Test workbook")
    assert result["review_status"] == "rejected"
    assert actions["inserted"][0][2] == "comparison_only"
    assert conn.committed


@pytest.mark.parametrize("decision", ["rejected", "changes_requested", "further_verification_required"])
def test_non_approval_requires_note_before_database(load_service, decision):
    service, conn, _ = load_service()
    with pytest.raises(ValueError, match="reason"):
        service.submit_comparison_review(7, decision, "  ")
    assert conn.cursor_obj.calls == []


@pytest.mark.parametrize("value", [None, False, 0, -1, "7"])
def test_bad_document_id_rejected_before_connection(load_service, value):
    service, conn, _ = load_service()
    with pytest.raises(ValueError, match="positive integer"):
        service.submit_comparison_review(value, "approved", "Okay")
    assert conn.cursor_obj.calls == []


def test_unknown_decision_rejected(load_service):
    service, conn, _ = load_service()
    with pytest.raises(ValueError, match="Invalid review decision"):
        service.submit_comparison_review(7, "complete", "Text")
    assert conn.cursor_obj.calls == []


@pytest.mark.parametrize("status", ["approved", "rejected", "changes_requested", "further_verification_required"])
def test_completed_revision_not_reviewed_again(load_service, status):
    service, conn, _ = load_service(status=status)
    with pytest.raises(ValueError, match="pending"):
        service.submit_comparison_review(7, "approved", "Text")
    assert conn.rolled_back


def test_superseded_document_rejected(load_service):
    service, conn, _ = load_service(superseded=True)
    with pytest.raises(ValueError, match="latest document"):
        service.submit_comparison_review(7, "approved", "Test")
    assert conn.rolled_back


def test_unsupported_document_type_rejected(load_service):
    service, conn, _ = load_service(kind="meter_csv")
    with pytest.raises(ValueError, match="Only MOD and ISO"):
        service.submit_comparison_review(7, "approved", "Test")
    assert conn.rolled_back


def test_correction_creates_immutable_snapshot_without_changing_source(load_service):
    original = comparison_values()
    service, conn, actions = load_service(values=original)
    edited = {name: next(
        (str(v["numeric_value"]) if v["numeric_value"] is not None else v["text_value"])
        for v in original if (v["phase"] + "_" if v["phase"] != "common" else "") + v["field_name"] == name
    ) for name in FIELDS}
    edited["existing_annual_electricity_expenditure"] = "1139"
    result = service.submit_mod_correction(7, edited, "Integration test only")
    assert result["revision_no"] == 2
    assert actions["inserted"][0][2] == "comparison_only"
    saved = actions["saved"][0][2]
    modified = next(v for v in saved if v["phase"] == "existing" and v["field_name"] == "annual_electricity_expenditure")
    unmodified = next(v for v in saved if v["phase"] == "proposed" and v["field_name"] == "annual_electricity_expenditure")
    assert modified["raw_source_value"] == "1138"
    assert modified["numeric_value"] == Decimal("1139")
    assert modified["is_corrected"] is True
    assert modified["source_verified"] is False
    assert unmodified["is_corrected"] is False
    assert original[0]["is_corrected"] is False
    assert conn.committed


def test_no_correction_rejected_and_rolled_back(load_service):
    original = comparison_values()
    service, conn, actions = load_service(values=original)
    edited = {(v["phase"] + "_" if v["phase"] != "common" else "") + v["field_name"]:
              str(v["numeric_value"]) if v["numeric_value"] is not None else v["text_value"]
              for v in original}
    with pytest.raises(ValueError, match="No comparison values changed"):
        service.submit_mod_correction(7, edited, "No changes")
    assert conn.rolled_back
    assert actions["inserted"] == []


def test_iso_cannot_use_mod_correction(load_service):
    service, conn, _ = load_service(kind="iso_excel")
    with pytest.raises(ValueError, match="MOD PDF"):
        service.submit_mod_correction(7, {}, "test")
    assert conn.rolled_back


def test_approved_cannot_be_corrected(load_service):
    service, conn, _ = load_service(status="approved")
    with pytest.raises(ValueError, match="unapproved"):
        service.submit_mod_correction(7, {}, "test")
    assert conn.rolled_back


def test_invalid_correction_explanation(load_service):
    service, conn, _ = load_service()
    with pytest.raises(ValueError, match="correction reason"):
        service.submit_mod_correction(7, {}, "")
    assert conn.cursor_obj.calls == []


def test_corrected_values_are_not_auto_verified(load_service):
    service, _, _ = load_service()
    original = comparison_values()
    original[0]["source_verified"] = True
    edits = {(v["phase"] + "_" if v["phase"] != "common" else "") + v["field_name"]:
             str(v["numeric_value"]) if v["numeric_value"] is not None else v["text_value"]
             for v in original}
    edits["existing_annual_electricity_expenditure"] = "1139"
    result = service._corrected_values(original, edits, "test")
    change = next(v for v in result if v["phase"] == "existing" and v["field_name"] == "annual_electricity_expenditure")
    assert not change["source_verified"]


def test_service_uses_only_five_workflow_tables():
    text = SERVICE.read_text()
    forbidden = [
        "source_documents", "extraction_revisions", "extraction_candidates",
        "enercal_specifications", "iso_assessments", "iso_documents",
        "iso_extracted_values", "enercal_review_events",
    ]
    assert not any(term in text for term in forbidden)
    assert "review_document_revision" in text
    assert "insert_engineering_values" in text


def test_page_uses_unified_repository_without_legacy_calls():
    text = PAGE.read_text()
    assert "list_workspace_cases" in text
    assert "list_case_documents" in text
    assert "get_revision_snapshot" in text
    assert "submit_comparison_review" in text
    assert "submit_mod_correction" in text
    for old in ("get_review_documents", "get_review_details", "review_iso_assessment", "iso_assessments", "source_documents"):
        assert old not in text


def test_page_role_check_precedes_data_loading():
    tree = ast.parse(PAGE.read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    first = main.body[0]
    assert isinstance(first, ast.If)
    assert "Admin" in ast.unparse(first.test)
    assert "st.stop()" in ast.unparse(first)


def test_all_ui_sections_present():
    page = PAGE.read_text()
    for label in ("Case Workspace", "PDF and data", "ISO Excel data", "Engineering review", "History", "Preview MOD PDF page 4"):
        assert label in page
    assert "session_state" in page
    assert "st.download_button" in page


def test_page_and_service_compile():
    ast.parse(PAGE.read_text())
    ast.parse(SERVICE.read_text())
