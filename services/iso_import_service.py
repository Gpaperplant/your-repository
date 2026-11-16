
import hashlib

from services.ee_pdf_extractor import extract_ee_pdf
from services.iso_assessment_merge import read_and_merge_iso_assessment
from services.iso_excel_extractor import extract_iso_excel
from services.iso_pair_validator import normalise_lift_id, validate_same_lift
from services.iso_repository import get_case_elevator, save_iso_assessment


MAX_FILE_BYTES = 15 * 1024 * 1024

# Important shared values must agree within their configured tolerances.
CRITICAL_FIELDS = {
    "test_date",
    "iso_usage_category",
    "operating_days",
    "express_zone_m",
    "landing_floors",
    "efficiency_class",
    "daily_energy_wh",
    "annual_energy_kwh",
}


def process_iso_pair(
    case_ref,
    pdf_filename,
    pdf_bytes,
    excel_filename,
    excel_bytes,
):
    # Validate uploaded files.
    if not pdf_bytes.startswith(b"%PDF-") or not excel_bytes.startswith(b"PK"):
        raise ValueError("Upload an EE PDF and an ISO .xlsx file.")

    if max(len(pdf_bytes), len(excel_bytes)) > MAX_FILE_BYTES:
        raise ValueError("Each document must be 15 MB or smaller.")

    # Apply the same reconciliation rules as the Streamlit preview.
    preview = read_and_merge_iso_assessment(
        pdf_bytes,
        excel_bytes,
        ee_filename=pdf_filename,
        excel_filename=excel_filename,
    )

    if preview["review_status"] != "RECONCILED":
        issues = preview["conflicting_fields"] + preview["missing_fields"]
        raise ValueError(
            "Documents do not reconcile: " + ", ".join(issues)
        )

    # Extract all supported fields and validate the elevator.
    pdf_records = extract_ee_pdf(pdf_bytes)
    excel_records = extract_iso_excel(excel_bytes)
    result = validate_same_lift(pdf_records, excel_records)

    # Ensure both extraction paths identified the same elevator.
    if (
        normalise_lift_id(preview["elevator_id"])
        != normalise_lift_id(result["lift_id"])
    ):
        raise ValueError(
            "Preview and import identified different elevators."
        )

    pdf_fields = {row["field"]: row for row in pdf_records}
    excel_fields = {row["field"]: row for row in excel_records}

    # Reject material differences while preserving descriptive differences.
    conflicts = [
        item["field"]
        for item in result["differences"]
        if item["field"] in CRITICAL_FIELDS
        or pdf_fields[item["field"]].get("kind") == "measured"
        or excel_fields[item["field"]].get("kind") == "measured"
    ]

    if conflicts:
        raise ValueError(
            "Conflicting important fields: " + ", ".join(conflicts)
        )

    # Confirm the selected case belongs to this elevator.
    case_id, elevator_ref = get_case_elevator(case_ref)

    if (
        not str(elevator_ref).upper().startswith("TEST-LIFT-")
        and normalise_lift_id(elevator_ref)
        != normalise_lift_id(result["lift_id"])
    ):
        raise ValueError(
            f"Selected case belongs to {elevator_ref}, "
            f"not {result['lift_id']}."
        )

    # Preserve both original documents and their unique hashes.
    files = [
        {
            "type": doc_type,
            "filename": filename,
            "bytes": data,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        for doc_type, filename, data in (
            ("ee_pdf", pdf_filename, pdf_bytes),
            ("iso_excel", excel_filename, excel_bytes),
        )
    ]

    # Save the assessment and both source datasets.
    saved = save_iso_assessment(
        case_id,
        result["lift_id"],
        files,
        (pdf_records, excel_records),
        result["comparisons"],
    )

    return {
        **saved,
        "lift_id": result["lift_id"],
        "pdf_fields": len(pdf_records),
        "excel_fields": len(excel_records),
        "shared_fields": len(result["comparisons"]),
        "differences": result["differences"],
    }
