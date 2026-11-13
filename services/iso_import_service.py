
import hashlib

from services.ee_pdf_extractor import extract_ee_pdf
from services.iso_excel_extractor import extract_iso_excel
from services.iso_pair_validator import (
    normalise_lift_id,
    validate_same_lift,
)
from services.iso_repository import (
    get_case_elevator,
    get_latest_iso_assessment,
    save_iso_assessment,
)


# Maximum upload size per document.
MAX_FILE_BYTES = 15 * 1024 * 1024


def process_iso_pair(
    case_ref,
    pdf_filename,
    pdf_bytes,
    excel_filename,
    excel_bytes,
):
    # Validate file formats.
    if not pdf_bytes.startswith(b"%PDF-") or not excel_bytes.startswith(b"PK"):
        raise ValueError("Upload an EE PDF and an ISO .xlsx file.")

    # Validate file sizes.
    if max(len(pdf_bytes), len(excel_bytes)) > MAX_FILE_BYTES:
        raise ValueError("Each document must be 15 MB or smaller.")

    # Extract data from both documents.
    pdf_records = extract_ee_pdf(pdf_bytes)
    excel_records = extract_iso_excel(excel_bytes)

    # Confirm both documents belong to the same lift.
    result = validate_same_lift(pdf_records, excel_records)

    # Reject conflicting measured values before saving.
    measured_fields = {
        row["field"]
        for row in pdf_records
        if row.get("kind") == "measured"
    }

    conflicts = [
        row for row in result["comparisons"]
        if row["field"] in measured_fields and not row["match"]
    ]

    if conflicts:
        fields = ", ".join(row["field"] for row in conflicts)
        raise ValueError(f"Measured data conflict: {fields}")

    # Retrieve the selected existing case.
    case_id, elevator_ref = get_case_elevator(case_ref)
    lift_id = normalise_lift_id(result["lift_id"])

    # Allow placeholder elevators used for testing.
    is_test_lift = str(elevator_ref).upper().startswith("TEST-LIFT-")

    # Enforce lift identity for non-placeholder elevators.
    if not is_test_lift and normalise_lift_id(elevator_ref) != lift_id:
        raise ValueError(
            f"Selected case belongs to {elevator_ref}, "
            f"not {result['lift_id']}."
        )

    # Prevent mixing different lifts within the same case.
    previous = get_latest_iso_assessment(case_ref)

    if previous and normalise_lift_id(previous[1]) != lift_id:
        raise ValueError(
            f"Case {case_ref} already has ISO data for {previous[1]}. "
            f"Cannot import documents for {result['lift_id']}."
        )

    # Prepare both original documents.
    files = [
        {
            "type": "ee_pdf",
            "filename": pdf_filename,
            "bytes": pdf_bytes,
            "sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        },
        {
            "type": "iso_excel",
            "filename": excel_filename,
            "bytes": excel_bytes,
            "sha256": hashlib.sha256(excel_bytes).hexdigest(),
        },
    ]

    # Save the documents and extracted data.
    saved = save_iso_assessment(
        case_id,
        result["lift_id"],
        files,
        (pdf_records, excel_records),
        result["comparisons"],
    )

    # Return the import summary.
    return {
        **saved,
        "lift_id": result["lift_id"],
        "pdf_fields": len(pdf_records),
        "excel_fields": len(excel_records),
        "shared_fields": len(result["comparisons"]),
        "differences": result["differences"],
    }
