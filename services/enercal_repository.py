"""Save a 12-page MOD report using the five-table evidence architecture only.

One transaction stores the original PDF, its first pending revision, and the
engineering-value snapshot. No legacy table is queried or written.
"""

from database import get_connection
from services.document_revision_repository import insert_revision
from services.enercal_extractor import extract_mod_document
from services.enercal_specs import extract_specs
from services.engineering_value_repository import insert_engineering_values
from services.mod_value_mapper import map_mod_values
from services.project_document_repository import insert_project_document


MAX_PDF_BYTES = 15 * 1024 * 1024


def save_complete_enercal_report(case_ref, filename, pdf_bytes, extracted=None):
    """Store a validated MOD report and return unified document/revision IDs.

    `extracted` is the preview output, if supplied; the PDF is not extracted a
    second time. All writes use one database transaction, with rollback on error.
    """
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("Choose a valid MOD PDF.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError("MOD PDF must be no larger than 15 MB.")

    data = extracted if extracted is not None else extract_mod_document(pdf_bytes)
    comparison = data["comparison"]
    pages = data["pages"]
    specifications = data.get("specifications")
    if specifications is None:
        specifications = extract_specs(pdf_bytes)

    if [page["page_number"] for page in pages] != list(range(1, 13)):
        raise ValueError("Expected a MOD report containing 12 pages.")
    if comparison.get("source_page") != 4:
        raise ValueError("The MOD energy comparison must come from page 4.")

    # Preserve original extractor duplicate-field protection before mapping.
    names = [item[0] for item in specifications]
    if len(names) != len(set(names)):
        raise ValueError("Repeated MOD specification fields were found.")

    values = map_mod_values(comparison, specifications)
    comparison_fields = sum(
        value["source_page"] == 4
        and value["value_kind"] in {"calculated_estimate", "reported_estimate", "engineering_specification"}
        for value in values
    )
    if comparison_fields != 16:
        raise ValueError("The MOD comparison must contain exactly 16 fields.")

    with get_connection() as connection, connection.cursor() as cursor:
        document = insert_project_document(
            cursor,
            case_ref=case_ref,
            document_type="mod_pdf",
            filename=filename,
            file_bytes=pdf_bytes,
            source_classification="unknown",
        )
        if not document["created"]:
            raise ValueError("This PDF is already saved for the selected case.")

        revision = insert_revision(cursor, document["document_id"])
        inserted = insert_engineering_values(cursor, revision["revision_id"], values)
        if inserted["values_saved"] != len(values):
            raise RuntimeError("Incomplete MOD engineering-value snapshot.")

    return {
        "document_id": document["document_id"],
        "revision_id": revision["revision_id"],
        "revision_no": revision["revision_no"],
        "review_scope": "full_values",
        "pages_extracted": len(pages),
        "comparison_fields_saved": comparison_fields,
        "additional_fields_extracted": len(specifications),
        "values_saved": len(values),
    }
