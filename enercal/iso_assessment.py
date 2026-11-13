
import json

import streamlit as st

from services.iso_assessment_merge import read_and_merge_iso_assessment
from services.iso_import_service import process_iso_pair
from services.iso_repository import get_cases


st.title("ISO Energy Assessment")
st.write(
    "Upload the **EE test PDF** and its **supporting ISO Excel** "
    "for the same lift. They will be combined into one assessment."
)

st.info(
    "The Excel is a supporting calculation, not a second independent "
    "measurement. ISO annual estimates are not verified modernisation savings."
)


# Upload both documents.
pdf_col, excel_col = st.columns(2)

with pdf_col:
    pdf_file = st.file_uploader(
        "EE test report",
        type=["pdf"],
        key="iso_ee_pdf",
    )

with excel_col:
    excel_file = st.file_uploader(
        "ISO calculation",
        type=["xlsx"],
        key="iso_excel",
    )

if pdf_file is None or excel_file is None:
    st.info("Upload both files to compare their values.")
    st.stop()

if max(pdf_file.size, excel_file.size) > 15 * 1024 * 1024:
    st.error("Each file must be 15 MB or smaller.")
    st.stop()


# Extract and reconcile both documents.
try:
    result = read_and_merge_iso_assessment(
        pdf_file.getvalue(),
        excel_file.getvalue(),
        ee_filename=pdf_file.name,
        excel_filename=excel_file.name,
    )
except Exception as error:
    st.error(f"Could not combine the documents: {error}")
    st.stop()


# Display comparison results.
rows = result["fields"]


def count(status):
    return sum(row["status"] == status for row in rows)


st.subheader(f"Combined assessment: {result['elevator_id']}")

matched, rounding, conflicts = st.columns(3)

matched.metric("Exact matches", count("MATCH"))
rounding.metric("Within rounding", count("ROUNDING"))
conflicts.metric(
    "Conflicts / missing",
    count("CONFLICT") + count("MISSING"),
)

reconciled = result["review_status"] == "RECONCILED"

if reconciled:
    st.success(
        "The documents reconcile. Both represent one lift assessment."
    )
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
    use_container_width=True,
)

st.caption(
    "EE PDF values are primary. Excel-only values remain labelled "
    "as reconstructed or assumed."
)


# Export the combined assessment.
st.download_button(
    "Download combined assessment (JSON)",
    data=json.dumps(result, indent=2, ensure_ascii=False),
    file_name=(
        f"{result['elevator_id'].replace(' ', '_')}"
        "_iso_assessment.json"
    ),
    mime="application/json",
)


# Only reconciled assessments can be saved.
if not reconciled:
    st.stop()

if st.session_state.get("role") != "Admin":
    st.info("Admin access is required to save the assessment.")
    st.stop()


# Select an existing modernisation case.
st.divider()
st.subheader("Save to PostgreSQL")

try:
    cases = get_cases()
except Exception as error:
    st.error(f"Database connection failed: {error}")
    st.stop()

if not cases:
    st.warning("No modernisation cases available.")
    st.stop()

case_ref = st.selectbox(
    "Modernisation case",
    cases,
    index=None,
    placeholder="Select a case",
    key="iso_save_case",
)


# Save both files and all extracted fields to PostgreSQL.
if st.button(
    "Save to PostgreSQL",
    type="primary",
    disabled=case_ref is None,
    key="iso_save_button",
):
    try:
        with st.spinner("Saving assessment..."):
            saved = process_iso_pair(
                case_ref=case_ref,
                pdf_filename=pdf_file.name,
                pdf_bytes=pdf_file.getvalue(),
                excel_filename=excel_file.name,
                excel_bytes=excel_file.getvalue(),
            )

        if saved["created"]:
            st.success("Assessment saved to PostgreSQL.")
        else:
            st.info("These documents are already saved.")

        st.write(f"**Assessment ID:** {saved['assessment_id']}")
        st.write(f"**Case:** {case_ref}")
        st.write(f"**Lift:** {saved['lift_id']}")

        col1, col2 = st.columns(2)
        col1.metric("PDF fields", saved["pdf_fields"])
        col2.metric("Excel fields", saved["excel_fields"])

    except Exception as error:
        st.error(f"PostgreSQL save failed: {error}")
