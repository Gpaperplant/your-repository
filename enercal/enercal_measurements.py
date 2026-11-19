"""Upload ISO Excel energy calculations and save them to a modernisation case."""

import hashlib
import json

import streamlit as st

from services.iso_excel_extractor import extract_iso_excel
from services.iso_import_service import MAX_FILE_BYTES, process_iso_excel
from services.iso_repository import get_cases


def main():
    st.title("ISO Excel Upload")
    st.caption("Upload an ISO calculation workbook for a registered elevator.")

    file = st.file_uploader(
        "ISO calculation workbook", type=["xlsx"], key="measure_iso_excel"
    )
    if file is None:
        return

    file_bytes = file.getvalue()
    if not file_bytes or len(file_bytes) > MAX_FILE_BYTES:
        st.error("Choose a nonempty workbook no larger than 15 MB.")
        return
    if not file.name.lower().endswith(".xlsx") or not file_bytes.startswith(b"PK"):
        st.error("Choose a valid ISO .xlsx workbook.")
        return

    checksum = hashlib.sha256(file_bytes).hexdigest()
    cached = st.session_state.get("measurement_iso_preview")
    if cached and cached[0] == checksum:
        records = cached[1]
    else:
        try:
            records = extract_iso_excel(file_bytes)
        except Exception as error:
            st.session_state.pop("measurement_iso_preview", None)
            st.error(f"Could not read ISO Excel: {error}")
            return
        st.session_state["measurement_iso_preview"] = (checksum, records)

    refs = [row["value"] for row in records if row["field"] == "lift_id"]
    if len(refs) != 1 or not str(refs[0] or "").strip():
        st.error("The workbook does not contain a valid elevator reference.")
        return

    lift_id = str(refs[0]).strip()
    st.subheader(f"ISO calculation: {lift_id}")
    st.metric("Extracted fields", len(records))
    st.dataframe(
        [
            {
                "Field": item["field"],
                "Value": item.get("value"),
                "Unit": item.get("unit") or "",
                "Excel sheet": item.get("sheet") or "",
                "Excel cell": item.get("cell") or "",
            }
            for item in records
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption("ISO annual consumption is a calculated estimate, not verified savings.")
    st.download_button(
        "Download extracted data (JSON)",
        data=json.dumps(
            {"lift_id": lift_id, "fields": records},
            indent=2, ensure_ascii=False, default=str,
        ),
        file_name=f"{lift_id.replace(' ', '_')}_iso_excel.json",
        mime="application/json",
    )

    if st.session_state.get("role") != "Admin":
        st.write("Admin access is required to save the workbook.")
        return

    st.subheader("Save ISO calculation")
    try:
        cases = get_cases()
    except Exception as error:
        st.error(f"Could not load modernisation cases: {error}")
        return
    if not cases:
        st.warning("No modernisation cases are available.")
        return

    case_ref = st.selectbox(
        "Modernisation case", cases, index=None,
        placeholder="Select a case", key="measure_iso_case",
    )
    if not st.button(
        "Save ISO Excel", type="primary", disabled=case_ref is None,
        key="measure_iso_save",
    ):
        return

    try:
        with st.spinner("Saving ISO calculation..."):
            saved = process_iso_excel(case_ref, file.name, file_bytes)
    except Exception as error:
        st.error(f"PostgreSQL save failed: {error}")
        return

    message = (
        "ISO Excel saved to PostgreSQL."
        if saved["created"] else "This ISO Excel is already saved."
    )
    st.write(f":green[{message}]")
    st.write(f"**Assessment ID:** {saved['assessment_id']}")
    st.write(f"**Case:** {case_ref}")
    st.write(f"**Elevator:** {saved['lift_id']}")
    st.metric("Excel fields", saved["excel_fields"])


main()
