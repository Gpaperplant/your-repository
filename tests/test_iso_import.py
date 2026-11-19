"""Regression tests for ISO Excel-only import and database persistence."""

import hashlib
import importlib.util
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def override(modules):
    original = {key: sys.modules.get(key) for key in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for key, value in original.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALUES = [
    {"field": "lift_id", "value": "OP13 (L)", "kind": "identity", "sheet": "Lift parameters", "cell": "C2"},
    {"field": "rated_load_kg", "value": "1635", "kind": "specification", "unit": "kg", "sheet": "Lift parameters", "cell": "C5"},
]


class FakeCursor:
    def __init__(self, answers=()):
        self.answers = list(answers)
        self.queries = []
        self.bulk = []

    def __enter__(self): return self
    def __exit__(self, *_): return False
    def execute(self, sql, values=()): self.queries.append((sql, values))
    def executemany(self, sql, values): self.bulk.append((sql, values))
    def fetchone(self): return self.answers.pop(0) if self.answers else None


class FakeConnection:
    def __init__(self, answers=()):
        self.cursor_obj = FakeCursor(answers)
        self.committed = False
        self.rolled_back = False

    def __enter__(self): return self
    def __exit__(self, exc_type, *_):
        self.committed = exc_type is None
        self.rolled_back = exc_type is not None
        return False
    def cursor(self): return self.cursor_obj


def load_repository(conn):
    db = ModuleType("database")
    db.get_connection = lambda: conn
    with override({"database": db}):
        return load("services.iso_repository", "services/iso_repository.py")


def load_importer(extracted=None, case=(7, "OP13(L)"), saved=None):
    extracted = VALUES if extracted is None else extracted
    extract_module = ModuleType("services.iso_excel_extractor")
    calls = []

    def extract(raw):
        calls.append(("extract", raw))
        return extracted

    extract_module.extract_iso_excel = extract
    repo_module = ModuleType("services.iso_repository")
    repo_module.normalise_lift_id = lambda val: "".join(c for c in str(val or "").upper() if c.isalnum())
    repo_module.get_case_elevator = lambda ref: case

    def store(**kwargs):
        calls.append(("save", kwargs))
        return {"assessment_id": 12, "created": True} if saved is None else saved(**kwargs)

    repo_module.save_iso_excel_assessment = store
    with override({"services.iso_excel_extractor": extract_module, "services.iso_repository": repo_module}):
        importer = load("_isolated_iso_import", "services/iso_import_service.py")
    return importer, calls


class ISOImportTests(unittest.TestCase):
    def test_valid_workbook_checks_case_and_stores_original(self):
        importer, calls = load_importer()
        data = b"PK\x03\x04demo"
        saved = importer.process_iso_excel("MOD1007", "OP13.xlsx", data)
        self.assertEqual(saved["lift_id"], "OP13 (L)")
        self.assertEqual(saved["excel_fields"], 2)
        self.assertEqual(calls[1][1]["checksum"], hashlib.sha256(data).hexdigest())
        self.assertEqual(calls[1][1]["file_bytes"], data)

    def test_wrong_case_rejected_before_storage(self):
        importer, calls = load_importer(case=(7, "OP21"))
        with self.assertRaisesRegex(ValueError, "belongs to"):
            importer.process_iso_excel("MOD1007", "OP13.xlsx", b"PK\x03\x04")
        self.assertEqual(len(calls), 1)

    def test_no_lift_id(self):
        importer, calls = load_importer(extracted=[VALUES[1]])
        with self.assertRaisesRegex(ValueError, "lift reference"):
            importer.process_iso_excel("MOD1007", "OP13.xlsx", b"PK\x03\x04")
        self.assertEqual(len(calls), 1)

    def test_invalid_extension_rejected_without_extraction(self):
        importer, calls = load_importer()
        with self.assertRaisesRegex(ValueError, "valid ISO"):
            importer.process_iso_excel("MOD1007", "OP13.pdf", b"PK\x03\x04")
        self.assertFalse(calls)

    def test_oversize_rejected(self):
        importer, calls = load_importer()
        with self.assertRaisesRegex(ValueError, "15 MB"):
            importer.process_iso_excel("MOD1007", "OP13.xlsx", b"PK" + b"1" * (15 * 1024 * 1024))
        self.assertFalse(calls)


class ISORepositoryTests(unittest.TestCase):
    checksum = "a" * 64

    def test_first_excel_only_assessment_is_atomic(self):
        conn = FakeConnection([(7,), None, None, (15,), (24,)])
        repo = load_repository(conn)
        saved = repo.save_iso_excel_assessment(7, "OP13 (L)", "OP13.xlsx", b"source", VALUES, self.checksum)
        self.assertEqual(saved, {"assessment_id": 15, "created": True})
        self.assertTrue(conn.committed)
        queries = [sql for sql, _ in conn.cursor_obj.queries]
        self.assertIn("FOR UPDATE", queries[0])
        self.assertTrue(any("VALUES (%s, %s, NULL, %s)" in sql for sql in queries))
        self.assertTrue(any("'iso_excel'" in sql for sql in queries))
        self.assertEqual(len(conn.cursor_obj.bulk[0][1]), 2)
        self.assertEqual({row[0] for row in conn.cursor_obj.bulk[0][1]}, {24})

    def test_existing_case_excel_is_deduplicated(self):
        conn = FakeConnection([(7,), (4, "OP13(L)"), (4,)])
        repo = load_repository(conn)
        result = repo.save_iso_excel_assessment(7, "OP13 (L)", "OP13.xlsx", b"source", VALUES, self.checksum)
        self.assertEqual(result, {"assessment_id": 4, "created": False})
        self.assertTrue(conn.committed)
        self.assertFalse(any("INSERT INTO" in sql for sql, _ in conn.cursor_obj.queries))

    def test_case_not_found_rolls_back(self):
        conn = FakeConnection([None])
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "case not found"):
            repo.save_iso_excel_assessment(7, "OP13", "OP13.xlsx", b"x", VALUES, self.checksum)
        self.assertTrue(conn.rolled_back)

    def test_historical_elevator_mismatch_rolls_back(self):
        conn = FakeConnection([(7,), (2, "OP21")])
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "already contains ISO data"):
            repo.save_iso_excel_assessment(7, "OP13", "OP13.xlsx", b"x", VALUES, self.checksum)
        self.assertTrue(conn.rolled_back)

    def test_duplicate_extracted_fields_rejected_before_connect(self):
        conn = FakeConnection()
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "Duplicate or invalid"):
            repo.save_iso_excel_assessment(7, "OP13", "OP13.xlsx", b"x", VALUES * 2, self.checksum)
        self.assertEqual(conn.cursor_obj.queries, [])

    def test_invalid_checksum_rejected_before_connect(self):
        conn = FakeConnection()
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "checksum"):
            repo.save_iso_excel_assessment(7, "OP13", "OP13.xlsx", b"x", VALUES, "bad")
        self.assertFalse(conn.cursor_obj.queries)

    def test_numeric_values_do_not_turn_text_into_zero(self):
        repo = load_repository(FakeConnection())
        self.assertIsNone(repo._number("not a number"))
        self.assertIsNone(repo._number(False))
        self.assertEqual(repo._number("1,635"), 1635)

    def test_database_migration_is_additive(self):
        sql = (ROOT / "database/migrations/012_iso_excel_only.sql").read_text()
        self.assertIn("ALTER COLUMN pdf_sha256 DROP NOT NULL", sql)
        self.assertIn("WHERE pdf_sha256 IS NULL", sql)
        self.assertNotIn("DROP TABLE", sql.upper())




