"""Backfill historical MOD/ISO evidence without changing legacy records.

Dry run by default. Apply is atomic, requires a backup and expected row count.
"""

import argparse
import hashlib
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from database import get_connection
from services.engineering_value_repository import _prepare_value, insert_engineering_values
from services.iso_value_mapper import map_iso_values
from services.mod_value_mapper import COMPARISON_FIELDS, map_mod_values


COMPARISON_NAMES = {
    *(f"{phase}_{field}" for phase in ("existing", "proposed")
      for field in ("elevator_technology", *COMPARISON_FIELDS)),
    "reported_energy_saving_percent", "calculated_energy_saving_percent",
}
ALLOWED_STATUSES = {
    "pending", "approved", "rejected", "changes_requested",
    "further_verification_required",
}


def _rows(cursor):
    names = [column.name for column in cursor.description]
    return [dict(zip(names, values)) for values in cursor.fetchall()]


def _inventory(cursor):
    cursor.execute("""
        SELECT 'mod_pdf' AS document_type, s.document_id AS legacy_id,
               s.case_id, m.case_ref, e.elevator_ref, s.filename,
               s.file_sha256::text AS checksum, s.pdf_bytes AS content,
               s.uploaded_at AS source_date, NULL::text AS lift_id
        FROM source_documents s
        JOIN modernisation_cases m ON m.case_id = s.case_id
        JOIN elevators e ON e.elevator_id = m.elevator_id
        WHERE NOT EXISTS (
            SELECT 1 FROM project_documents p
            WHERE p.case_id = s.case_id AND p.document_type = 'mod_pdf'
              AND p.file_sha256 = s.file_sha256
        )
        UNION ALL
        SELECT 'iso_excel', d.document_id, a.case_id, m.case_ref,
               e.elevator_ref, d.filename, d.file_sha256::text,
               d.content, a.imported_at, a.lift_id
        FROM iso_documents d
        JOIN iso_assessments a ON a.assessment_id = d.assessment_id
        JOIN modernisation_cases m ON m.case_id = a.case_id
        JOIN elevators e ON e.elevator_id = m.elevator_id
        WHERE d.document_type = 'iso_excel'
          AND NOT EXISTS (
            SELECT 1 FROM project_documents p
            WHERE p.case_id = a.case_id AND p.document_type = 'iso_excel'
              AND p.file_sha256 = d.file_sha256
          )
        ORDER BY case_ref, document_type, legacy_id
    """)
    docs = _rows(cursor)
    for doc in docs:
        content = bytes(doc["content"])
        if hashlib.sha256(content).hexdigest() != doc["checksum"].strip():
            raise ValueError(f"Checksum mismatch: {doc['case_ref']} / {doc['legacy_id']}")
        doc["content"] = content
        doc["classification"] = (
            "test_fixture" if doc["elevator_ref"].upper().startswith("TEST-LIFT-")
            else "unknown"
        )
    return docs


def _get_mod_source(cursor, legacy_id):
    cursor.execute("""
        SELECT field_name, field_value, unit, source_page
        FROM enercal_specifications
        WHERE document_id = %s ORDER BY field_name
    """, (legacy_id,))
    specs = list(cursor.fetchall())
    cursor.execute("""
        SELECT revision_id, revision_no, review_status, review_note,
               created_at, reviewed_at
        FROM extraction_revisions WHERE document_id = %s
        ORDER BY revision_no
    """, (legacy_id,))
    revisions = _rows(cursor)
    if not revisions or [r["revision_no"] for r in revisions] != list(range(1, len(revisions) + 1)):
        raise ValueError(f"Invalid legacy MOD revision numbering: {legacy_id}")
    for rev in revisions:
        cursor.execute("""
            SELECT field_name, raw_value, numeric_value, unit
            FROM extraction_candidates
            WHERE extraction_revision_id = %s ORDER BY field_name
        """, (rev["revision_id"],))
        rev["candidates"] = _rows(cursor)
        names = [c["field_name"] for c in rev["candidates"]]
        if len(names) != 16 or set(names) != COMPARISON_NAMES:
            raise ValueError(f"Invalid 16 comparison fields in legacy revision {rev['revision_id']}")
        if rev["review_status"] not in ALLOWED_STATUSES:
            raise ValueError(f"Unsupported legacy MOD status: {rev['review_status']}")
    return specs, revisions


