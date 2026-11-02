import altair as alt
import pymupdf
import pandas as pd
import streamlit as st

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


# =====================================================
# 1. ACCESS AND PAGE HELPERS
# =====================================================

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
    "changes_requested": "Follow-up required. Check the PDF below, then confirm or correct the extracted values.",
    "further_verification_required": "Further checks are needed. Confirm the original values or submit corrections below.",
    "approved": "The extracted EnerCal values have been approved for reporting as calculated estimates.",
    "rejected": "This extraction was finally rejected. Its original PDF and review history remain preserved.",
}


def finish_action(message):
    """Show the result after Streamlit refreshes the latest database state."""
    st.session_state["enercal_review_message"] = message
    st.rerun()


def display_number(value):
    """Format database numbers without silently rounding meaningful digits."""
    if value is None:
        return "N/A"
    try:
        from decimal import Decimal, InvalidOperation

        number = Decimal(str(value))
        if not number.is_finite():
            return "N/A"
        return format(number, ",f").rstrip("0").rstrip(".") if "." in format(number, ",f") else format(number, ",f")
    except (InvalidOperation, ValueError, TypeError):
        return "N/A"


def display_percent(value):
    """Format percentages without discarding meaningful decimal places."""
    if value is None:
        return "N/A"
    text = str(value).strip().removesuffix("%").strip()
    return f"{display_number(text)}%" if text else "N/A"


def status_message(status):
    message = STATUS_GUIDANCE.get(status, "Review status is not recognised.")
    if status == "approved":
        st.success(message)
    elif status == "rejected":
        st.error(message)
    elif status in ("changes_requested", "further_verification_required"):
        st.warning(message)
    else:
        st.info(message)


# =====================================================
# PDF PAGE PREVIEW
# =====================================================

@st.cache_data(show_spinner=False, max_entries=24)
def pdf_page_info(pdf_bytes: bytes):
    """Find the EnerCal comparison page without assuming it is always page 4."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
        count = len(document)
        if not count:
            raise ValueError("The source PDF has no pages.")
        comparison_page = 1
        for page_index, page in enumerate(document):
            text = page.get_text("text").upper()
            if "EXISTING TECHNOLOGY COMPARISON" in text:
                comparison_page = page_index + 1
                break
    return count, comparison_page


@st.cache_data(show_spinner=False, max_entries=48)
def render_source_page(pdf_bytes: bytes, page_number: int) -> bytes:
    """Render the original PDF page as a PNG for the Streamlit viewer."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
        if not 1 <= page_number <= len(document):
            raise ValueError("The requested PDF page does not exist.")
        page = document[page_number - 1]
        pixmap = page.get_pixmap(dpi=155, alpha=False)
        return pixmap.tobytes("png")


def show_original_pdf_page(pdf_bytes: bytes, document_id, view_name: str):
    """Show a page preview from the saved PDF without external PDF services."""
    if not pdf_bytes:
        st.warning("The original PDF is unavailable for preview.")
        return

    try:
        page_count, comparison_page = pdf_page_info(pdf_bytes)
    except Exception as error:
        st.warning(f"Unable to read the original PDF: {error}")
        return

    selector_col, note_col = st.columns([1, 2], vertical_alignment="bottom")
    with selector_col:
        chosen_page = st.selectbox(
            "Original PDF page",
            options=range(1, page_count + 1),
            index=comparison_page - 1,
            key=f"enercal_pdf_page_{view_name}_{document_id}",
            format_func=lambda page: f"Page {page} of {page_count}",
        )
    with note_col:
        st.caption(
            f"Comparison found on page {comparison_page}. "
            "This is the original source PDF, not an edited revision."
        )

    try:
        image_bytes = render_source_page(pdf_bytes, chosen_page)
    except Exception as error:
        st.warning(f"Could not display this PDF page: {error}")
    else:
        st.image(
            image_bytes,
            width="stretch",
            caption=f"Original EnerCal source PDF · Page {chosen_page} of {page_count}",
        )


