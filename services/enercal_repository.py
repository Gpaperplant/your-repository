import hashlib
from decimal import Decimal

from database import get_connection

from services.enercal_extractor import (
    extract_pdf_text,
    extract_energy_comparison,
    extract_full_energy_comparison,
)




def save_enercal_report(case_ref, filename, pdf_bytes):
    """Save an EnerCal PDF and extracted values."""

    if not pdf_bytes:
        raise ValueError("PDF file is empty.")

    # 1. Extract PDF data
    pages = extract_pdf_text(pdf_bytes)
    energy = extract_energy_comparison(pages)

    # 2. Generate the PDF fingerprint
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()

    # 3. Connect to PostgreSQL
    with get_connection() as conn:
        with conn.cursor() as cur:

            # Find the modernisation case
            cur.execute(
                """
                SELECT case_id
                FROM modernisation_cases
                WHERE case_ref = %s
                """,
                (case_ref,),
            )

            case_row = cur.fetchone()

            if case_row is None:
                raise ValueError(
                    f"Case not found: {case_ref}"
                )

            case_id = case_row[0]

            # 4. Save the original PDF
            cur.execute(
                """
                INSERT INTO source_documents (
                    case_id,
                    filename,
                    file_sha256,
                    file_size_bytes,
                    pdf_bytes
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (case_id, file_sha256)
                DO NOTHING
                RETURNING document_id
                """,
                (
                    case_id,
                    filename,
                    file_hash,
                    len(pdf_bytes),
                    pdf_bytes,
                ),
            )

            saved_document = cur.fetchone()

            if saved_document is None:
                raise ValueError(
                    "This PDF is already saved for this case."
                )

            document_id = saved_document[0]

            # 5. Save text extracted from each page
            page_ids = {}

            for page in pages:
                cur.execute(
                    """
                    INSERT INTO document_pages (
                        document_id,
                        page_number,
                        extracted_text,
                        extraction_method
                    )
                    VALUES (%s, %s, %s, %s)
                    RETURNING page_id
                    """,
                    (
                        document_id,
                        page["page_number"],
                        page["extracted_text"],
                        "pymupdf",
                    ),
                )

                page_ids[page["page_number"]] = (
                    cur.fetchone()[0]
                )

            # 6. Link values to the source PDF page
            source_page_id = page_ids[
                energy["source_page"]
            ]

            candidates = [
                (
                    "baseline_annual_energy",
                    energy["baseline_annual_energy_kwh"],
                    "kWh/year",
                ),
                (
                    "proposed_annual_energy",
                    energy["proposed_annual_energy_kwh"],
                    "kWh/year",
                ),
                (
                    "calculated_saving_percent",
                    energy["calculated_saving_percent"],
                    "%",
                ),
            ]

            # 7. Save extracted values as pending
            for field_name, value, unit in candidates:
                cur.execute(
                    """
                    INSERT INTO extraction_candidates (
                        page_id,
                        field_name,
                        raw_value,
                        numeric_value,
                        unit,
                        review_status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        source_page_id,
                        field_name,
                        str(value),
                        Decimal(str(value)),
                        unit,
                        "pending",
                    ),
                )

    return {
        "document_id": document_id,
        "case_ref": case_ref,
        "pages_saved": len(pages),
        "energy": energy,
    }


def save_full_comparison(document_id):
    """Store the complete EnerCal comparison in existing tables."""

    # Field names and their corresponding units
    fields = {
        "annual_energy_consumption": "kWh/year",
        "annual_regenerative_energy": "kWh/year",
        "annual_electricity_expenditure": "BHD/year",
        "lifetime_electricity_expenditure": "BHD/lifetime",
        "operational_co2e": "kgCO2e/lifetime 20 years",
        "embodied_co2e": "kgCO2e",
    }

    with get_connection() as conn:
        with conn.cursor() as cur:

            # 1. Retrieve the original PDF
            cur.execute(
                """
                SELECT pdf_bytes
                FROM source_documents
                WHERE document_id = %s
                """,
                (document_id,)
            )

            document = cur.fetchone()

            if document is None:
                raise ValueError("Document not found.")

            pdf_bytes = bytes(document[0])

            # 2. Extract the complete comparison
            data = extract_full_energy_comparison(pdf_bytes)

            # 3. Find the source page record
            cur.execute(
                """
                SELECT page_id
                FROM document_pages
                WHERE document_id = %s
                  AND page_number = %s
                """,
                (document_id, data["source_page"])
            )

            page_record = cur.fetchone()

            if page_record is None:
                raise ValueError("Source page not found.")

            page_id = page_record[0]

            # 4. Prepare all 16 candidate fields
            candidates = {}

            for role, row in data["rows"].items():

                candidates[f"{role}_elevator_technology"] = (
                    row["elevator_technology"],
                    None,
                    None
                )

                for field_name, unit in fields.items():

                    value = row[field_name]

                    candidates[f"{role}_{field_name}"] = (
                        "N/A" if value is None else str(value),
                        value,
                        unit
                    )

            candidates["reported_energy_saving_percent"] = (
                str(data["reported_energy_saving_percent"]) + "%",
                data["reported_energy_saving_percent"],
                "%"
            )

            candidates["calculated_energy_saving_percent"] = (
                str(data["calculated_energy_saving_percent"]),
                data["calculated_energy_saving_percent"],
                "%"
            )

            # 5. Reuse our three original pending records
            legacy_names = {
                "baseline_annual_energy":
                    "existing_annual_energy_consumption",

                "proposed_annual_energy":
                    "proposed_annual_energy_consumption",

                "calculated_saving_percent":
                    "calculated_energy_saving_percent",
            }

            for old_name, new_name in legacy_names.items():

                cur.execute(
                    """
                    SELECT candidate_id, numeric_value
                    FROM extraction_candidates
                    WHERE page_id = %s
                      AND field_name = %s
                      AND review_status = 'pending'
                    """,
                    (page_id, old_name)
                )

                old_records = cur.fetchall()

                cur.execute(
                    """
                    SELECT candidate_id
                    FROM extraction_candidates
                    WHERE page_id = %s
                      AND field_name = %s
                    """,
                    (page_id, new_name)
                )

                new_record = cur.fetchone()

                expected_value = candidates[new_name][1]

                if (
                    len(old_records) == 1
                    and new_record is None
                    and old_records[0][1] == expected_value
                ):
                    cur.execute(
                        """
                        UPDATE extraction_candidates
                        SET field_name = %s
                        WHERE candidate_id = %s
                        """,
                        (new_name, old_records[0][0])
                    )

            # 6. Insert fields that do not already exist
            inserted = 0

            for field_name, values in candidates.items():

                raw_value, numeric_value, unit = values

                cur.execute(
                    """
                    SELECT candidate_id
                    FROM extraction_candidates
                    WHERE page_id = %s
                      AND field_name = %s
                    """,
                    (page_id, field_name)
                )

                if cur.fetchone() is not None:
                    continue

                cur.execute(
                    """
                    INSERT INTO extraction_candidates (
                        page_id,
                        field_name,
                        raw_value,
                        numeric_value,
                        unit,
                        review_status
                    )
                    VALUES (%s, %s, %s, %s, %s, 'pending')
                    """,
                    (
                        page_id,
                        field_name,
                        raw_value,
                        numeric_value,
                        unit
                    )
                )

                inserted += 1

    return {
        "document_id": document_id,
        "source_page": data["source_page"],
        "fields_prepared": len(candidates),
        "new_records_inserted": inserted
    }




def save_complete_enercal_report(case_ref, filename, pdf_bytes):
    """
    Save a new EnerCal PDF, all its pages,
    and the complete 16-field comparison.
    """

    if not pdf_bytes:
        raise ValueError("The PDF file is empty.")

    # Extract everything before opening a database transaction
    pages = extract_pdf_text(pdf_bytes)
    comparison = extract_full_energy_comparison(pdf_bytes)

    # Identify identical files by their SHA-256 fingerprint
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()

    units = {
        "annual_energy_consumption": "kWh/year",
        "annual_regenerative_energy": "kWh/year",
        "annual_electricity_expenditure": "BHD/year",
        "lifetime_electricity_expenditure": "BHD/lifetime",
        "operational_co2e": "kgCO2e/lifetime 20 years",
        "embodied_co2e": "kgCO2e",
    }

    with get_connection() as conn:
        with conn.cursor() as cur:

            # Find the registered modernisation case
            cur.execute(
                """
                SELECT case_id
                FROM modernisation_cases
                WHERE case_ref = %s
                """,
                (case_ref,)
            )

            case = cur.fetchone()

            if case is None:
                raise ValueError("Modernisation case not found.")

            # Save the original PDF
            cur.execute(
                """
                INSERT INTO source_documents (
                    case_id,
                    filename,
                    file_sha256,
                    file_size_bytes,
                    pdf_bytes
                )
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (case_id, file_sha256)
                DO NOTHING
                RETURNING document_id
                """,
                (
                    case[0],
                    filename,
                    file_hash,
                    len(pdf_bytes),
                    pdf_bytes
                )
            )

            document = cur.fetchone()

            if document is None:
                raise ValueError(
                    "This PDF already exists for this case."
                )

                        
            document_id = document[0]

            # Create Revision 1 for the new EnerCal document
            cur.execute(
                """
                INSERT INTO extraction_revisions (
                    document_id,
                    revision_no,
                    review_status
                )
                VALUES (%s, 1, 'pending')
                RETURNING revision_id
                """,
                (document_id,)
            )

            revision_id = cur.fetchone()[0]

            # Store all pages and remember their database IDs
            page_ids = {}

            for page in pages:
                cur.execute(
                    """
                    INSERT INTO document_pages (
                        document_id,
                        page_number,
                        extracted_text,
                        extraction_method
                    )
                    VALUES (%s, %s, %s, %s)
                    RETURNING page_id
                    """,
                    (
                        document_id,
                        page["page_number"],
                        page["extracted_text"],
                        "pymupdf"
                    )
                )

                page_ids[page["page_number"]] = cur.fetchone()[0]

            # Link comparison fields to their source page
            page_id = page_ids[comparison["source_page"]]

            candidates = []

            for role in ("existing", "proposed"):
                elevator = comparison["rows"][role]

                # Save elevator technology name
                candidates.append((
                    f"{role}_elevator_technology",
                    elevator["elevator_technology"],
                    None,
                    None
                ))

                # Save all six numeric fields
                for field_name, unit in units.items():
                    value = elevator[field_name]

                    candidates.append((
                        f"{role}_{field_name}",
                        "N/A" if value is None else str(value),
                        value,
                        unit
                    ))

            # Save the two percentage fields
            for field_name in (
                "reported_energy_saving_percent",
                "calculated_energy_saving_percent"
            ):
                value = comparison[field_name]

                candidates.append((
                    field_name,
                    str(value),
                    value,
                    "%"
                ))
                
            # Ensure that the complete comparison was extracted
            if len(candidates) != 16:
                raise ValueError(
                    "Expected exactly 16 EnerCal comparison fields."
                )

            # Save all 16 values under Revision 1
            for field_name, raw_value, number, unit in candidates:
                cur.execute(
                    """
                    INSERT INTO extraction_candidates (
                        page_id,
                        field_name,
                        raw_value,
                        numeric_value,
                        unit,
                        revision_no,
                        extraction_revision_id,
                        review_status
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        %s, 1, %s, 'pending'
                    )
                    """,
                    (
                        page_id,
                        field_name,
                        raw_value,
                        number,
                        unit,
                        revision_id
                    )
                )

            # Record the original upload as a review-history event.
            # It belongs to Revision 1, not to 16 separate candidates.
            cur.execute(
                """
                INSERT INTO enercal_review_events (
                    revision_id,
                    event_type,
                    previous_status,
                    new_status,
                    reason
                )
                VALUES (
                    %s,
                    'initial_extraction',
                    NULL,
                    'pending',
                    'EnerCal PDF uploaded and 16 fields extracted.'
                )
                """,
                (revision_id,)
            )

    return {
        "document_id": document_id,
        "pages_saved": len(pages),
        "fields_saved": len(candidates)
    }






# The 16 fields expected from a complete EnerCal comparison
ENERGY_FIELDS = (
    "annual_energy_consumption",
    "annual_regenerative_energy",
    "annual_electricity_expenditure",
    "lifetime_electricity_expenditure",
    "operational_co2e",
    "embodied_co2e",
)

EXPECTED_REVIEW_FIELDS = {
    "reported_energy_saving_percent",
    "calculated_energy_saving_percent",
}

for role in ("existing", "proposed"):
    EXPECTED_REVIEW_FIELDS.add(
        f"{role}_elevator_technology"
    )

    for field in ENERGY_FIELDS:
        EXPECTED_REVIEW_FIELDS.add(f"{role}_{field}")


def get_review_documents():
    """List PDFs with an extracted EnerCal comparison."""

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT
                    d.document_id,
                    m.case_ref,
                    d.filename
                FROM source_documents d
                JOIN modernisation_cases m
                    ON d.case_id = m.case_id
                JOIN enercal_comparison_view v
                    ON d.document_id = v.document_id
                ORDER BY d.document_id DESC
                """
            )

            return cur.fetchall()