def _build_comparison(candidates, specs):
    index = {c["field_name"]: c for c in candidates}
    cost_unit = str(index["existing_annual_electricity_expenditure"]["unit"] or "")
    if not cost_unit.endswith("/year"):
        raise ValueError("Cannot reconstruct MOD currency from legacy cost unit")
    currency = cost_unit.removesuffix("/year")
    lifetimes = [v for key, v, _, _ in specs if key == "lifetime_years"]
    if len(lifetimes) == 1:
        lifetime = int(Decimal(str(lifetimes[0]).strip()))
    else:
        unit = str(index["existing_operational_co2e"]["unit"] or "")
        match = re.search(r"lifetime\s+(\d+)\s+years", unit)
        if not match:
            raise ValueError("Cannot reconstruct MOD lifetime from legacy evidence")
        lifetime = int(match.group(1))
    rows = {}
    for phase in ("existing", "proposed"):
        rows[phase] = {
            "elevator_technology": index[f"{phase}_elevator_technology"]["raw_value"],
            **{field: index[f"{phase}_{field}"]["numeric_value"]
               for field in COMPARISON_FIELDS},
        }
    return {
        "source_page": 4,
        "currency_symbol": currency,
        "lifetime_years": lifetime,
        "rows": rows,
        "reported_energy_saving_percent": index["reported_energy_saving_percent"]["numeric_value"],
        "calculated_energy_saving_percent": index["calculated_energy_saving_percent"]["numeric_value"],
    }


def _build_mod_values(revision, specs, previous, legacy_id):
    comparison = _build_comparison(revision["candidates"], specs)
    values = map_mod_values(comparison, specs)
    original = {c["field_name"]: c for c in revision["candidates"]}
    old = {c["field_name"]: c for c in previous["candidates"]} if previous else {}
    reason = str(revision.get("review_note") or "").strip()
    for row in values:
        key = row["field_name"]
        if row["phase"] in ("existing", "proposed"):
            label = f"{row['phase']}_{key}"
        else:
            label = key
        if label in original:
            row["raw_source_value"] = original[label]["raw_value"]
            if previous and label in old:
                curr = original[label]
                prior = old[label]
                changed = (
                    curr["numeric_value"] != prior["numeric_value"]
                    if curr["numeric_value"] is not None or prior["numeric_value"] is not None
                    else curr["raw_value"] != prior["raw_value"]
                )
                if changed:
                    row["is_corrected"] = True
                    row["correction_reason"] = reason or (
                        f"Legacy revision {revision['revision_no']} changed {label}; original review events retained"
                    )
        row["source_verified"] = False
        provenance = (
            f"Legacy MOD document {legacy_id}; extraction revision {revision['revision_no']}; "
            f"original status {revision['review_status']}. Original audit events remain in legacy tables."
        )
        row["source_note"] = f"{row['source_note']}; {provenance}" if row.get("source_note") else provenance
    return values