class ISOApprovalTests(unittest.TestCase):
    def test_approve_latest_pending_assessment(self):
        conn = FakeConnection([(7,), (15, "pending"), (15,)])
        repo = load_repository(conn)
        self.assertEqual(repo.review_iso_assessment("MOD1007", 15, "approved", "Checked source"), "approved")
        self.assertTrue(conn.committed)
        sql, params = conn.cursor_obj.queries[-1]
        self.assertIn("review_status = %s", sql)
        self.assertIn("reviewed_at = NOW()", sql)
        self.assertEqual(params, ("approved", "Checked source", 15, 7))
        self.assertIn("FOR UPDATE", conn.cursor_obj.queries[0][0])

    def test_reject_with_note(self):
        conn = FakeConnection([(7,), (15, "pending"), (15,)])
        repo = load_repository(conn)
        self.assertEqual(repo.review_iso_assessment("MOD1007", 15, "rejected", "Bad formula"), "rejected")
        self.assertEqual(conn.cursor_obj.queries[-1][1][0:2], ("rejected", "Bad formula"))

    def test_rejection_requires_reason_before_db(self):
        conn = FakeConnection()
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "reason"):
            repo.review_iso_assessment("MOD1007", 15, "rejected")
        self.assertFalse(conn.cursor_obj.queries)

    def test_unknown_decision_rejected_before_db(self):
        conn = FakeConnection()
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "decision"):
            repo.review_iso_assessment("MOD1007", 15, "ready")
        self.assertFalse(conn.cursor_obj.queries)

    def test_stale_assessment_not_approved(self):
        conn = FakeConnection([(7,), (16, "pending")])
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "latest"):
            repo.review_iso_assessment("MOD1007", 15, "approved")
        self.assertFalse(conn.committed)
        self.assertFalse(any("UPDATE iso_assessments" in q for q, _ in conn.cursor_obj.queries))

    def test_approved_cannot_be_approved_twice(self):
        conn = FakeConnection([(7,), (15, "approved")])
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "pending"):
            repo.review_iso_assessment("MOD1007", 15, "approved")
        self.assertFalse(conn.committed)

    def test_missing_case_rejected(self):
        conn = FakeConnection([None])
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "case not found"):
            repo.review_iso_assessment("MOD1007", 15, "approved")
        self.assertFalse(conn.committed)

    def test_no_iso_workbook_rejected(self):
        conn = FakeConnection([(7,), None])
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "latest ISO"):
            repo.review_iso_assessment("MOD1007", 15, "approved")
        self.assertFalse(conn.committed)

    def test_update_race_rolls_back(self):
        conn = FakeConnection([(7,), (15, "pending"), None])
        repo = load_repository(conn)
        with self.assertRaisesRegex(ValueError, "modified"):
            repo.review_iso_assessment("MOD1007", 15, "approved")
        self.assertFalse(conn.committed)

    def test_approval_migration_has_safe_defaults(self):
        sql = (ROOT / "database/migrations/013_iso_excel_approval.sql").read_text()
        self.assertIn("DEFAULT 'pending'", sql)
        self.assertIn("reviewed_at TIMESTAMPTZ", sql)
        self.assertIn("review_note TEXT", sql)
        self.assertIn("review_status IN ('pending', 'approved', 'rejected')", sql)
        self.assertNotIn("DROP TABLE", sql)
        self.assertNotIn("TRUNCATE", sql)

if __name__ == "__main__":
    unittest.main()
