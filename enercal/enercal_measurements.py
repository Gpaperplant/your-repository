"""Upload EE test and ISO calculation evidence together for one elevator."""

import hashlib
import json
from collections import Counter

import streamlit as st

from services.iso_assessment_merge import read_and_merge_iso_assessment
from services.iso_import_service import process_iso_pair
from services.iso_repository import get_cases

MAX_FILE_BYTES = 15 * 1024 * 1024


def main():
    st.title("Energy Measurements")
    st.caption("Upload the EE test PDF and its supporting ISO Excel calculation")
    st.info(
        "Both documents describe the same elevator test. ISO annual estimates are "
        "not independently verified before-and-after energy savings."
    )

    pdf_column, excel_column = st.columns(2)
    with pdf_column:
        pdf_file = st.file_uploader("EE test report", type=["pdf"], key="measure_ee_pdf")
    with excel_column:
        excel_file = st.file_uploader("ISO calculation", type=["xlsx"], key="measure_iso_excel")

    if pdf_file is None or excel_file is None:
        st.info("Upload both files to compare their values.")
        st.stop()

    if max(pdf_file.size, excel_file.size) > MAX_FILE_BYTES:
        st.error("Each file must be 15 MB or smaller.")
        st.stop()

    # Reuse a validated preview when the uploaded files have not changed.
    pdf_bytes, excel_bytes = pdf_file.getvalue(), excel_file.getvalue()
    if not pdf_bytes.startswith(b"%PDF-") or not excel_bytes.startswith(b"PK"):
        st.error("Upload a valid EE PDF and ISO .xlsx workbook.")
        st.stop()

    fingerprint = (
        pdf_file.name, hashlib.sha256(pdf_bytes).digest(),
        excel_file.name, hashlib.sha256(excel_bytes).digest(),
    )
    cached = st.session_state.get("measurement_iso_preview")
    if cached is not None and cached[0] == fingerprint:
        result = cached[1]
    else:
        try:
            result = read_and_merge_iso_assessment(
                pdf_bytes, excel_bytes,
                ee_filename=pdf_file.name,
                excel_filename=excel_file.name,
            )
        except Exception as error:
            st.session_state.pop("measurement_iso_preview", None)
            st.error(f"Could not compare the documents: {error}")
            st.stop()
        st.session_state["measurement_iso_preview"] = (fingerprint, result)

    rows = result["fields"]
    counts = Counter(row["status"] for row in rows)
    st.subheader(f"Combined assessment: {result['elevator_id']}")
    matched, rounding, conflicts = st.columns(3)
    matched.metric("Exact matches", counts["MATCH"])
    rounding.metric("Within rounding", counts["ROUNDING"])
    conflicts.metric("Conflicts / missing", counts["CONFLICT"] + counts["MISSING"])

    reconciled = result["review_status"] == "RECONCILED"
    if reconciled:
        st.success("The documents reconcile. Both represent one lift assessment.")
    else:
        st.error("Documents do not reconcile. Database saving is disabled.")

    st.dataframe(
        [
            {
                "Field": row["name"],
                "EE PDF": row["pdf_value"],
                "ISO Excel": row["excel_value"],
                "Unit": row["unit"],
                "Result": row["status"],
                "Selected": row["value"],
                "PDF page": row["ee_pdf_page"],
                "Excel sheet": row["iso_excel_sheet"],
                "Excel cell": row["iso_excel_cell"],
            }
            for row in rows
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "EE PDF values are primary; Excel-only calculations are shown as "
        "supporting evidence, not separate meter measurements."
    )
    st.download_button(
        "Download combined assessment (JSON)",
        data=json.dumps(result, indent=2, ensure_ascii=False),
        file_name=f"{result['elevator_id'].replace(' ', '_')}_ee_iso_measurements.json",
        mime="application/json",
    )

    # Retain preview/export access; only admins can persist reconciled pairs.
    if not reconciled:
        st.stop()
    if st.session_state.get("role") != "Admin":
        st.info("Admin access is required to save the assessment.")
        st.stop()

    st.divider()
    st.subheader("Save measurement evidence")
    try:
        cases = get_cases()
    except Exception as error:
        st.error(f"Database connection failed: {error}")
        st.stop()

    if not cases:
        st.warning("No modernisation cases available.")
        st.stop()

    case_ref = st.selectbox(
        "Modernisation case", cases, index=None,
        placeholder="Select a case", key="measure_iso_case",
    )
    if not st.button(
        "Save EE PDF and ISO Excel", type="primary",
        disabled=case_ref is None, key="measure_iso_save",
    ):
        return

    try:
        with st.spinner("Saving assessment..."):
            saved = process_iso_pair(
                case_ref=case_ref,
                pdf_filename=pdf_file.name,
                pdf_bytes=pdf_bytes,
                excel_filename=excel_file.name,
                excel_bytes=excel_bytes,
            )
    except Exception as error:
        st.error(f"PostgreSQL save failed: {error}")
        return

    if saved["created"]:
        st.success("EE PDF and ISO Excel saved to PostgreSQL.")
    else:
        st.info("This document pair is already saved.")

    st.write(f"**Assessment ID:** {saved['assessment_id']}")
    st.write(f"**Case:** {case_ref}")
    st.write(f"**Lift:** {saved['lift_id']}")
    first, second = st.columns(2)
    first.metric("PDF fields", saved["pdf_fields"])
    second.metric("Excel fields", saved["excel_fields"])


main()