def _get_iso_source(cursor, legacy_id):
    cursor.execute("""
        SELECT field_name, value_text, numeric_value, unit, data_kind,
               pdf_page, excel_sheet, excel_cell, excel_formula,
               source_label, source_note
        FROM iso_extracted_values
        WHERE document_id = %s ORDER BY field_name
    """, (legacy_id,))
    source = _rows(cursor)
    if not source:
        raise ValueError(f"Legacy ISO workbook has no fields: {legacy_id}")
    records = [{
        "field": v["field_name"],
        "value": v["numeric_value"] if v["numeric_value"] is not None else v["value_text"],
        "kind": v["data_kind"], "unit": v["unit"], "page": v["pdf_page"],
        "sheet": v["excel_sheet"], "cell": v["excel_cell"],
        "formula": v["excel_formula"], "label": v["source_label"],
        "note": v["source_note"],
    } for v in source]
    values = map_iso_values(records)
    by_name = {v["field_name"]: v for v in source}
    for row in values:
        source_name = (
            "before_" if row["phase"] == "existing" else
            "after_" if row["phase"] == "proposed" else ""
        ) + row["field_name"]
        legacy = by_name[source_name]
        row["raw_source_value"] = (
            legacy["excel_formula"] if legacy["excel_formula"] is not None
            else legacy["value_text"]
        )
        row["source_verified"] = False
        provenance = f"Legacy ISO document {legacy_id}; original assessment status retained in iso_assessments."
        row["source_note"] = (
            f"{row['source_note']}; {provenance}" if row.get("source_note") else provenance
        )
    return values


def _get_iso_review(cursor, legacy_id):
    cursor.execute("""
        SELECT a.review_status, a.review_note, a.reviewed_at,
               a.imported_at AS created_at
        FROM iso_documents d JOIN iso_assessments a ON a.assessment_id = d.assessment_id
        WHERE d.document_id = %s
    """, (legacy_id,))
    row = cursor.fetchone()
    if row is None or row[0] not in ALLOWED_STATUSES:
        raise ValueError(f"Invalid ISO review status: {legacy_id}")
    return {
        "review_status": row[0], "review_note": row[1],
        "reviewed_at": row[2], "created_at": row[3],
    }


def _insert_document(cursor, doc):
    cursor.execute("""
        INSERT INTO project_documents
            (case_id, document_type, source_classification,
             reported_elevator_ref, filename, file_sha256, file_content, uploaded_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (case_id, document_type, file_sha256) DO NOTHING
        RETURNING document_id
    """, (
        doc["case_id"], doc["document_type"], doc["classification"],
        doc["lift_id"], doc["filename"], doc["checksum"], doc["content"],
        doc["source_date"],
    ))
    saved = cursor.fetchone()
    if saved is None:
        raise RuntimeError("Document already appeared during migration; aborting")
    return saved[0]


def _new_revision(cursor, document_id, number, created_at):
    cursor.execute("""
        INSERT INTO document_revisions
            (document_id, revision_no, review_scope, created_at)
        VALUES (%s, %s, 'comparison_only', %s)
        RETURNING revision_id
    """, (document_id, number, created_at))
    return cursor.fetchone()[0]


def _review(cursor, revision_id, status, note, reviewed_at=None):
    if status == "pending":
        return
    note = str(note or "").strip()
    if not note:
        note = f"Historical {status} decision imported from legacy evidence; not full-value verification."
    cursor.execute("""
        UPDATE document_revisions
        SET review_status = %s, review_note = %s, reviewed_at = COALESCE(%s, NOW())
        WHERE revision_id = %s AND review_status = 'pending'
    """, (status, note, reviewed_at, revision_id))
    if cursor.rowcount != 1:
        raise RuntimeError("Could not apply historical comparison review")


def _backfill_mod(cursor, doc, document_id):
    specs, revisions = _get_mod_source(cursor, doc["legacy_id"])
    previous = None
    for index, old in enumerate(revisions):
        new_id = _new_revision(cursor, document_id, old["revision_no"], old["created_at"])
        values = _build_mod_values(old, specs, previous, doc["legacy_id"])
        insert_engineering_values(cursor, new_id, values)
        if old["review_status"] == "pending" and index + 1 < len(revisions):
            _review(cursor, new_id, "further_verification_required", (
                f"Migration compatibility transition only: legacy revision {old['revision_no']} "
                f"remains pending in extraction_revisions. Its successor exists in the legacy "
                "history. This does not imply a historical engineer decision."
            ))
        else:
            _review(cursor, new_id, old["review_status"], (
                f"Imported historical comparison decision. Original status: {old['review_status']}. "
                f"Original note: {old['review_note'] or '(none)'}"
            ), old["reviewed_at"])
        previous = old
    return len(revisions)