def get_review_details(document_id):
    """Retrieve PDF, comparison rows and review statuses."""

    with get_connection() as conn:
        with conn.cursor() as cur:

            # Retrieve the original PDF
            cur.execute(
                """
                SELECT filename, pdf_bytes
                FROM source_documents
                WHERE document_id = %s
                """,
                (document_id,)
            )

            document = cur.fetchone()

            if document is None:
                raise ValueError("Document not found.")

            filename, pdf_bytes = document

            # Retrieve the existing and proposed elevator rows
            cur.execute(
                """
                SELECT *
                FROM enercal_comparison_view
                WHERE document_id = %s
                ORDER BY
                    CASE technology_role
                        WHEN 'existing' THEN 1
                        ELSE 2
                    END
                """,
                (document_id,)
            )

            comparison = cur.fetchall()
            column_names = [
                desc.name for desc in cur.description
            ]

            comparison_rows = [
                dict(zip(column_names, row))
                for row in comparison
            ]

            # Retrieve the current review statuses
            cur.execute(
                """
                SELECT
                    c.field_name,
                    c.review_status
                FROM extraction_candidates c
                JOIN document_pages p
                    ON c.page_id = p.page_id
                WHERE p.document_id = %s
                ORDER BY c.field_name
                """,
                (document_id,)
            )

            statuses = cur.fetchall()

    return {
        "filename": filename,
        "pdf_bytes": bytes(pdf_bytes),
        "comparison": comparison_rows,
        "statuses": statuses,
    }