# =====================================================
# 2. PAGE HEADER AND DOCUMENT SELECTION
# =====================================================

st.title("EnerCal | Case Workspace")
st.caption(
    "Engineering review of estimated elevator energy performance. "
    "Approval checks extracted report data, not measured energy savings."
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

case_refs = sorted({row[1] for row in documents})
case_col, report_col = st.columns([1, 2])

with case_col:
    selected_case = st.selectbox(
        "Modernisation Case",
        options=case_refs,
        key="enercal_workspace_case",
    )

case_documents = [row for row in documents if row[1] == selected_case]
report_labels = {
    row[0]: f"{row[2]}  |  Document {row[0]}"
    for row in case_documents
}

with report_col:
    document_id = st.selectbox(
        "EnerCal PDF Report",
        options=list(report_labels),
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

if not valid_revision:
    st.error(
        "The latest revision does not contain exactly 16 unique fields "
        "from one PDF page. Review actions are disabled."
    )
    st.stop()

if set(FIELD_LABELS) != EXPECTED_FIELDS:
    st.error(
        "The review field labels do not match the expected 16 extraction fields. "
        "Check services/enercal_review_repository.py."
    )
    st.stop()

# Page-level summary, visible regardless of the selected tab.
# Use normal text for case reference and review status so long labels
# such as "Further Verification Required" are never truncated by st.metric.
st.subheader("Case overview")
summary_cols = st.columns([1.2, 0.7, 1.5], gap="large")
with summary_cols[0]:
    st.caption("MODERNISATION CASE")
    st.subheader(str(selected_case))
with summary_cols[1]:
    st.caption("CURRENT REVISION")
    st.markdown(f"### {revision_no}")
with summary_cols[2]:
    st.caption("ENGINEERING STATUS")
    st.markdown(f"**{STATUS_LABELS.get(review_status, review_status)}**")

status_message(review_status)

# Four short tabs replace the original long scrolling page.
overview_tab, document_tab, review_tab, history_tab = st.tabs(
    ["Overview", "PDF & Data", "Engineering Review", "History"]
)


# =====================================================
# 3. OVERVIEW TAB
# =====================================================

with overview_tab:
    st.subheader("Estimated energy performance")

    existing_energy = candidate_lookup[
        "existing_annual_energy_consumption"
    ]["numeric_value"]
    proposed_energy = candidate_lookup[
        "proposed_annual_energy_consumption"
    ]["numeric_value"]
    calculated_saving = candidate_lookup[CALCULATED_FIELD]["numeric_value"]
    reported_saving = candidate_lookup[
        "reported_energy_saving_percent"
    ]["numeric_value"]

    metrics = st.columns(4)
    with metrics[0]:
        st.metric("Existing (kWh/year)", display_number(existing_energy))
    with metrics[1]:
        st.metric("Proposed (kWh/year)", display_number(proposed_energy))
    with metrics[2]:
        st.metric("Calculated Saving", display_percent(calculated_saving))
    with metrics[3]:
        st.metric("EnerCal Reported", display_percent(reported_saving))

    if existing_energy is not None and proposed_energy is not None:
        # A compact horizontal chart is easier to compare and read than
        # Streamlit's default tall chart with vertical category labels.
        # Both measurements are consumption estimates, so the axis begins
        # at zero and never extends into negative energy values.
        energy_chart = pd.DataFrame(
            {
                "Elevator": ["Existing elevator", "Proposed elevator"],
                "Annual energy (kWh/year)": [
                    float(existing_energy),
                    float(proposed_energy),
                ],
            }
        )
        largest_value = max(
            float(existing_energy), float(proposed_energy), 1.0
        )
        chart_max = largest_value * 1.22  # Room for labels beside bars

        energy_bars = alt.Chart(energy_chart).mark_bar(
            size=38,
            cornerRadiusEnd=5,
        ).encode(
            y=alt.Y(
                "Elevator:N",
                sort=["Existing elevator", "Proposed elevator"],
                title=None,
                axis=alt.Axis(labelAngle=0, labelLimit=200),
            ),
            x=alt.X(
                "Annual energy (kWh/year):Q",
                scale=alt.Scale(domain=[0, chart_max]),
                axis=alt.Axis(title="Annual energy (kWh/year)", format=",.0f"),
            ),
            color=alt.Color(
                "Elevator:N",
                scale=alt.Scale(
                    domain=["Existing elevator", "Proposed elevator"],
                    range=["#7AA6C2", "#2EAAA1"],
                ),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("Elevator:N", title="Elevator"),
                alt.Tooltip(
                    "Annual energy (kWh/year):Q",
                    title="Estimated annual energy (kWh)",
                    format=",.0f",
                ),
            ],
        )
        energy_labels = alt.Chart(energy_chart).mark_text(
            align="left",
            baseline="middle",
            dx=9,
            fontSize=14,
            fontWeight="bold",
        ).encode(
            y=alt.Y(
                "Elevator:N",
                sort=["Existing elevator", "Proposed elevator"],
            ),
            x=alt.X("Annual energy (kWh/year):Q"),
            text=alt.Text("Annual energy (kWh/year):Q", format=",.0f"),
        )
        st.altair_chart(
            (energy_bars + energy_labels).properties(height=150),
            use_container_width=True,
        )
        st.caption(
            "Annual energy consumption estimated by EnerCal. "
            "A shorter bar represents lower estimated consumption."
        )

    st.subheader("Existing vs proposed elevator")
    comparison_rows = []
    for row in details["comparison"]:
        comparison_rows.append(
            {
                "Role": row["technology_role"].title(),
                "Elevator Technology": row["elevator_technology"],
                "Annual Energy (kWh/year)": display_number(
                    row["annual_energy_consumption_kwh_year"]
                ),
                "Regenerative Energy (kWh/year)": display_number(
                    row["annual_regenerative_energy_kwh_year"]
                ),
                "Annual Expenditure (BHD/year)": display_number(
                    row["annual_electricity_expenditure_bhd_year"]
                ),
                "Lifetime Expenditure (BHD)": display_number(
                    row["lifetime_electricity_expenditure_bhd"]
                ),
                "Operational CO2e (kg / 20 years)": display_number(
                    row["operational_co2e_kg_lifetime_20_years"]
                ),
                "Embodied CO2e (kg)": display_number(
                    row["embodied_co2e_kg"]
                ),
            }
        )

    if comparison_rows:
        st.dataframe(comparison_rows, hide_index=True, width="stretch")
    else:
        st.warning("No comparison preview is available for this revision.")

    st.caption(
        "Source: stored EnerCal PDF. These values are calculated report estimates, "
        "not verified before-and-after meter measurements."
    )


# =====================================================
# 4. PDF AND EXTRACTED DATA TAB
# =====================================================

with document_tab:
    st.subheader("Original source document")
    st.write("**File:**", details["filename"])
    st.write("**Document ID:**", document_id)
    st.download_button(
        "Download Original EnerCal PDF",
        data=details["pdf_bytes"],
        file_name=details["filename"],
        mime="application/pdf",
        key=f"enercal_download_{document_id}",
    )
    st.caption(
        "Compare the extracted values with the original PDF before "
        "making a review decision. The PDF is stored only once."
    )
    with st.expander("Preview the original PDF here", expanded=False):
        show_original_pdf_page(details["pdf_bytes"], document_id, "document")

    st.subheader("Extracted fields")
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

    field_rows = [
        {
            "Field": label,
            "Extracted Value": candidate_lookup[name]["raw_value"],
            "Unit": candidate_lookup[name]["unit"] or "",
        }
        for name, label in FIELD_LABELS.items()
        if name in candidate_lookup and included_field(name)
    ]
    st.dataframe(field_rows, hide_index=True, width="stretch")
    st.caption(
        f"Showing {len(field_rows)} of {len(EXPECTED_FIELDS)} fields "
        f"from revision {revision_no}."
    )

    # Display additional engineering fields without changing the approval form.
    with st.expander("Additional engineering specifications (unreviewed)"):
        extra_fields = get_specifications(document_id)
        if extra_fields:
            st.dataframe(extra_fields, hide_index=True, width="stretch")
        else:
            st.info("No additional specifications stored for this report.")


# =====================================================
# 5. ENGINEERING REVIEW TAB
# =====================================================

with review_tab:
    st.subheader("Engineering review")
    st.caption(
        "Check the source PDF and the 16 extracted values below. "
        "Your decision and any corrections will be kept in History."
    )

    with st.expander("Original EnerCal PDF · Review the source page", expanded=True):
        show_original_pdf_page(details["pdf_bytes"], document_id, "review")

    # Always show the complete current revision before asking for a decision.
    # The derived saving cannot be edited directly.
    table_rows = [
        {
            "Field": label,
            "Extracted value": str(candidate_lookup[name]["raw_value"]),
            "Unit": candidate_lookup[name]["unit"] or "—",
        }
        for name, label in FIELD_LABELS.items()
    ]
    st.markdown("#### Extracted values")
    st.caption(f"Revision {revision_no} · {len(table_rows)} fields · Read-only")
    st.dataframe(
        pd.DataFrame(table_rows),
        hide_index=True,
        width="stretch",
        height=600,
        column_config={
            "Field": st.column_config.TextColumn("Field", width="large"),
            "Extracted value": st.column_config.TextColumn(
                "Extracted value", width="medium"
            ),
            "Unit": st.column_config.TextColumn("Unit", width="medium"),
        },
    )

    if review_status in (
        "pending",
        "changes_requested",
        "further_verification_required",
    ):
        # Key includes document and revision, so switching reports cannot carry
        # an earlier decision into the next case.
        mode_key = f"enercal_review_mode_{document_id}_{revision_no}"
        if mode_key not in st.session_state:
            st.session_state[mode_key] = None

        st.markdown("#### Are the extracted values correct?")
        left, right = st.columns(2, gap="medium")
        with left:
            if st.button(
                "✓ Values are correct",
                type="primary",
                width="stretch",
                key=f"enercal_values_correct_{document_id}_{revision_no}",
            ):
                st.session_state[mode_key] = "correct"
        with right:
            if st.button(
                "✎ Values need correction",
                width="stretch",
                key=f"enercal_values_wrong_{document_id}_{revision_no}",
            ):
                st.session_state[mode_key] = "edit"

        mode = st.session_state.get(mode_key)

        if mode == "correct":
            st.divider()
            if review_status == "pending":
                st.markdown("##### Approve this extraction")
                st.caption(
                    "This approves the extracted EnerCal report values only. "
                    "It does not verify measured energy savings."
                )
            else:
                st.markdown("##### Confirm the existing values")
                st.caption(
                    "The report currently requires follow-up. Confirming its values "
                    "will return it to Pending for another engineering decision. "
                    "This confirmation does not approve the report."
                )

            with st.form(f"enercal_correct_form_{document_id}_{revision_no}"):
                if review_status == "pending":
                    explanation = st.text_input(
                        "Review note (optional)",
                        placeholder="Optional notes about your check",
                    )
                else:
                    explanation = st.text_input(
                        "Reason no correction is needed (required)",
                        placeholder="For example, I rechecked the flagged values against the original PDF.",
                        help=(
                            "A brief explanation is needed in the audit history "
                            "before this report can return to Pending."
                        ),
                    )
                confirmed = st.checkbox(
                    "I checked all 16 extracted fields against the original PDF."
                )
                button_text = (
                    "Confirm and approve extraction"
                    if review_status == "pending"
                    else "Confirm values for re-review"
                )
                submitted = st.form_submit_button(button_text, type="primary")

                if submitted:
                    if not confirmed:
                        st.error("Please confirm that the PDF values were checked.")
                    elif review_status != "pending" and not explanation.strip():
                        st.error("Briefly explain why no correction is needed before submitting.")
                    else:
                        try:
                            if review_status == "pending":
                                result = review_revision(
                                    document_id, "approved", explanation.strip()
                                )
                                message = (
                                    f"Revision {result} approved as an EnerCal "
                                    "calculated estimate."
                                )
                            else:
                                result = submit_verification(
                                    document_id, explanation.strip()
                                )
                                message = (
                                    f"Revision {result} returned to Pending for "
                                    "engineering re-review."
                                )
                        except Exception as error:
                            st.error(f"Could not save review: {error}")
                        else:
                            finish_action(message)

        elif mode == "edit":
            st.divider()
            st.markdown("##### Correct extracted values")
            st.caption(
                "Edit only the Corrected value cells that are wrong. "
                "Calculated saving is excluded because the backend recalculates it "
                "from the two annual energy values."
            )

            editor_rows = [
                {
                    "Field key": name,
                    "Field": label,
                    "Original value": str(candidate_lookup[name]["raw_value"]),
                    "Corrected value": str(candidate_lookup[name]["raw_value"]),
                }
                for name, label in FIELD_LABELS.items()
                if name != CALCULATED_FIELD
            ]
            editor_df = pd.DataFrame(editor_rows)

            with st.form(f"enercal_edit_form_{document_id}_{revision_no}"):
                edited_df = st.data_editor(
                    editor_df,
                    hide_index=True,
                    num_rows="fixed",
                    width="stretch",
                    height=580,
                    disabled=["Field key", "Field", "Original value"],
                    column_config={
                        "Field key": None,
                        "Field": st.column_config.TextColumn("Field", width="large"),
                        "Original value": st.column_config.TextColumn(
                            "Original value", width="medium"
                        ),
                        "Corrected value": st.column_config.TextColumn(
                            "Corrected value", width="medium",
                            help="Edit only values that differ from the source PDF.",
                        ),
                    },
                    key=f"enercal_editor_{document_id}_{revision_no}",
                )

                correction_note = st.text_area(
                    "Correction explanation",
                    placeholder="Describe which values were corrected and why.",
                    height=100,
                )
                confirmed = st.checkbox(
                    "I checked the corrected values against the original PDF.",
                    key=f"enercal_correction_confirm_{document_id}_{revision_no}",
                )
                submitted = st.form_submit_button(
                    "Submit corrected values", type="primary"
                )

                if submitted:
                    corrected_values = {
                        row["Field key"]: str(row["Corrected value"]).strip()
                        for _, row in edited_df.iterrows()
                    }
                    # Backend recalculates this field and validates its value.
                    corrected_values[CALCULATED_FIELD] = candidate_lookup[
                        CALCULATED_FIELD
                    ]["raw_value"]

                    changed_fields = [
                        name
                        for name in corrected_values
                        if name != CALCULATED_FIELD
                        and str(corrected_values[name]).strip()
                        != str(candidate_lookup[name]["raw_value"]).strip()
                    ]

                    if not changed_fields:
                        st.error(
                            "No values were changed. Select 'Values are correct' "
                            "to confirm the original extraction instead."
                        )
                    elif any(
                        not str(corrected_values[name]).strip()
                        for name in changed_fields
                    ):
                        st.error("Corrected values cannot be empty.")
                    elif not correction_note.strip():
                        st.error("Please explain what was corrected.")
                    elif not confirmed:
                        st.error("Please confirm that you checked the corrections.")
                    else:
                        # The existing repository supports resubmission after a
                        # changes_requested or verification-required decision.
                        # For a Pending revision, record that decision first.
                        # If resubmission fails, the document remains in
                        # changes_requested and the reviewer can retry safely.
                        if review_status == "pending":
                            try:
                                review_revision(
                                    document_id,
                                    "changes_requested",
                                    correction_note.strip(),
                                )
                            except Exception as error:
                                st.error(
                                    "Could not initiate the correction workflow: "
                                    f"{error}"
                                )
                                st.stop()

                        try:
                            result = resubmit_revision(
                                document_id,
                                corrected_values,
                                correction_note.strip(),
                            )
                        except Exception as error:
                            st.error(f"Could not submit corrections: {error}")
                            if review_status == "pending":
                                st.warning(
                                    "The report may now be marked Changes Requested. "
                                    "Refresh the page and retry the corrections."
                                )
                        else:
                            finish_action(
                                f"Revision {result} created as Pending. "
                                f"{len(changed_fields)} field(s) updated; "
                                "the previous revision remains in History."
                            )

        # These are not the main path and stay collapsed to avoid clutter.
        if review_status == "pending":
            with st.expander("Other review decisions"):
                st.caption(
                    "Use these only when the report cannot be approved or "
                    "corrected from the available evidence."
                )
                with st.form(f"enercal_other_decisions_{document_id}_{revision_no}"):
                    decision = st.selectbox(
                        "Decision",
                        ["Require further verification", "Final rejection"],
                    )
                    reason = st.text_area(
                        "Reason (required)",
                        placeholder="Explain the unresolved issue or final rejection.",
                    )
                    submitted = st.form_submit_button("Record decision")
                    if submitted:
                        if not reason.strip():
                            st.error("A reason is required.")
                        else:
                            target = (
                                "further_verification_required"
                                if decision == "Require further verification"
                                else "rejected"
                            )
                            try:
                                result = review_revision(
                                    document_id, target, reason.strip()
                                )
                            except Exception as error:
                                st.error(f"Could not record decision: {error}")
                            else:
                                finish_action(
                                    f"Revision {result}: {STATUS_LABELS[target]}. "
                                    "The decision was recorded in History."
                                )

    elif review_status == "approved":
        st.success(
            "This revision has been approved for EnerCal calculated estimate "
            "reporting. It is not a verified measured energy savings result."
        )
    elif review_status == "rejected":
        st.error(
            "This revision was finally rejected. Its source PDF and review "
            "history have been preserved."
        )
    else:
        st.error(f"Unknown review status: {review_status}")


# =====================================================
# 6. HISTORY TAB
# =====================================================

with history_tab:
    st.subheader("Revision history")
    revision_rows = [
        {
            "Revision": revision["revision_no"],
            "Status": STATUS_LABELS.get(
                revision["review_status"], revision["review_status"]
            ),
            "Field Count": revision["field_count"],
            "Review Note": revision["review_note"] or "",
            "Reviewed At": str(revision["reviewed_at"] or ""),
        }
        for revision in details["history"]
    ]
    if revision_rows:
        st.dataframe(revision_rows, hide_index=True, width="stretch")
    else:
        st.info("No revision records found.")

    st.subheader("Audit event history")
    event_rows = [
        {
            "Revision": event["revision_no"],
            "Action": event["event_type"].replace("_", " ").title(),
            "Previous Status": STATUS_LABELS.get(
                event["previous_status"], event["previous_status"] or ""
            ),
            "New Status": STATUS_LABELS.get(
                event["new_status"], event["new_status"] or ""
            ),
            "Explanation": event["reason"] or "",
            "Date": str(event["created_at"]),
        }
        for event in details["events"]
    ]
    if event_rows:
        st.dataframe(event_rows, hide_index=True, width="stretch")
    else:
        st.info("No engineering review events have been recorded yet.")

    st.caption(
        "Review events capture decisions and explanations. Reviewer identity "
        "will require proper authentication before production deployment."
    )
