"""Offline safeguards for additive historical evidence backfill."""

import hashlib
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import backfill_legacy_evidence as backfill


CANDIDATES = [
    *[
        {"field_name": f"{phase}_{field}",
         "raw_value": "Typical Traction Elevator" if field == "elevator_technology" else "100",
         "numeric_value": None if field == "elevator_technology" else Decimal("100"),
         "unit": "$ /year" if field == "annual_electricity_expenditure" else "kWh/year"}
        for phase in ("existing", "proposed")
        for field in ("elevator_technology", *backfill.COMPARISON_FIELDS)
    ],
    {"field_name": "reported_energy_saving_percent", "raw_value": "0", "numeric_value": Decimal(0), "unit": "%"},
    {"field_name": "calculated_energy_saving_percent", "raw_value": "0", "numeric_value": Decimal(0), "unit": "%"},
]
SPECS = [("lifetime_years", "25", "years", 8), ("country", "Singapore", None, 8)]


class FakeCursor:
    def __init__(self, fetches=None, rows=None):
        self.fetches = list(fetches or [])
        self.result_rows = list(rows or [])
        self.sql = []
        self.rowcount = 1
        self.description = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.fetches.pop(0) if self.fetches else None

    def fetchall(self):
        return self.result_rows


class Connection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.rollbacks = 0
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        self.committed = exc_type is None

    def cursor(self):
        return self.cursor_obj

    def rollback(self):
        self.rollbacks += 1


class CursorContext(FakeCursor):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_comparison_has_exact_16_fields():
    assert len(CANDIDATES) == len(backfill.COMPARISON_NAMES) == 16
    result = backfill._build_comparison(CANDIDATES, SPECS)
    assert result["lifetime_years"] == 25
    assert result["currency_symbol"] == "$ "
    assert result["rows"]["existing"]["annual_energy_consumption"] == 100


def test_missing_currency_rejected():
    changed = [dict(r) for r in CANDIDATES]
    for row in changed:
        if row["field_name"] == "existing_annual_electricity_expenditure":
            row["unit"] = "unknown"
    with pytest.raises(ValueError, match="currency"):
        backfill._build_comparison(changed, SPECS)


def test_lifetime_unit_fallback():
    changed = [dict(r) for r in CANDIDATES]
    for row in changed:
        if row["field_name"] == "existing_operational_co2e":
            row["unit"] = "kgCO2e/lifetime 20 years"
    assert backfill._build_comparison(changed, [])["lifetime_years"] == 20


def test_missing_lifetime_rejected():
    with pytest.raises(ValueError, match="lifetime"):
        backfill._build_comparison(CANDIDATES, [])


def test_mod_correction_preserves_original_and_marks_only_changed_field():
    old = {"revision_no": 1, "review_status": "pending", "review_note": None,
           "candidates": [dict(r) for r in CANDIDATES]}
    new = {"revision_no": 2, "review_status": "approved", "review_note": "Simulated test change",
           "candidates": [dict(r) for r in CANDIDATES]}
    changed = next(r for r in new["candidates"] if r["field_name"] == "existing_annual_electricity_expenditure")
    changed["numeric_value"] = Decimal("101")
    changed["raw_value"] = "101"
    mapped = backfill._build_mod_values(new, SPECS, old, 53)
    altered = next(r for r in mapped if r["phase"] == "existing" and r["field_name"] == "annual_electricity_expenditure")
    assert altered["numeric_value"] == Decimal("101")
    assert altered["raw_source_value"] == "101"
    assert altered["is_corrected"] is True
    assert altered["source_verified"] is False
    assert "Simulated test change" == altered["correction_reason"]
    assert "original status approved" in altered["source_note"]
    assert sum(row["is_corrected"] for row in mapped) == 1
    assert next(r for r in old["candidates"] if r["field_name"] == "existing_annual_electricity_expenditure")["numeric_value"] == 100


def test_mod_first_snapshot_has_no_corrections():
    first = {"revision_no": 1, "review_status": "pending", "review_note": None,
             "candidates": [dict(r) for r in CANDIDATES]}
    mapped = backfill._build_mod_values(first, SPECS, None, 53)
    assert len(mapped) == 18
    assert all(not row["is_corrected"] and not row["source_verified"] for row in mapped)


def test_mod_corrected_reason_fallback():
    first = {"revision_no": 1, "review_status": "pending", "review_note": None,
             "candidates": [dict(r) for r in CANDIDATES]}
    second = {**first, "revision_no": 2, "candidates": [dict(r) for r in CANDIDATES]}
    second["candidates"][0]["raw_value"] = "Different Technology"
    row = next(v for v in backfill._build_mod_values(second, SPECS, first, 53)
               if v["field_name"] == "elevator_technology" and v["phase"] == "existing")
    assert row["is_corrected"]
    assert row["correction_reason"]


def test_review_pending_is_noop():
    cursor = FakeCursor()
    backfill._review(cursor, 5, "pending", None)
    assert cursor.sql == []


def test_review_uses_original_timestamp_when_present():
    cursor = FakeCursor()
    timestamp = datetime(2026, 11, 1, tzinfo=timezone.utc)
    backfill._review(cursor, 5, "approved", "Legacy note", timestamp)
    assert cursor.sql[-1][1] == ("approved", "Legacy note", timestamp, 5)


