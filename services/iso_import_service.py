"""Validate and import one ISO Excel workbook for a modernisation case."""

import hashlib

from services.iso_excel_extractor import extract_iso_excel
from services.iso_repository import (
    get_case_elevator,
    normalise_lift_id,
    save_iso_excel_assessment,
)

MAX_FILE_BYTES = 15 * 1024 * 1024


def process_iso_excel(case_ref, excel_filename, excel_bytes):
    if not excel_bytes or len(excel_bytes) > MAX_FILE_BYTES:
        raise ValueError("ISO Excel file must be nonempty and no larger than 15 MB.")
    if not excel_filename.lower().endswith(".xlsx") or not excel_bytes.startswith(b"PK"):
        raise ValueError("Upload a valid ISO .xlsx workbook.")

    records = extract_iso_excel(excel_bytes)
    lift_ids = [row["value"] for row in records if row["field"] == "lift_id"]
    if len(lift_ids) != 1 or not normalise_lift_id(lift_ids[0]):
        raise ValueError("ISO Excel is missing a valid lift reference.")
    lift_id = str(lift_ids[0]).strip()

    case_id, case_elevator = get_case_elevator(case_ref)
    if (
        not str(case_elevator).upper().startswith("TEST-LIFT-")
        and normalise_lift_id(lift_id) != normalise_lift_id(case_elevator)
    ):
        raise ValueError(
            f"Selected case belongs to {case_elevator}, not {lift_id}."
        )

    checksum = hashlib.sha256(excel_bytes).hexdigest()
    saved = save_iso_excel_assessment(
        case_id=case_id,
        lift_id=lift_id,
        filename=excel_filename,
        file_bytes=excel_bytes,
        records=records,
        checksum=checksum,
    )
    return {**saved, "lift_id": lift_id, "excel_fields": len(records)}
