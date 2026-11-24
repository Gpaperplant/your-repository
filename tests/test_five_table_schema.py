import hashlib
import os
from pathlib import Path
from uuid import uuid4

import pytest

psycopg = pytest.importorskip("psycopg")

DATABASE_URL = os.getenv("TEST_DATABASE_URL")
if not DATABASE_URL:
    pytest.skip("Set TEST_DATABASE_URL for PostgreSQL integration tests", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[1]
REGISTRATION_SQL = ROOT / "database" / "migrations" / "001_elevator_cases.sql"
CORE_SQL = ROOT / "database" / "migrations" / "014_five_table_core.sql"


@pytest.fixture
def db():
    schema = "fyp_test_" + uuid4().hex
    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        try:
            conn.execute(f"SET search_path TO {schema}, public")
            conn.execute(REGISTRATION_SQL.read_text())
            conn.execute(CORE_SQL.read_text())
            yield conn
        finally:
            conn.execute("SET search_path TO public")
            conn.execute(f"DROP SCHEMA {schema} CASCADE")


def register_case(db, elevator="OP13 (L)"):
    elevator_id = db.execute(
        "INSERT INTO elevators (elevator_ref) VALUES (%s) RETURNING elevator_id",
        (elevator,),
    ).fetchone()[0]
    return db.execute(
        "INSERT INTO modernisation_cases (case_ref, elevator_id) "
        "VALUES (%s, %s) RETURNING case_id",
        (str(uuid4()), elevator_id),
    ).fetchone()[0]


def upload(db, case_id, doc_type="mod_pdf", body=b"sample", elevator=None):
    return db.execute(
        """
        INSERT INTO project_documents (
            case_id, document_type, filename, file_sha256,
            file_content, reported_elevator_ref
        ) VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING document_id
        """,
        (
            case_id, doc_type, "evidence.bin", hashlib.sha256(body).hexdigest(),
            body, elevator,
        ),
    ).fetchone()[0]


def revision(db, document_id, number=1):
    return db.execute(
        "INSERT INTO document_revisions (document_id, revision_no) "
        "VALUES (%s, %s) RETURNING revision_id",
        (document_id, number),
    ).fetchone()[0]


def value(db, revision_id, verified=True, amount=100, **extra):
    values = {
        "revision_id": revision_id,
        "field_name": "annual_energy_kwh",
        "phase": "existing",
        "value_kind": "calculated_estimate",
        "numeric_value": amount,
        "raw_source_value": "100",
        "source_verified": verified,
        **extra,
    }
    columns = ", ".join(values)
    params = ", ".join(["%s"] * len(values))
    db.execute(
        f"INSERT INTO engineering_values ({columns}) VALUES ({params})",
        tuple(values.values()),
    )


def review(db, revision_id, decision="approved", reason=None):
    return db.execute(
        "SELECT review_document_revision(%s, %s, %s, %s, %s)",
        (revision_id, decision, reason, "engineer", False),
    ).fetchone()[0]


def assert_rejected(db, statement, args=()):
    with pytest.raises(psycopg.Error):
        with db.transaction():
            db.execute(statement, args)


def test_existing_tables_and_four_new_tables(db):
    rows = db.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    ).fetchall()
    assert {row[0] for row in rows} == {
        "elevators", "modernisation_cases", "project_documents",
        "document_revisions", "engineering_values", "document_reviews",
    }


def test_valid_mod_and_iso_documents(db):
    case_id = register_case(db)
    assert upload(db, case_id) > 0
    assert upload(db, case_id, "iso_excel", b"workbook", "OP13(L)") > 0


def test_rejects_wrong_elevator(db):
    case_id = register_case(db)
    assert_rejected(
        db,
        """
        INSERT INTO project_documents
        (case_id, document_type, filename, file_sha256,
         file_content, reported_elevator_ref)
        VALUES (%s, 'iso_excel', 'x.xlsx', %s, %s, 'OTHER')
        """,
        (case_id, hashlib.sha256(b"x").hexdigest(), b"x"),
    )


def test_rejects_missing_iso_lift(db):
    case_id = register_case(db)
    with pytest.raises(psycopg.Error):
        with db.transaction():
            upload(db, case_id, "iso_excel", b"x")


def test_rejects_duplicate_and_bad_checksum(db):
    case_id = register_case(db)
    upload(db, case_id)
    with pytest.raises(psycopg.Error):
        with db.transaction():
            upload(db, case_id)
    assert_rejected(
        db,
        """
        INSERT INTO project_documents
        (case_id, document_type, filename, file_sha256, file_content)
        VALUES (%s, 'mod_pdf', 'x.pdf', %s, %s)
        """,
        (case_id, "a" * 64, b"wrong"),
    )