def update_enercal_review(document_id, decision):
    """Approve or reject all 16 fields in one transaction."""

    if decision not in ("approved", "rejected"):
        raise ValueError("Invalid review decision.")

    with get_connection() as conn:
        with conn.cursor() as cur:

            # Lock candidate records during the decision
            cur.execute(
                """
                SELECT
                    c.candidate_id,
                    c.field_name,
                    c.review_status
                FROM extraction_candidates c
                JOIN document_pages p
                    ON c.page_id = p.page_id
                WHERE p.document_id = %s
                FOR UPDATE OF c
                """,
                (document_id,)
            )

            candidates = cur.fetchall()

            # Check that the 16 required fields exist exactly once
            names = [row[1] for row in candidates]

            if (
                len(names) != len(EXPECTED_REVIEW_FIELDS)
                or set(names) != EXPECTED_REVIEW_FIELDS
            ):
                raise ValueError(
                    "The document does not contain exactly "
                    "16 expected fields. Review cannot proceed."
                )

            # Don't overwrite an earlier review decision
            if any(row[2] != "pending" for row in candidates):
                raise ValueError(
                    "This document has already been reviewed "
                    "or contains mixed review statuses."
                )

            # Change all 16 records together
            candidate_ids = [row[0] for row in candidates]

            cur.execute(
                """
                UPDATE extraction_candidates
                SET review_status = %s
                WHERE candidate_id = ANY(%s)
                """,
                (decision, candidate_ids)
            )

            if cur.rowcount != len(candidate_ids):
                raise RuntimeError(
                    "Not all candidate records were updated."
                )

    return len(candidate_ids)
