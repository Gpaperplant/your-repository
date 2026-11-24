from pathlib import Path
import re

SQL = (
    Path(__file__).resolve().parents[1]
    / "database/migrations/014_five_table_core.sql"
).read_text()


def test_transaction_wrapped():
    assert SQL.strip().startswith("BEGIN;")
    assert SQL.strip().endswith("COMMIT;")


def test_only_four_new_tables():
    actual = set(re.findall(r"CREATE TABLE\s+(\w+)\s*\(", SQL))
    assert actual == {
        "project_documents", "document_revisions",
        "engineering_values", "document_reviews",
    }


def test_existing_tables_not_recreated():
    for table in ("elevators", "modernisation_cases"):
        assert f"CREATE TABLE {table}" not in SQL
    assert "REFERENCES modernisation_cases(case_id)" in SQL
    assert "JOIN elevators e" in SQL


def test_no_destructive_migration():
    assert not re.search(r"\b(?:DROP|TRUNCATE)\s+TABLE\b", SQL, re.I)
    assert not re.search(r"\bALTER\s+TABLE\b", SQL, re.I)


def test_revision_relationships():
    for relation in (
        "REFERENCES modernisation_cases(case_id)",
        "REFERENCES project_documents(document_id)",
        "REFERENCES document_revisions(revision_id)",
    ):
        assert relation in SQL


def test_supports_mod_iso_and_49_feature_sources():
    for field in (
        "phase TEXT", "value_kind TEXT", "raw_source_value TEXT",
        "numeric_value NUMERIC", "text_value TEXT", "source_page INTEGER",
        "excel_sheet TEXT", "excel_cell TEXT", "excel_formula TEXT",
        "source_note TEXT", "source_verified BOOLEAN",
        "source_classification TEXT",
    ):
        assert field in SQL


def test_history_and_hash_integrity_protected():
    assert "SHA256(file_content)" in SQL
    assert "Original evidence cannot be changed or deleted" in SQL
    assert "Engineering values are immutable" in SQL
    assert "Review events are append-only" in SQL


def test_revision_guard_and_audit_events():
    assert "Revision numbers must be consecutive" in SQL
    assert "Only the newest document revision can be reviewed" in SQL
    assert "INSERT INTO document_reviews" in SQL
    assert "Full approval requires verified source values" in SQL


def test_no_noisy_sql_comments():
    assert not re.search(r"(?m)^\s*--", SQL)


def test_truncation_blocked_for_all_history_tables():
    for table in (
        "project_documents", "document_revisions",
        "engineering_values", "document_reviews",
    ):
        assert f"BEFORE TRUNCATE ON {table}" in SQL


def test_period_requires_both_dates():
    assert "period_start IS NOT NULL AND period_end IS NOT NULL" in SQL