def test_original_file_immutable(db):
    case_id = register_case(db)
    doc = upload(db, case_id)
    assert_rejected(
        db, "UPDATE project_documents SET filename = 'changed' WHERE document_id = %s",
        (doc,),
    )
    assert_rejected(db, "DELETE FROM project_documents WHERE document_id = %s", (doc,))


def test_revisions_are_consecutive_and_not_parallel(db):
    case_id = register_case(db)
    doc = upload(db, case_id)
    revision(db, doc)
    with pytest.raises(psycopg.Error):
        with db.transaction():
            revision(db, doc, 3)
    with pytest.raises(psycopg.Error):
        with db.transaction():
            revision(db, doc, 2)


def test_new_revision_preserves_prior_evidence(db):
    case_id = register_case(db)
    doc = upload(db, case_id)
    first = revision(db, doc)
    value(db, first)
    review(db, first, "changes_requested", "Correct energy")
    second = revision(db, doc, 2)
    value(
        db, second, amount=120, is_corrected=True,
        correction_reason="Verified against source", source_page=4,
    )
    rows = db.execute(
        "SELECT revision_id, numeric_value FROM engineering_values ORDER BY value_id"
    ).fetchall()
    assert rows == [(first, 100), (second, 120)]
    assert_rejected(
        db, "UPDATE engineering_values SET numeric_value = 121 WHERE revision_id = %s",
        (second,),
    )
    assert_rejected(db, "DELETE FROM engineering_values WHERE revision_id = %s", (first,))


def test_cannot_approve_empty_revision(db):
    case_id = register_case(db)
    rev = revision(db, upload(db, case_id))
    with pytest.raises(psycopg.Error):
        with db.transaction():
            review(db, rev)


def test_cannot_approve_unverified_values(db):
    case_id = register_case(db)
    rev = revision(db, upload(db, case_id))
    value(db, rev, verified=False)
    with pytest.raises(psycopg.Error):
        with db.transaction():
            review(db, rev)


def test_approved_revision_is_logged_and_final(db):
    case_id = register_case(db)
    rev = revision(db, upload(db, case_id))
    value(db, rev)
    event_id = review(db, rev)
    events = db.execute(
        "SELECT action, new_status FROM document_reviews WHERE revision_id = %s "
        "ORDER BY review_id",
        (rev,),
    ).fetchall()
    assert events == [("revision_submitted", "pending"), ("approved", "approved")]
    assert event_id is not None
    assert_rejected(
        db, "UPDATE document_reviews SET reason = 'fake' WHERE revision_id = %s",
        (rev,),
    )
    assert_rejected(db, "DELETE FROM document_reviews WHERE revision_id = %s", (rev,))
    with pytest.raises(psycopg.Error):
        with db.transaction():
            review(db, rev, "rejected", "Changed mind")


def test_latest_document_only_can_be_reviewed(db):
    case_id = register_case(db)
    old = revision(db, upload(db, case_id, body=b"old"))
    value(db, old)
    upload(db, case_id, body=b"new")
    with pytest.raises(psycopg.Error):
        with db.transaction():
            review(db, old)


def test_meter_value_requires_period(db):
    case_id = register_case(db)
    rev = revision(db, upload(db, case_id, "meter_csv"))
    with pytest.raises(psycopg.Error):
        with db.transaction():
            value(db, rev, value_kind="meter_measurement")
    value(
        db, rev, value_kind="meter_measurement", unit="kWh",
        period_start="2026-01-01", period_end="2026-01-31",
    )


def test_phase_and_correction_constraints(db):
    case_id = register_case(db)
    rev = revision(db, upload(db, case_id))
    with pytest.raises(psycopg.Error):
        with db.transaction():
            value(db, rev, phase="before")
    with pytest.raises(psycopg.Error):
        with db.transaction():
            value(db, rev, is_corrected=True)
    value(db, rev)
    with pytest.raises(psycopg.Error):
        with db.transaction():
            value(db, rev)


def test_no_value_edits_after_approval(db):
    case_id = register_case(db)
    rev = revision(db, upload(db, case_id))
    value(db, rev)
    review(db, rev)
    with pytest.raises(psycopg.Error):
        with db.transaction():
            value(db, rev, phase="proposed")


@pytest.mark.parametrize(
    "table", [
        "project_documents", "document_revisions",
        "engineering_values", "document_reviews",
    ],
)
def test_history_cannot_be_truncated(db, table):
    assert_rejected(db, f"TRUNCATE TABLE {table} CASCADE")
