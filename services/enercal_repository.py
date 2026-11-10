
"""Save extracted data from the 12 page EnerCal MOD report"""

import hashlib

from database import get_connection
from services.enercal_extractor import COMPARISON_FIELDS, extract_mod_document
from services.enercal_specs import extract_specs


def _comparison_candidates(comparison):
    """Build the 16 fields used by engineering review"""
    currency = comparison["currency_symbol"]
    years = comparison["lifetime_years"]
    units = {
        "annual_energy_consumption": "kWh/year",
        "annual_regenerative_energy": "kWh/year",
        "annual_electricity_expenditure": f"{currency}/year",
        "lifetime_electricity_expenditure": f"{currency}/lifetime",
        "operational_co2e": f"kgCO2e/lifetime {years} years",
        "embodied_co2e": "kgCO2e",
    }

    candidates = []
    for role in ("existing", "proposed"):
        row = comparison["rows"][role]
        candidates.append(
            (f"{role}_elevator_technology", row["elevator_technology"], None, None)
        )
        for field in COMPARISON_FIELDS:
            value = row[field]
            candidates.append((
                f"{role}_{field}",
                "N/A" if value is None else str(value),
                value,
                units[field],
            ))

    for field in ("reported_energy_saving_percent", "calculated_energy_saving_percent"):
        value = comparison[field]
        candidates.append((field, str(value), value, "%"))

    if len(candidates) != 16 or len({item[0] for item in candidates}) != 16:
        raise ValueError("The comparison must contain 16 unique fields")
    return candidates


def save_complete_enercal_report(case_ref, filename, pdf_bytes, extracted=None):
    """Save the original PDF and one pending extraction revision"""
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("Choose a valid PDF")

    # Reuse the preview results when the upload page supplies them
    data = extracted if extracted is not None else extract_mod_document(pdf_bytes)
    comparison = data["comparison"]
    pages = data["pages"]
    specifications = data.get("specifications")
    if specifications is None:
        specifications = extract_specs(pdf_bytes)

    if [page["page_number"] for page in pages] != list(range(1, 13)):
        raise ValueError("Expected a MOD report containing 12 pages")
    if comparison["source_page"] != 4:
        raise ValueError("The energy comparison must come from page 4")

    candidates = _comparison_candidates(comparison)
    names = [field for field, *_ in specifications]
    if len(names) != len(set(names)):
        raise ValueError("Repeated specification fields were found")

    checksum = hashlib.sha256(pdf_bytes).hexdigest()

    # Save every record in one transaction to prevent partial uploads
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT case_id FROM modernisation_cases WHERE case_ref = %s",
            (case_ref,),
        )
        case = cur.fetchone()
        if case is None:
            raise ValueError("The selected modernisation case was not found")

        cur.execute(
            """
            INSERT INTO source_documents
                (case_id, filename, file_sha256, file_size_bytes, pdf_bytes)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (case_id, file_sha256) DO NOTHING
            RETURNING document_id
            """,
            (case[0], filename, checksum, len(pdf_bytes), pdf_bytes),
        )
        saved = cur.fetchone()
        if saved is None:
            raise ValueError("This PDF is already saved for the selected case")
        document_id = saved[0]

        cur.execute(
            """
            INSERT INTO extraction_revisions (document_id, revision_no, review_status)
            VALUES (%s, 1, 'pending')
            RETURNING revision_id
            """,
            (document_id,),
        )
        revision_id = cur.fetchone()[0]

        page_ids = {}
        for page in pages:
            cur.execute(
                """
                INSERT INTO document_pages
                    (document_id, page_number, extracted_text, extraction_method)
                VALUES (%s, %s, %s, 'pymupdf')
                RETURNING page_id
                """,
                (document_id, page["page_number"], page["extracted_text"]),
            )
            page_ids[page["page_number"]] = cur.fetchone()[0]

        if specifications:
            cur.executemany(
                """
                INSERT INTO enercal_specifications
                    (document_id, field_name, field_value, unit, source_page)
                VALUES (%s, %s, %s, %s, %s)
                """,
                [(document_id, *field) for field in specifications],
            )

        comparison_page_id = page_ids[comparison["source_page"]]
        cur.executemany(
            """
            INSERT INTO extraction_candidates
                (page_id, field_name, raw_value, numeric_value, unit,
                 revision_no, extraction_revision_id, review_status)
            VALUES (%s, %s, %s, %s, %s, 1, %s, 'pending')
            """,
            [
                (comparison_page_id, field, raw, numeric, unit, revision_id)
                for field, raw, numeric, unit in candidates
            ],
        )

        # Record one event for the first extraction revision
        cur.execute(
            """
            INSERT INTO enercal_review_events
                (revision_id, event_type, previous_status, new_status, reason)
            VALUES (%s, 'initial_extraction', NULL, 'pending', %s)
            """,
            (revision_id, "MOD report uploaded with 16 comparison fields"),
        )

    return {
        "document_id": document_id,
        "pages_saved": len(pages),
        "fields_saved": len(candidates),
        "additional_fields_saved": len(specifications),
    }
