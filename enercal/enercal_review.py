from decimal import Decimal

import altair as alt
import pandas as pd
import pymupdf
import streamlit as st

from services.enercal_extractor import extract_full_energy_comparison
from services.enercal_specs import build_crosscheck_details

from services.enercal_review_repository import (
    CALCULATED_FIELD,
    EXPECTED_FIELDS,
    FIELD_LABELS,
    get_review_details,
    get_review_documents,
    get_specifications,
    resubmit_revision,
    review_revision,
    submit_verification,
)


# Control access to the engineering review workspace.
if st.session_state.get("role") != "Admin":
    st.error("Admin access is required to review EnerCal reports.")
    st.stop()


STATUS_LABELS = {
    "pending": "Pending Review",
    "changes_requested": "Changes Requested",
    "further_verification_required": "Further Verification Required",
    "approved": "Approved",
    "rejected": "Final Rejection",
}

STATUS_GUIDANCE = {
    "pending": "Review the PDF and extracted values, then record an engineering decision.",
    "changes_requested": "Check the original PDF, then confirm or correct the extracted values.",
    "further_verification_required": "Confirm the original values or submit corrections.",
    "approved": "Approved as EnerCal estimates, not verified meter measurements.",
    "rejected": "This report was rejected. Its PDF and audit history are preserved.",
}


def finish_action(message):
    # Display the result after refreshing the database state.
    st.session_state["enercal_review_message"] = message
    st.rerun()


