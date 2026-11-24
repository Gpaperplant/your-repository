import hashlib
import importlib.util
import sys
from io import BytesIO
from pathlib import Path
from types import ModuleType
from zipfile import ZipFile

import pytest


FILE = Path(__file__).resolve().parents[1] / "services/project_document_repository.py"


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.queries.append((sql, params))
        database = self.connection
        statement = " ".join(sql.split())
        if "SELECT m.case_id, e.elevator_ref" in statement:
            self.row = database.cases.get(params[0])
        elif "INSERT INTO project_documents" in statement:
            if database.fail_insert:
                raise RuntimeError("Database write failed")
            case_id, doc_type, source, lift, name, checksum, content = params
            key = (case_id, doc_type, checksum)
            if key in database.documents:
                self.row = None
            else:
                document_id = database.next_id
                database.next_id += 1
                database.documents[key] = (
                    document_id, case_id, doc_type, name, checksum,
                    content, lift, source, "2026-11-23",
                )
                self.row = (document_id,)
        elif "SELECT document_id FROM project_documents" in statement:
            match = database.documents.get(tuple(params))
            self.row = (match[0],) if match else None
        elif "SELECT document_id, case_id, document_type" in statement:
            self.row = next(
                (value for value in database.documents.values() if value[0] == params[0]),
                None,
            )
        else:
            raise AssertionError(f"Unexpected query: {sql}")
        return self

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self):
        self.cases = {"MOD1001": (7, "OP13 (L)"), "TEST1001": (8, "TEST-LIFT-001")}
        self.documents = {}
        self.next_id = 1
        self.fail_insert = False
        self.committed = False
        self.rolled_back = False
        self.cursor_object = FakeCursor(self)

    def __enter__(self):
        self.snapshot = self.documents.copy()
        return self

    def __exit__(self, error, *_):
        if error:
            self.documents = self.snapshot
            self.rolled_back = True
        else:
            self.committed = True
        return False

    def cursor(self):
        return self.cursor_object


@pytest.fixture
def setup(monkeypatch):
    database = ModuleType("database")
    connection = FakeConnection()
    database.get_connection = lambda: connection
    monkeypatch.setitem(sys.modules, "database", database)
    spec = importlib.util.spec_from_file_location("_project_documents_under_test", FILE)
    repository = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(repository)
    return repository, connection


def mod(**kwargs):
    return {
        "case_ref": "MOD1001", "document_type": "mod_pdf",
        "filename": "EnerCal.pdf", "file_bytes": b"%PDF-1.7 sample",
        **kwargs,
    }


def excel_bytes():
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("xl/workbook.xml", "<workbook />")
    return output.getvalue()


def test_upload_preserves_original_bytes_and_hash(setup):
    repository, conn = setup
    original = b"%PDF-1.7 sample"
    result = repository.save_project_document(**mod())
    stored = next(iter(conn.documents.values()))
    assert result == {"document_id": 1, "case_id": 7, "document_type": "mod_pdf", "created": True}
    assert stored[4] == hashlib.sha256(original).hexdigest()
    assert stored[5] == original
    assert conn.committed


def test_duplicate_upload_is_idempotent(setup):
    repository, conn = setup
    first = repository.save_project_document(**mod())
    second = repository.save_project_document(**mod())
    assert first["document_id"] == second["document_id"]
    assert not second["created"]
    assert len(conn.documents) == 1


def test_duplicate_lookup_accepts_multiline_sql(setup):
    repository, conn = setup
    document_id = repository.save_project_document(**mod())["document_id"]
    checksum = hashlib.sha256(mod()["file_bytes"]).hexdigest()
    cursor = conn.cursor_object
    cursor.execute(
        """
        SELECT document_id
        FROM project_documents
        WHERE case_id = %s
          AND document_type = %s
          AND file_sha256 = %s
        """,
        (7, "mod_pdf", checksum),
    )
    assert cursor.fetchone() == (document_id,)


def test_different_document_types_with_same_content(setup):
    repository, conn = setup
    first = repository.save_project_document(**mod())
    second = repository.save_project_document(**{
        **mod(), "document_type": "iso_test_pdf", "filename": "Test.pdf",
    })
    assert first["document_id"] != second["document_id"]
    assert len(conn.documents) == 2


def test_valid_iso_accepts_normalised_lift_id(setup):
    repository, _ = setup
    result = repository.save_project_document(
        case_ref="MOD1001", document_type="iso_excel",
        filename="ISO.xlsx", file_bytes=excel_bytes(),
        reported_elevator_ref="OP13(L)", source_classification="real",
    )
    assert result["created"]


