"""Read-only MOD duplicate lookup and upload-page compatibility checks."""

import ast
import hashlib
from decimal import Decimal
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "enercal/enercal_upload.py"


class FakeCursor:
    def __init__(self, result=None):
        self.result = result
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params):
        self.queries.append((query, params))

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, cursor):
        self.cur = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def cursor(self):
        return self.cur


def lookup(result=None):
    # Load only the pure lookup function so Streamlit does not execute at import.
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef)
                    and node.name == "find_existing_document")
    module = ast.Module(body=[function], type_ignores=[])
    cursor = FakeCursor(result)
    namespace = {
        "hashlib": hashlib,
        "get_connection": lambda: FakeConnection(cursor),
    }
    exec(compile(module, str(PAGE), "exec"), namespace)
    return namespace["find_existing_document"], cursor


def test_duplicate_check_uses_unified_documents_and_checksum():
    find, cur = lookup((301,))
    content = b"%PDF-1.7 example"
    assert find("TEST-MOD-REV-001", content) == 301
    assert len(cur.queries) == 1
    query, params = cur.queries[0]
    assert "FROM project_documents d" in query
    assert "d.document_type = 'mod_pdf'" in query
    assert "JOIN modernisation_cases m" in query
    assert params == ("TEST-MOD-REV-001", hashlib.sha256(content).hexdigest())


def test_no_matching_unified_document_returns_none():
    find, _ = lookup(None)
    assert find("MOD1001", b"%PDF-1.7 example") is None


def test_legacy_only_record_is_not_used_by_duplicate_query():
    find, cur = lookup(None)
    assert find("MOD1001", b"%PDF-1.7 example") is None
    assert "source_documents" not in cur.queries[0][0]


def test_lookup_rejects_non_bytes():
    find, cur = lookup()
    with pytest.raises(ValueError, match="bytes"):
        find("MOD1001", "PDF as text")
    assert not cur.queries


def test_upload_page_uses_new_ids_and_preserves_preview():
    page = PAGE.read_text(encoding="utf-8")
    assert 'result["document_id"]' in page
    assert 'result["values_saved"]' in page
    assert 'result["comparison_fields_saved"]' in page
    assert "extract_mod_document(pdf_bytes)" in page
    assert "extract_specs(pdf_bytes)" in page
    assert "comparison_rows(comparison)" in page
    assert "Case Workspace" in page
    assert "source_documents" not in page


def test_upload_page_keeps_role_check_and_case_registration():
    tree = ast.parse(PAGE.read_text(encoding="utf-8"))
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert {"load_options", "create_case", "find_existing_document", "extract_report"} <= functions
    page = PAGE.read_text(encoding="utf-8")
    assert 'st.session_state.get("role") != "Admin"' in page
    assert "INSERT INTO modernisation_cases" in page
    assert "MAX_PDF_SIZE = 15 * 1024 * 1024" in page