def display_number(value):
    # Format numeric values without losing meaningful decimals.
    if value is None:
        return "N/A"
    try:
        from decimal import Decimal
        value = Decimal(str(value))
        if not value.is_finite():
            return "N/A"
        result = format(value, ",f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    except (ValueError, TypeError, ArithmeticError):
        return "N/A"


def display_evidence(value):
    """Format a PDF value for the evidence table without changing calculations."""
    if value is None:
        return "—"
    try:
        number = Decimal(str(value).replace(",", "").strip())
        return f"{number:,.4f}".rstrip("0").rstrip(".")
    except (ValueError, ArithmeticError):
        return str(value)


@st.cache_data(show_spinner=False, max_entries=24)
def original_comparison(pdf_bytes):
    """Cache comparison extracted from the unmodified PDF."""
    return extract_full_energy_comparison(pdf_bytes)


def display_percent(value):
    # Preserve the precision of reported percentages.
    if value is None:
        return "N/A"
    value = str(value).strip().removesuffix("%").strip()
    return f"{display_number(value)}%" if value else "N/A"


def status_message(status):
    # Display current engineering review status.
    message = STATUS_GUIDANCE.get(status, "Review status is not recognised.")
    if status == "approved":
        st.success(message)
    elif status == "rejected":
        st.error(message)
    elif status in ("changes_requested", "further_verification_required"):
        st.warning(message)
    else:
        st.info(message)


@st.cache_data(show_spinner=False, max_entries=24)
def pdf_page_info(pdf_bytes):
    # Locate the original comparison page dynamically.
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if not len(pdf):
            raise ValueError("The source PDF contains no pages.")
        for index, page in enumerate(pdf):
            if "EXISTING TECHNOLOGY COMPARISON" in page.get_text("text").upper():
                return len(pdf), index + 1
        return len(pdf), 1


@st.cache_data(show_spinner=False, max_entries=48)
def render_source_page(pdf_bytes, page_number):
    # Render the saved original PDF without external services.
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if not 1 <= page_number <= len(pdf):
            raise ValueError("The requested page does not exist.")
        return pdf[page_number - 1].get_pixmap(dpi=155, alpha=False).tobytes("png")


def show_original_pdf_page(pdf_bytes, document_id, view_name):
    # Allow engineers to inspect any page of the original report.
    if not pdf_bytes:
        st.warning("The original PDF is unavailable.")
        return
    try:
        count, comparison_page = pdf_page_info(pdf_bytes)
    except Exception as error:
        st.warning(f"Unable to read the PDF: {error}")
        return

    left, right = st.columns([1, 2], vertical_alignment="bottom")
    with left:
        chosen = st.selectbox(
            "Original PDF page", range(1, count + 1),
            index=comparison_page - 1,
            key=f"enercal_pdf_page_{view_name}_{document_id}",
            format_func=lambda n: f"Page {n} of {count}",
        )
    with right:
        st.caption(f"Comparison on page {comparison_page}; displaying the original PDF.")
    try:
        st.image(
            render_source_page(pdf_bytes, chosen), width="stretch",
            caption=f"Original EnerCal source PDF · Page {chosen} of {count}",
        )
    except Exception as error:
        st.warning(f"Could not display this page: {error}")


# Load the case, latest revision, and saved additional observations.
st.title("EnerCal | Case Workspace")
st.caption(
    "Review extracted EnerCal estimates. Approval does not verify actual energy savings."
)
if "enercal_review_message" in st.session_state:
    st.success(st.session_state.pop("enercal_review_message"))

try:
    documents = get_review_documents()
except Exception as error:
    st.error(f"Unable to load EnerCal cases: {error}")
    st.stop()
if not documents:
    st.info("No EnerCal reports are available for engineering review yet.")
    st.stop()

st.subheader("Select a case")
case_col, report_col = st.columns([1, 2])
with case_col:
    selected_case = st.selectbox(
        "Modernisation Case", sorted({row[1] for row in documents}),
        key="enercal_workspace_case",
    )
case_documents = [row for row in documents if row[1] == selected_case]
report_labels = {
    row[0]: f"{row[2]}  |  Document {row[0]}" for row in case_documents
}
with report_col:
    document_id = st.selectbox(
        "EnerCal PDF Report", list(report_labels),
        format_func=lambda value: report_labels[value],
        key=f"enercal_workspace_report_{selected_case}",
    )

try:
    details = get_review_details(document_id)
except Exception as error:
    st.error(f"Unable to load this report: {error}")
    st.stop()

revision_no = details["revision_no"]
review_status = details["status"]
candidates = details["candidates"]
candidate_lookup = {row["field_name"]: row for row in candidates}
valid_revision = (
    len(candidates) == len(EXPECTED_FIELDS)
    and set(candidate_lookup) == EXPECTED_FIELDS
    and len({row["page_id"] for row in candidates}) == 1
    and all(row["revision_no"] == revision_no for row in candidates)
)
if not valid_revision or set(FIELD_LABELS) != EXPECTED_FIELDS:
    st.error("The latest revision does not contain exactly 16 valid comparison fields.")
    st.stop()

# Read additional fields once, then separate cross-checks from specifications.
try:
    extra_fields = get_specifications(document_id)
except Exception as error:
    st.error(f"Unable to load additional specifications: {error}")
    st.stop()

checks = [row for row in extra_fields if row["Unit"] == "cross-page check"]
specifications = [row for row in extra_fields if row["Unit"] != "cross-page check"]
counts = {
    status: sum(row["Value"] == status for row in checks)
    for status in ("PASS", "REVIEW", "MISSING")
}

st.subheader("Case overview")
summary = st.columns([1.2, 0.7, 1.5], gap="large")
with summary[0]:
    st.caption("MODERNISATION CASE")
    st.subheader(str(selected_case))
with summary[1]:
    st.caption("CURRENT REVISION")
    st.markdown(f"### {revision_no}")
with summary[2]:
    st.caption("ENGINEERING STATUS")
    st.markdown(f"**{STATUS_LABELS.get(review_status, review_status)}**")
status_message(review_status)

overview_tab, document_tab, review_tab, history_tab = st.tabs(
    ["Overview", "PDF & Data", "Engineering Review", "History"]
)


# Overview: preserve the existing comparison metrics and visualisation.
with overview_tab:
    st.subheader("Estimated energy performance")
    existing_energy = candidate_lookup["existing_annual_energy_consumption"]["numeric_value"]
    proposed_energy = candidate_lookup["proposed_annual_energy_consumption"]["numeric_value"]
    calculated_saving = candidate_lookup[CALCULATED_FIELD]["numeric_value"]
    reported_saving = candidate_lookup["reported_energy_saving_percent"]["numeric_value"]

    metrics = st.columns(4)
    for col, label, value in zip(
        metrics,
        ["Existing (kWh/year)", "Proposed (kWh/year)", "Calculated Saving", "EnerCal Reported"],
        [display_number(existing_energy), display_number(proposed_energy),
         display_percent(calculated_saving), display_percent(reported_saving)],
    ):
        col.metric(label, value)

    if existing_energy is not None and proposed_energy is not None:
        energy_chart = pd.DataFrame({
            "Elevator": ["Existing elevator", "Proposed elevator"],
            "Annual energy (kWh/year)": [float(existing_energy), float(proposed_energy)],
        })
        chart_max = max(float(existing_energy), float(proposed_energy), 1) * 1.22
        bars = alt.Chart(energy_chart).mark_bar(
            size=38, cornerRadiusEnd=5,
        ).encode(
            y=alt.Y("Elevator:N", sort=["Existing elevator", "Proposed elevator"],
                    title=None, axis=alt.Axis(labelAngle=0, labelLimit=200)),
            x=alt.X("Annual energy (kWh/year):Q",
                    scale=alt.Scale(domain=[0, chart_max]),
                    axis=alt.Axis(title="Annual energy (kWh/year)", format=",.0f")),
            color=alt.Color("Elevator:N", scale=alt.Scale(
                domain=["Existing elevator", "Proposed elevator"],
                range=["#7AA6C2", "#2EAAA1"],
            ), legend=None),
        )
        labels = alt.Chart(energy_chart).mark_text(
            align="left", baseline="middle", dx=9, fontSize=14,
        ).encode(
            y=alt.Y("Elevator:N", sort=["Existing elevator", "Proposed elevator"]),
            x=alt.X("Annual energy (kWh/year):Q"),
            text=alt.Text("Annual energy (kWh/year):Q", format=",.0f"),
        )
        st.altair_chart((bars + labels).properties(height=150), width="stretch")
        st.caption("These are EnerCal estimates, not verified meter measurements.")

    st.subheader("Existing vs proposed elevator")
    comparison_rows = [{
        "Role": row["technology_role"].title(),
        "Elevator Technology": row["elevator_technology"],
        "Annual Energy (kWh/year)": display_number(row["annual_energy_consumption_kwh_year"]),
        "Regenerative Energy (kWh/year)": display_number(row["annual_regenerative_energy_kwh_year"]),
        "Annual Expenditure (BHD/year)": display_number(row["annual_electricity_expenditure_bhd_year"]),
        "Lifetime Expenditure (BHD)": display_number(row["lifetime_electricity_expenditure_bhd"]),
        "Operational CO2e (kg / 20 years)": display_number(row["operational_co2e_kg_lifetime_20_years"]),
        "Embodied CO2e (kg)": display_number(row["embodied_co2e_kg"]),
    } for row in details["comparison"]]
    if comparison_rows:
        st.dataframe(comparison_rows, hide_index=True, width="stretch")
    else:
        st.warning("No comparison preview is available for this revision.")
    st.caption("Source: stored EnerCal PDF. Values are report estimates.")


# PDF & Data: show cross-page validation separately from engineering inputs.
with document_tab:
    st.subheader("Original source document")
    st.write("**File:**", details["filename"])
    st.write("**Document ID:**", document_id)
    st.download_button(
        "Download Original EnerCal PDF", data=details["pdf_bytes"],
        file_name=details["filename"], mime="application/pdf",
        key=f"enercal_download_{document_id}",
    )
    with st.expander("Preview the original PDF", expanded=False):
        show_original_pdf_page(details["pdf_bytes"], document_id, "document")

    st.subheader("Extracted comparison fields")
    field_filter = st.selectbox(
        "Filter extracted fields",
        ["All fields", "Existing elevator", "Proposed elevator", "Savings"],
        key=f"enercal_field_filter_{document_id}",
    )

    def included_field(field_name):
        if field_filter == "Existing elevator":
            return field_name.startswith("existing_")
        if field_filter == "Proposed elevator":
            return field_name.startswith("proposed_")
        if field_filter == "Savings":
            return field_name.endswith("_saving_percent")
        return True

    field_rows = [{
        "Field": label,
        "Extracted Value": candidate_lookup[name]["raw_value"],
        "Unit": candidate_lookup[name]["unit"] or "",
    } for name, label in FIELD_LABELS.items()
        if name in candidate_lookup and included_field(name)]
    st.dataframe(field_rows, hide_index=True, width="stretch")
    st.caption(f"Showing {len(field_rows)} of {len(EXPECTED_FIELDS)} fields from revision {revision_no}.")

    # Show the original PDF's page-2 report details in their own table.
    page2_values = {
        row["Field"].casefold(): row["Value"] or ""
        for row in specifications
        if row["Source page"] == 2
    }
    page2_fields = [
        ("Customer Contact Information", "Client", "Client"),
        ("Customer Contact Information", "Project", "Project name"),
        ("Customer Contact Information", "Site", "Site"),
        ("Main Elevator Specification", "Platform Type", "Platform type"),
        ("Main Elevator Specification", "Quantity of identical elevators", "Elevator quantity"),
        ("Main Elevator Specification", "Group Name", "Group name"),
        ("KONE Contact Information", "Contact first name", "Contact first name"),
        ("KONE Contact Information", "Contact last name", "Contact last name"),
        ("KONE Contact Information", "Address", "Contact address"),
        ("KONE Contact Information", "City", "Contact city"),
        ("KONE Contact Information", "Country", "Contact country"),
        ("KONE Contact Information", "Phone", "Contact phone"),
        ("KONE Contact Information", "Email", "Contact email"),
        ("Report Details", "Report date", "Report date"),
    ]

    st.subheader("Page 2 — Report Information")
    if not page2_values:
        st.info("No page-2 information stored. Backfill this document if needed.")
    else:
        st.dataframe(
            [
                {"Section": section, "Field": field,
                 "Value": page2_values.get(key.casefold(), "")}
                for section, field, key in page2_fields
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption("Blank cells mean no value was provided in the PDF.")

    # Show the PDF's page-3 energy-performance summary separately.
    page3_values = {
        row["Field"].casefold(): row["Value"] or ""
        for row in specifications
        if row["Source page"] == 3
    }
    page3_fields = [
        ("Energy saving", "P3 energy saving percent", ""),
        ("ISO 25745-2 energy efficiency class", "P3 iso class", ""),
        ("Annual electricity expenditure", "P3 annual cost bhd", "BHD/year"),
        ("Operational CO₂e over lifetime", "P3 operational co2e kg", "kg CO₂e"),
        ("Annual energy consumption", "P3 annual energy kwh", "kWh/year"),
        ("Regenerative energy saving", "P3 regen saving percent", ""),
    ]

    st.subheader("Page 3 — Energy Performance Summary")
    if page3_values:
        st.dataframe(
            [
                {
                    "Field": label,
                    "Value": page3_values.get(key.casefold(), ""),
                    "Unit": unit,
                }
                for label, key, unit in page3_fields
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption("Original PDF estimates; missing report values remain blank.")
    else:
        st.info("No page-3 summary stored. Backfill the report if needed.")

    st.subheader("Cross-page validation")
    st.caption("Checks original PDF pages against the page-4 comparison, not actual meter readings.")
    if checks:
        metric_cols = st.columns(3)
        metric_cols[0].metric("Passed", counts["PASS"])
        metric_cols[1].metric("Needs Review", counts["REVIEW"])
        metric_cols[2].metric("Missing", counts["MISSING"])

        if counts["REVIEW"] or counts["MISSING"]:
            st.warning(
                f"{counts['REVIEW']} checks need review and "
                f"{counts['MISSING']} have missing values."
            )
        else:
            st.success("All stored cross-page checks passed.")

        status_filter = st.selectbox(
            "Filter validation results", ["All", "REVIEW", "MISSING", "PASS"],
            key=f"validation_filter_{document_id}",
        )
        filtered = [
            row for row in checks
            if status_filter == "All" or row["Value"] == status_filter
        ]
        order = {"REVIEW": 0, "MISSING": 1, "PASS": 2}
        filtered.sort(key=lambda row: (order.get(row["Value"], 3), row["Field"]))
        # Reuse the extractor's rules to show the source and reference figures.
        try:
            evidence = build_crosscheck_details(
                extra_fields, original_comparison(details["pdf_bytes"])
            )
        except (ValueError, KeyError, TypeError) as error:
            evidence = []
            st.warning(f"Could not display validation details: {error}")

        evidence = [
            item for item in evidence
            if status_filter == "All" or item["saved_status"] == status_filter
        ]
        if evidence:
            evidence.sort(key=lambda item: (
                order.get(item["saved_status"], 3), item["key"]
            ))
            st.dataframe([{
                "Check": item["key"].replace("_", " ").title(),
                "Status": item["saved_status"],
                "Source": display_evidence(item["source"]),
                "Reference": display_evidence(item["reference"]),
                "Difference": display_evidence(item["difference"]),
                "Page": item["page"],
            } for item in evidence], hide_index=True, width="stretch")

            disagreements = [
                item for item in evidence
                if item["status"] != item["saved_status"]
            ]
            if disagreements:
                st.warning(
                    f"{len(disagreements)} stored check status(es) no longer match "
                    "the current validation rules. Recheck the original PDF."
                )

            selected = st.selectbox(
                "Explain a cross-check",
                range(len(evidence)),
                format_func=lambda i: evidence[i]["key"].replace("_", " ").title(),
                key=f"enercal_check_detail_{document_id}_{status_filter}",
            )
            if selected is not None:
                selected_check = evidence[selected]
                st.caption(
                    f"Rule: {selected_check['rule']}. "
                    f"Tolerance: {selected_check['tolerance']}. "
                    f"Source: page {selected_check['page']} of the original PDF."
                )
        elif filtered:
            st.dataframe([{
                "Check": row["Field"],
                "Status": row["Value"],
                "Source page": row["Source page"],
            } for row in filtered], hide_index=True, width="stretch")
        else:
            st.info(f"No {status_filter} validation results for this report.")
        st.caption(
            "Checks are based on the original upload; they do not automatically "
            "change when engineers correct a later revision."
        )
    else:
        st.info("No stored cross-page checks for this report. Older PDFs may need a backfill.")

    with st.expander("Additional engineering specifications (unreviewed)"):
        other_specs = [
            row for row in specifications if row["Source page"] not in (2, 3)
        ]
        if other_specs:
            st.dataframe(other_specs, hide_index=True, width="stretch")
        else:
            st.info("No other engineering specifications stored for this report.")


# Engineering Review: preserve the existing 16-field review and corrections.
with review_tab:
    st.subheader("Engineering review")
    if counts["REVIEW"] or counts["MISSING"]:
        st.warning(
            f"Original PDF cross-checks: {counts['REVIEW']} need review; "
            f"{counts['MISSING']} missing. Inspect the PDF & Data tab."
        )
    elif not checks:
        st.info("No cross-page validation was saved for this report.")
    st.caption(
        "Check all 16 values against the PDF. Your decision and corrections "
        "are recorded in History. Cross-checks are advisory."
    )
    with st.expander("Original EnerCal PDF · Review the source page", expanded=True):
        show_original_pdf_page(details["pdf_bytes"], document_id, "review")

    table_rows = [{
        "Field": label,
        "Extracted value": str(candidate_lookup[name]["raw_value"]),
        "Unit": candidate_lookup[name]["unit"] or "—",
    } for name, label in FIELD_LABELS.items()]
    st.markdown("#### Extracted values")
    st.caption(f"Revision {revision_no} · {len(table_rows)} fields · Read-only")
    st.dataframe(
        pd.DataFrame(table_rows), hide_index=True, width="stretch", height=600,
        column_config={
            "Field": st.column_config.TextColumn("Field", width="large"),
            "Extracted value": st.column_config.TextColumn("Extracted value", width="medium"),
            "Unit": st.column_config.TextColumn("Unit", width="medium"),
        },
    )

    if review_status in ("pending", "changes_requested", "further_verification_required"):
        mode_key = f"enercal_review_mode_{document_id}_{revision_no}"
        if mode_key not in st.session_state:
            st.session_state[mode_key] = None
        st.markdown("#### Are the extracted values correct?")
        left, right = st.columns(2, gap="medium")
        with left:
            if st.button(
                "✓ Values are correct", type="primary", width="stretch",
                key=f"enercal_values_correct_{document_id}_{revision_no}",
            ):
                st.session_state[mode_key] = "correct"
        with right:
            if st.button(
                "✎ Values need correction", width="stretch",
                key=f"enercal_values_wrong_{document_id}_{revision_no}",
            ):
                st.session_state[mode_key] = "edit"

        mode = st.session_state.get(mode_key)
        if mode == "correct":
            st.divider()
            if review_status == "pending":
                st.markdown("##### Approve this extraction")
                st.caption("Approves reported estimates only, not measured energy savings.")
            else:
                st.markdown("##### Confirm the existing values")
                st.caption("Returns this revision to Pending for re-review; not approval.")
            with st.form(f"enercal_correct_form_{document_id}_{revision_no}"):
                explanation = st.text_input(
                    "Review note (optional)" if review_status == "pending"
                    else "Reason no correction is needed (required)",
                )
                confirmed = st.checkbox(
                    "I checked all 16 extracted fields against the original PDF."
                )
                button_text = (
                    "Confirm and approve extraction" if review_status == "pending"
                    else "Confirm values for re-review"
                )
                submitted = st.form_submit_button(button_text, type="primary")
                if submitted:
                    if not confirmed:
                        st.error("Please confirm that the PDF values were checked.")
                    elif review_status != "pending" and not explanation.strip():
                        st.error("Explain why no correction is needed.")
                    else:
                        try:
                            if review_status == "pending":
                                result = review_revision(
                                    document_id, "approved", explanation.strip()
                                )
                                message = f"Revision {result} approved as an EnerCal estimate."
                            else:
                                result = submit_verification(
                                    document_id, explanation.strip()
                                )
                                message = f"Revision {result} returned to Pending."
                        except Exception as error:
                            st.error(f"Could not save review: {error}")
                        else:
                            finish_action(message)

        elif mode == "edit":
            st.divider()
            st.markdown("##### Correct extracted values")
            st.caption(
                "Edit incorrect values only. The backend recalculates the "
                "saving percentage from the two annual energy values."
            )
            editor_rows = [{
                "Field key": name,
                "Field": label,
                "Original value": str(candidate_lookup[name]["raw_value"]),
                "Corrected value": str(candidate_lookup[name]["raw_value"]),
            } for name, label in FIELD_LABELS.items() if name != CALCULATED_FIELD]
            editor_df = pd.DataFrame(editor_rows)

            with st.form(f"enercal_edit_form_{document_id}_{revision_no}"):
                edited_df = st.data_editor(
                    editor_df, hide_index=True, num_rows="fixed", width="stretch",
                    height=580, disabled=["Field key", "Field", "Original value"],
                    column_config={
                        "Field key": None,
                        "Field": st.column_config.TextColumn("Field", width="large"),
                        "Original value": st.column_config.TextColumn("Original value", width="medium"),
                        "Corrected value": st.column_config.TextColumn(
                            "Corrected value", width="medium",
                            help="Edit values that differ from the original PDF.",
                        ),
                    },
                    key=f"enercal_editor_{document_id}_{revision_no}",
                )
                correction_note = st.text_area(
                    "Correction explanation", height=100,
                    placeholder="Describe which values were corrected and why.",
                )
                confirmed = st.checkbox(
                    "I checked the corrected values against the original PDF.",
                    key=f"enercal_correction_confirm_{document_id}_{revision_no}",
                )
                submitted = st.form_submit_button("Submit corrected values", type="primary")
                if submitted:
                    corrected_values = {
                        row["Field key"]: str(row["Corrected value"]).strip()
                        for _, row in edited_df.iterrows()
                    }
                    corrected_values[CALCULATED_FIELD] = candidate_lookup[
                        CALCULATED_FIELD
                    ]["raw_value"]
                    changed_fields = [
                        name for name in corrected_values
                        if name != CALCULATED_FIELD
                        and corrected_values[name] != str(candidate_lookup[name]["raw_value"]).strip()
                    ]
                    if not changed_fields:
                        st.error(
                            "No values changed. Use 'Values are correct' to confirm the extraction."
                        )
                    elif any(not corrected_values[name] for name in changed_fields):
                        st.error("Corrected values cannot be empty.")
                    elif not correction_note.strip():
                        st.error("Please explain what was corrected.")
                    elif not confirmed:
                        st.error("Please confirm that you checked the corrections.")
                    else:
                        if review_status == "pending":
                            try:
                                review_revision(
                                    document_id, "changes_requested", correction_note.strip()
                                )
                            except Exception as error:
                                st.error(f"Could not begin corrections: {error}")
                                st.stop()
                        try:
                            result = resubmit_revision(
                                document_id, corrected_values, correction_note.strip()
                            )
                        except Exception as error:
                            st.error(f"Could not submit corrections: {error}")
                            if review_status == "pending":
                                st.warning(
                                    "This report may now be Changes Requested. "
                                    "Refresh and retry corrections if necessary."
                                )
                        else:
                            finish_action(
                                f"Revision {result} created as Pending. "
                                f"{len(changed_fields)} field(s) updated."
                            )

        if review_status == "pending":
            with st.expander("Other review decisions"):
                st.caption("Use when the report cannot be approved or corrected.")
                with st.form(f"enercal_other_decisions_{document_id}_{revision_no}"):
                    decision = st.selectbox(
                        "Decision", ["Require further verification", "Final rejection"]
                    )
                    reason = st.text_area("Reason (required)")
                    submitted = st.form_submit_button("Record decision")
                    if submitted:
                        if not reason.strip():
                            st.error("A reason is required.")
                        else:
                            target = (
                                "further_verification_required"
                                if decision == "Require further verification" else "rejected"
                            )
                            try:
                                result = review_revision(document_id, target, reason.strip())
                            except Exception as error:
                                st.error(f"Could not record decision: {error}")
                            else:
                                finish_action(
                                    f"Revision {result}: {STATUS_LABELS[target]}. "
                                    "Decision recorded in History."
                                )
    elif review_status == "approved":
        st.success("Approved as EnerCal estimates, not measured energy savings.")
    elif review_status == "rejected":
        st.error("This report was finally rejected. Its PDF and history remain stored.")
    else:
        st.error(f"Unknown review status: {review_status}")


# History: preserve all recorded revisions and review events.
with history_tab:
    st.subheader("Revision history")
    revision_rows = [{
        "Revision": row["revision_no"],
        "Status": STATUS_LABELS.get(row["review_status"], row["review_status"]),
        "Field Count": row["field_count"],
        "Review Note": row["review_note"] or "",
        "Reviewed At": str(row["reviewed_at"] or ""),
    } for row in details["history"]]
    if revision_rows:
        st.dataframe(revision_rows, hide_index=True, width="stretch")
    else:
        st.info("No revisions found.")

    st.subheader("Audit event history")
    event_rows = [{
        "Revision": row["revision_no"],
        "Action": row["event_type"].replace("_", " ").title(),
        "Previous Status": STATUS_LABELS.get(
            row["previous_status"], row["previous_status"] or ""
        ),
        "New Status": STATUS_LABELS.get(
            row["new_status"], row["new_status"] or ""
        ),
        "Explanation": row["reason"] or "",
        "Date": str(row["created_at"]),
    } for row in details["events"]]
    if event_rows:
        st.dataframe(event_rows, hide_index=True, width="stretch")
    else:
        st.info("No review events recorded yet.")
    st.caption(
        "Review events include decisions and explanations. Reviewer identity "
        "requires authentication buvefore production deployment."
    )