@pytest.mark.parametrize("lift", [None, "", "wrong lift"])
def test_iso_requires_matching_lift(setup, lift):
    repository, conn = setup
    with pytest.raises(ValueError, match="elevator reference|elevator reference does not match"):
        repository.save_project_document(
            case_ref="MOD1001", document_type="iso_excel",
            filename="ISO.xlsx", file_bytes=excel_bytes(),
            reported_elevator_ref=lift,
        )
    assert not conn.documents


def test_mod_pdf_for_test_lift_is_always_test_fixture(setup):
    repository, connection = setup
    result = repository.save_project_document(**{**mod(), "case_ref": "TEST1001"})
    assert result["created"]
    saved = repository.get_project_document(result["document_id"])
    assert saved["source_classification"] == "test_fixture"


def test_mod_pdf_cannot_mark_a_test_lift_as_real(setup):
    repository, connection = setup
    result = repository.save_project_document(**{
        **mod(), "case_ref": "TEST1001", "source_classification": "real"
    })
    saved = repository.get_project_document(result["document_id"])
    assert saved["source_classification"] == "test_fixture"


def test_mod_pdf_for_non_test_lift_keeps_unknown_provenance(setup):
    repository, connection = setup
    result = repository.save_project_document(**mod())
    saved = repository.get_project_document(result["document_id"])
    assert saved["source_classification"] == "unknown"


def test_test_fixture_exception_does_not_apply_to_real_data(setup):
    repository, conn = setup
    record = {
        "case_ref": "TEST1001", "document_type": "iso_excel",
        "filename": "ISO.xlsx", "file_bytes": excel_bytes(),
        "reported_elevator_ref": "OP13 (L)",
    }
    with pytest.raises(ValueError, match="does not match"):
        repository.save_project_document(**record)
    result = repository.save_project_document(**record, source_classification="test_fixture")
    assert result["created"]
    assert len(conn.documents) == 1


@pytest.mark.parametrize("changes", [
    {"document_type": "unknown"},
    {"filename": "report.exe"},
    {"filename": "../EnerCal.pdf"},
    {"filename": ""},
    {"file_bytes": b""},
    {"file_bytes": "not bytes"},
    {"source_classification": "verified"},
    {"case_ref": ""},
])
def test_invalid_upload_inputs(setup, changes):
    repository, conn = setup
    with pytest.raises(ValueError):
        repository.save_project_document(**{**mod(), **changes})
    assert not conn.documents


def test_invalid_pdf_and_excel_content(setup):
    repository, _ = setup
    with pytest.raises(ValueError, match="Invalid PDF"):
        repository.save_project_document(**{**mod(), "file_bytes": b"not a pdf"})
    with pytest.raises(ValueError, match="Invalid Excel"):
        repository.save_project_document(
            case_ref="MOD1001", document_type="iso_excel",
            filename="ISO.xlsx", file_bytes=b"PKnotzip",
            reported_elevator_ref="OP13 (L)",
        )


def test_elevator_reference_must_be_text(setup):
    repository, _ = setup
    with pytest.raises(ValueError, match="must be text"):
        repository.save_project_document(**{
            **mod(), "reported_elevator_ref": 123,
        })


def test_csv_requires_utf8(setup):
    repository, _ = setup
    with pytest.raises(ValueError, match="UTF-8"):
        repository.save_project_document(
            case_ref="MOD1001", document_type="meter_csv",
            filename="meter.csv", file_bytes=b"\xff\xfe",
        )


def test_case_must_exist(setup):
    repository, conn = setup
    with pytest.raises(ValueError, match="not found"):
        repository.save_project_document(**{**mod(), "case_ref": "MISSING"})
    assert not conn.documents


def test_database_failure_rolls_back(setup):
    repository, conn = setup
    conn.fail_insert = True
    with pytest.raises(RuntimeError, match="Database write failed"):
        repository.save_project_document(**mod())
    assert conn.rolled_back
    assert not conn.documents


def test_retrieve_original_document(setup):
    repository, _ = setup
    document_id = repository.save_project_document(**mod())["document_id"]
    saved = repository.get_project_document(document_id)
    assert saved["filename"] == "EnerCal.pdf"
    assert saved["file_content"] == b"%PDF-1.7 sample"
    assert saved["document_type"] == "mod_pdf"
    with pytest.raises(ValueError, match="not found"):
        repository.get_project_document(999)


def test_queries_use_bound_parameters(setup):
    repository, conn = setup
    repository.save_project_document(**mod())
    queries = conn.cursor_object.queries
    assert len(queries) == 2
    assert all("%s" in statement for statement, _ in queries)
    assert all("MOD1001" not in statement for statement, _ in queries)
    assert any("ON CONFLICT" in statement for statement, _ in queries)