def _backfill_iso(cursor, doc, document_id):
    review = _get_iso_review(cursor, doc["legacy_id"])
    new_id = _new_revision(cursor, document_id, 1, review["created_at"])
    values = _get_iso_source(cursor, doc["legacy_id"])
    insert_engineering_values(cursor, new_id, values)
    _review(cursor, new_id, review["review_status"], (
        f"Imported legacy ISO comparison decision: {review['review_status']}. "
        f"Original note: {review['review_note'] or '(none)'}"
    ), review["reviewed_at"])
    return 1


def _check_no_same_type_document(cursor, docs):
    for doc in docs:
        cursor.execute("""
            SELECT document_id FROM project_documents
            WHERE case_id = %s AND document_type = %s LIMIT 1
        """, (doc["case_id"], doc["document_type"]))
        if cursor.fetchone() is not None:
            raise ValueError(
                f"Case {doc['case_ref']} already contains unified {doc['document_type']} evidence; "
                "historical backfill needs explicit ordering review"
            )
    groups = defaultdict(list)
    for doc in docs:
        groups[(doc["case_id"], doc["document_type"])].append(doc)
    if any(len(v) > 1 for v in groups.values()):
        raise ValueError("Multiple unmigrated documents of one type within a case need an ordering plan")


def run(*, apply=False, backup=None, expected_missing=None):
    if apply:
        if expected_missing is None or expected_missing < 0:
            raise ValueError("--apply requires --expected-missing N")
        if not backup or not Path(backup).is_file() or Path(backup).stat().st_size < 1024:
            raise ValueError("--apply requires an existing nonempty --backup file")

    with get_connection() as connection, connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        if not apply:
            cursor.execute("SET TRANSACTION READ ONLY")
        docs = _inventory(cursor)
        print(f"Missing historical documents: {len(docs)}")
        for doc in docs:
            print(f"  {doc['case_ref']:18} {doc['document_type']:10} "
                  f"legacy={doc['legacy_id']:>3} {doc['classification']}")
        if apply and len(docs) != expected_missing:
            raise ValueError(
                f"Expected {expected_missing} missing documents; found {len(docs)}. "
                "No changes committed."
            )
        _check_no_same_type_document(cursor, docs)
        planned_revisions = 0
        planned_values = 0
        for doc in docs:
            if doc["document_type"] == "mod_pdf":
                specs, historical = _get_mod_source(cursor, doc["legacy_id"])
                previous = None
                for old in historical:
                    records = _build_mod_values(old, specs, previous, doc["legacy_id"])
                    [_prepare_value(record) for record in records]
                    planned_values += len(records)
                    planned_revisions += 1
                    previous = old
            else:
                _get_iso_review(cursor, doc["legacy_id"])
                records = _get_iso_source(cursor, doc["legacy_id"])
                [_prepare_value(record) for record in records]
                planned_values += len(records)
                planned_revisions += 1
        print(f"Validated {planned_revisions} historical revisions and {planned_values} engineering values.")
        if not apply:
            connection.rollback()
            print("DRY RUN ONLY: no records were changed.")
            return len(docs)
        revisions = 0
        for doc in docs:
            document_id = _insert_document(cursor, doc)
            revisions += (
                _backfill_mod(cursor, doc, document_id)
                if doc["document_type"] == "mod_pdf"
                else _backfill_iso(cursor, doc, document_id)
            )
        print(f"Backfilled {len(docs)} documents and {revisions} revisions atomically.")
        print("Legacy records untouched; engineering source_verified remains FALSE.")
    return len(docs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write backfill in one transaction")
    parser.add_argument("--backup", type=Path, help="Existing pg_dump archive required for --apply")
    parser.add_argument("--expected-missing", type=int, help="Exact expected number of missing documents")
    args = parser.parse_args()
    run(apply=args.apply, backup=args.backup, expected_missing=args.expected_missing)


if __name__ == "__main__":
    main()