def test_review_nonapproval_gets_reason():
    cursor = FakeCursor()
    backfill._review(cursor, 3, "further_verification_required", "")
    assert cursor.sql[0][1][1]


def test_review_rowcount_failure_aborts():
    cursor = FakeCursor()
    cursor.rowcount = 0
    with pytest.raises(RuntimeError):
        backfill._review(cursor, 3, "approved", "note")


def test_case_with_other_unified_document_is_refused():
    cursor = FakeCursor(fetches=[(22,)])
    docs = [{"case_id": 4, "case_ref": "MOD1004", "document_type": "mod_pdf"}]
    with pytest.raises(ValueError, match="already contains"):
        backfill._check_no_same_type_document(cursor, docs)


def test_multiple_missing_same_type_refused():
    cursor = FakeCursor(fetches=[None, None])
    docs = [
        {"case_id": 4, "case_ref": "MOD1004", "document_type": "mod_pdf"},
        {"case_id": 4, "case_ref": "MOD1004", "document_type": "mod_pdf"},
    ]
    with pytest.raises(ValueError, match="Multiple"):
        backfill._check_no_same_type_document(cursor, docs)


def test_empty_apply_without_backup_is_blocked():
    with pytest.raises(ValueError, match="expected-missing"):
        backfill.run(apply=True)


def test_nonexistent_backup_is_blocked():
    with pytest.raises(ValueError, match="backup"):
        backfill.run(apply=True, expected_missing=11, backup="/does/not/exist.dump")


def test_short_backup_is_blocked(tmp_path):
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"123")
    with pytest.raises(ValueError, match="backup"):
        backfill.run(apply=True, expected_missing=11, backup=archive)


def test_dry_run_does_not_mutate():
    conn = Connection(CursorContext())
    with patch.object(backfill, "get_connection", return_value=conn), patch.object(backfill, "_inventory", return_value=[]):
        assert backfill.run() == 0
    assert conn.rollbacks == 1
    assert all("INSERT" not in sql and "UPDATE" not in sql for sql, _ in conn.cursor_obj.sql)
    assert any("READ ONLY" in sql for sql, _ in conn.cursor_obj.sql)


def test_apply_count_guard_rolls_back(tmp_path):
    archive = tmp_path / "backup.dump"
    archive.write_bytes(b"x" * 4096)
    conn = Connection(CursorContext())
    with patch.object(backfill, "get_connection", return_value=conn), patch.object(backfill, "_inventory", return_value=[]):
        with pytest.raises(ValueError, match="Expected 11"):
            backfill.run(apply=True, backup=archive, expected_missing=11)
    assert not conn.committed


def test_iso_value_preserves_formulas_notes_and_phase():
    cursor = FakeCursor(rows=[
        ("before_annual_energy_kwh", "41933.05", Decimal("41933.05"), "kWh", "calculated", None,
         "Classification", "C12", "=C10*365", "Annual energy", "Synthetic Before"),
        ("after_efficiency_class", "A", None, None, "calculated", None,
         "Classification", "D11", None, "Energy class", None),
    ])
    cursor.description = [SimpleNamespace(name=x) for x in (
        "field_name", "value_text", "numeric_value", "unit", "data_kind", "pdf_page",
        "excel_sheet", "excel_cell", "excel_formula", "source_label", "source_note"
    )]
    mapped = backfill._get_iso_source(cursor, 1)
    before = next(r for r in mapped if r["phase"] == "existing")
    assert before["excel_formula"] == "=C10*365"
    assert before["raw_source_value"] == "=C10*365"
    assert "Synthetic Before" in before["source_note"]
    assert before["source_verified"] is False
    assert any(r["phase"] == "proposed" for r in mapped)


def test_iso_empty_values_rejected():
    cursor = FakeCursor(rows=[])
    cursor.description = [SimpleNamespace(name="field_name")]
    with pytest.raises(ValueError, match="no fields"):
        backfill._get_iso_source(cursor, 1)


def test_new_revision_defaults_comparison_only_and_copies_created_at():
    cursor = FakeCursor(fetches=[(99,)])
    stamp = datetime.now(timezone.utc)
    assert backfill._new_revision(cursor, 15, 2, stamp) == 99
    assert cursor.sql[-1][1] == (15, 2, stamp)
    assert "comparison_only" in cursor.sql[-1][0]


def test_document_insert_keeps_file_bytes_and_checksum():
    cursor = FakeCursor(fetches=[(123,)])
    content = b"%PDF-test"
    doc = {"case_id": 15, "document_type": "mod_pdf", "classification": "test_fixture",
           "lift_id": None, "filename": "a.pdf", "checksum": hashlib.sha256(content).hexdigest(),
           "content": content, "source_date": datetime.now(timezone.utc)}
    assert backfill._insert_document(cursor, doc) == 123
    assert cursor.sql[0][1][6] == content
    assert cursor.sql[0][1][2] == "test_fixture"


def test_duplicate_document_insert_aborts():
    cursor = FakeCursor(fetches=[None])
    doc = {"case_id": 1, "document_type": "mod_pdf", "classification": "test_fixture",
           "lift_id": None, "filename": "a.pdf", "checksum": "a" * 64,
           "content": b"%PDF-", "source_date": datetime.now(timezone.utc)}
    with pytest.raises(RuntimeError, match="already appeared"):
        backfill._insert_document(cursor, doc)
