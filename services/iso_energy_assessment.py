
import hashlib

import streamlit as st

from services.iso_import_service import process_iso_pair
from services.iso_repository import get_cases


# Restrict ISO imports to the Admin role.
if st.session_state.get("role") != "Admin":
    st.error("Admin access required.")
    st.stop()


st.title("ISO Energy Assessment")
st.caption(
    "Upload a TÜV SÜD EE report and ISO calculation workbook "
    "for the same elevator."
)


# Load registered modernisation cases.
try:
    cases = get_cases()
except Exception as error:
    st.error(f"Cannot load cases: {error}")
    st.stop()

if not cases:
    st.info("Register a modernisation case before importing ISO documents.")
    st.stop()


# Collect both documents in one form.
with st.form("iso_import_form"):
    case_ref = st.selectbox(
        "Modernisation case",
        options=cases,
        index=None,
        placeholder="Select a case",
    )

    left, right = st.columns(2)

    with left:
        pdf_file = st.file_uploader(
            "TÜV SÜD EE Report",
            type=["pdf"],
            help="Upload the elevator energy efficiency test report.",
        )

    with right:
        excel_file = st.file_uploader(
            "ISO Calculation Excel",
            type=["xlsx"],
            help="Upload the corresponding ISO calculation workbook.",
        )

    submitted = st.form_submit_button(
        "Verify and Save",
        type="primary",
        disabled=not (case_ref and pdf_file and excel_file),
    )


# Process both documents automatically.
if submitted:
    st.session_state.pop("iso_import_result", None)

    with st.spinner("Extracting, verifying and saving documents..."):
        try:
            pdf_bytes = pdf_file.getvalue()
            excel_bytes = excel_file.getvalue()

            result = process_iso_pair(
                case_ref=case_ref,
                pdf_filename=pdf_file.name,
                pdf_bytes=pdf_bytes,
                excel_filename=excel_file.name,
                excel_bytes=excel_bytes,
            )

            # Keep the result visible after Streamlit reruns.
            st.session_state["iso_import_result"] = {
                **result,
                "case_ref": case_ref,
                "pdf_hash": hashlib.sha256(pdf_bytes).hexdigest(),
                "excel_hash": hashlib.sha256(excel_bytes).hexdigest(),
            }

        except ValueError as error:
            st.error(f"Import rejected: {error}")

        except Exception as error:
            st.error(f"Import failed: {error}")


# Display results only for the currently selected documents.
result = st.session_state.get("iso_import_result")

current_files_match = (
    result is not None
    and case_ref == result["case_ref"]
    and pdf_file is not None
    and excel_file is not None
    and hashlib.sha256(pdf_file.getvalue()).hexdigest()
        == result["pdf_hash"]
    and hashlib.sha256(excel_file.getvalue()).hexdigest()
        == result["excel_hash"]
)

if current_files_match:
    st.divider()

    if result["created"]:
        st.success("ISO assessment imported and saved successfully.")
    else:
        st.info("These documents have already been imported.")

    st.write(f"**Assessment ID:** {result['assessment_id']}")
    st.write(f"**Modernisation case:** {result['case_ref']}")
    st.write(f"**Lift ID:** {result['lift_id']}")

    # Show the number of fields imported from each document.
    pdf_count = result.get(
        "pdf_fields_saved",
        result["pdf_fields"],
    )
    excel_count = result.get(
        "excel_fields_saved",
        result["excel_fields"],
    )

    col1, col2, col3 = st.columns(3)

    col1.metric("EE PDF Fields", pdf_count)
    col2.metric("ISO Excel Fields", excel_count)
    col3.metric("Total Source Fields", pdf_count + excel_count)

    st.subheader("Automatic Validation")

    st.success("Both documents belong to the same elevator.")

    st.write(
        f"**Shared fields compared:** {result['shared_fields']}"
    )

    # Display non-identity differences without requiring approval.
    differences = result.get("differences", [])

    if differences:
        st.warning(
            f"{len(differences)} differences detected. "
            "Both source values have been preserved."
        )

        st.dataframe(
            differences,
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info("No significant differences detected.")

    # Show whether this import creates a new assessment version.
    previous_id = result.get("previous_assessment_id")

    if result["created"] and previous_id is not None:
        st.caption(
            f"New assessment version saved. "
            f"Previous assessment ID: {previous_id}."
        )

    st.caption(
        "Original documents and extracted values are stored in PostgreSQL. "
        "No manual review is required."
    )
