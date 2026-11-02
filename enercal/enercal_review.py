import altair as alt
import pandas as pd
import streamlit as st

from services.enercal_review_repository import (
    CALCULATED_FIELD,
    EXPECTED_FIELDS,
    FIELD_LABELS,
    get_review_details,
    get_review_documents,
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
    "changes_requested": "Corrections are requested. Update any incorrect values or submit a clarification if the values are already correct.",
    "further_verification_required": "Additional checks are required. Submit supporting clarification, or correct any values that changed.",
    "approved": "The extracted EnerCal values have been approved for reporting as simulation estimates.",
    "rejected": "This extraction was finally rejected. Its original PDF and review history remain preserved.",
}


def finish_action(message):
    """Show the result after Streamlit refreshes the latest database state."""
    st.session_state["enercal_review_message"] = message
    st.rerun()


def display_number(value):
    """Readable numbers without hiding meaningful decimal places."""
    if value is None:
        return "N/A"
    return f"{value:,.3f}".rstrip("0").rstrip(".")


def display_percent(value):
    """Avoid displaying redundant zeros such as 55.390% or 55.000%."""
    if value is None:
        return "N/A"
    text = str(value).strip().rstrip("%")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text}%"


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

# Page-level summary, visible regardless of the selected tab.
# Use normal text for case reference and review status so long labels
# such as "Further Verification Required" are never truncated by st.metric.
st.subheader("Case overview")
summary_cols = st.columns([1.2, 0.7, 1.5], gap="large")
with summary_cols[0]:
    st.caption("MODERNISATION CASE")
    st.markdown(f"### {selected_case}")
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
        "Source: stored EnerCal PDF. These values are simulated estimates, "
        "not verified before-and-after meter measurements."
    )


# =====================================================
# 4. PDF AND EXTRACTED DATA TAB
# =====================================================

with document_tab:
    st.subheader("Original source document")
    st.write(f"**File:** {details['filename']}")
    st.write(f"**Document ID:** {document_id}")
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
        if included_field(name)
    ]
    st.dataframe(field_rows, hide_index=True, width="stretch")
    st.caption(
        f"Showing {len(field_rows)} of {len(EXPECTED_FIELDS)} fields "
        f"from revision {revision_no}."
    )


# =====================================================
# 5. ENGINEERING REVIEW TAB
# =====================================================

with review_tab:
    st.subheader("Engineering review")
    st.write(
        "Review the original PDF in the **PDF & Data** tab before "
        "submitting a decision. All actions are recorded in the audit history."
    )

    if review_status == "pending":
        with st.form(key=f"decision_{document_id}_{revision_no}"):
            decision_options = {
                "Approve Extraction": "approved",
                "Request Changes": "changes_requested",
                "Require Further Verification": "further_verification_required",
                "Final Reject": "rejected",
            }
            selected_decision = st.radio(
                "Engineering decision",
                options=list(decision_options),
                help="Final Reject closes the current extraction. Use Request Changes for correctable errors.",
            )
            review_note = st.text_area(
                "Decision explanation",
                placeholder="Explain the reason for corrections, verification, or final rejection.",
            )
            confirmed = st.checkbox(
                "I have checked the 16 extracted fields against the original PDF."
            )
            submitted = st.form_submit_button(
                "Save Engineering Decision", type="primary"
            )

            if submitted:
                decision = decision_options[selected_decision]
                if not confirmed:
                    st.error("Confirm the PDF check before proceeding.")
                elif decision != "approved" and not review_note.strip():
                    st.error("Enter a reason for this decision.")
                else:
                    try:
                        result = review_revision(document_id, decision, review_note)
                    except Exception as error:
                        st.error(f"Unable to save decision: {error}")
                    else:
                        finish_action(
                            f"Revision {result}: {STATUS_LABELS[decision]}. "
                            "The decision was saved to review history."
                        )

    elif review_status in ("changes_requested", "further_verification_required"):
        # This wizard is scoped to the PDF and revision. Selecting another
        # document or creating a new revision starts at Step 1 automatically.
        wizard_key = f"enercal_response_step_{document_id}_{revision_no}"
        # Keep the choice independent of the temporary radio widget.
        # Streamlit removes widget state when its widget disappears in Step 2.
        choice_key = f"enercal_response_choice_{document_id}_{revision_no}"
        radio_key = f"enercal_response_radio_{document_id}_{revision_no}"
        current_step = st.session_state.get(wizard_key, 1)

        if current_step == 1:
            st.markdown("#### Step 1 of 2 · Check the extracted values")
            st.write(
                "Compare the extraction with the original EnerCal PDF in "
                "the **PDF & Data** tab, then choose what needs to happen. "
                "No database changes are made at this step."
            )
            response_options = [
                "Confirm values are correct",
                "Change extracted values",
            ]
            previous_choice = st.session_state.get(choice_key)
            choice_index = (
                response_options.index(previous_choice)
                if previous_choice in response_options else None
            )
            selected_response = st.radio(
                "Are the extracted values correct?",
                options=response_options,
                index=choice_index,
                key=radio_key,
                help=(
                    "Confirm opens a clarification form. Change opens "
                    "the field correction editor. Neither option approves the report."
                ),
            )
            st.caption(
                "**Confirm** means the values need no correction and you "
                "will explain your checks. **Change** means at least one "
                "extracted value needs correcting."
            )
            if st.button(
                "Next: Continue to details",
                type="primary",
                key=f"enercal_response_next_{document_id}_{revision_no}",
            ):
                if not selected_response:
                    st.warning("Choose Confirm or Change before continuing.")
                else:
                    # This state is NOT linked to a disappearing Streamlit widget.
                    st.session_state[choice_key] = selected_response
                    st.session_state[wizard_key] = 2
                    st.rerun()

        else:
            selection = st.session_state.get(choice_key)
            if selection not in (
                "Confirm values are correct",
                "Change extracted values",
            ):
                # Handle an incomplete/cleared widget state safely.
                st.session_state[wizard_key] = 1
                st.rerun()

            st.markdown("#### Step 2 of 2 · Submit your response")
            if st.button(
                "← Back to Confirm or Change",
                key=f"enercal_response_back_{document_id}_{revision_no}",
            ):
                st.session_state[wizard_key] = 1
                st.rerun()

            if selection == "Confirm values are correct":
                st.info(
                    "The values will remain unchanged. Submit a documented "
                    "clarification to return this **same revision** to Pending "
                    "for a new engineering decision. This does not approve it."
                )
                with st.form(key=f"clarification_{document_id}_{revision_no}"):
                    explanation = st.text_area(
                        "Verification explanation",
                        placeholder=(
                            "What was checked against the PDF, and why "
                            "are the existing values correct?"
                        ),
                    )
                    confirmed = st.checkbox(
                        "I confirm the extracted values do not require correction."
                    )
                    submitted = st.form_submit_button(
                        "Submit Clarification", type="primary"
                    )
                    if submitted:
                        if not confirmed:
                            st.error("Confirm that no values need to change.")
                        elif not explanation.strip():
                            st.error("A verification explanation is required.")
                        else:
                            try:
                                result = submit_verification(document_id, explanation)
                            except Exception as error:
                                st.error(f"Unable to submit clarification: {error}")
                            else:
                                finish_action(
                                    f"Revision {result} returned to Pending. "
                                    "No extra extraction fields were created."
                                )

            else:
                st.info(
                    "Edit only the **Corrected Value** column for the 15 "
                    "source fields. Calculated Saving is the 16th field and "
                    "will be recalculated by Python. A new revision is "
                    "created only if a value really changes."
                )
                editor_rows = [
                    {
                        "Field Name": name,
                        "Field": label,
                        "Original Value": candidate_lookup[name]["raw_value"],
                        "Corrected Value": candidate_lookup[name]["raw_value"],
                    }
                    for name, label in FIELD_LABELS.items()
                    if name != CALCULATED_FIELD
                ]
                editor_df = pd.DataFrame(editor_rows)

                with st.form(key=f"correction_{document_id}_{revision_no}"):
                    corrected_df = st.data_editor(
                        editor_df,
                        hide_index=True,
                        num_rows="fixed",
                        width="stretch",
                        disabled=[
                            "Field Name",
                            "Field",
                            "Original Value",
                        ],
                        column_config={
                            "Field Name": None,
                            "Corrected Value": st.column_config.TextColumn(
                                "Corrected Value", help="Enter a corrected value."
                            ),
                        },
                        key=f"enercal_editor_{document_id}_{revision_no}",
                    )
                    correction_note = st.text_area(
                        "Correction explanation",
                        placeholder="Which field did you correct and why?",
                    )
                    confirmed = st.checkbox(
                        "I have checked the corrected values against the original PDF."
                    )
                    submitted = st.form_submit_button(
                        f"Submit as Revision {revision_no + 1}", type="primary"
                    )

                    if submitted:
                        corrected_values = {
                            row["Field Name"]: str(row["Corrected Value"])
                            for _, row in corrected_df.iterrows()
                        }
                        # Backend recalculates this field; it is never editable.
                        corrected_values[CALCULATED_FIELD] = candidate_lookup[
                            CALCULATED_FIELD
                        ]["raw_value"]
                        if not confirmed:
                            st.error("Confirm the corrections have been checked.")
                        elif not correction_note.strip():
                            st.error("A correction explanation is required.")
                        else:
                            try:
                                result = resubmit_revision(
                                    document_id, corrected_values, correction_note
                                )
                            except Exception as error:
                                st.error(f"Unable to submit corrections: {error}")
                            else:
                                finish_action(
                                    f"Revision {result} created as Pending. "
                                    f"Revision {revision_no} remains in history."
                                )

    elif review_status == "approved":
        st.success(
            f"Revision {revision_no} is approved. It is eligible for approved "
            "EnerCal estimate reporting, not measured-savings verification."
        )
    elif review_status == "rejected":
        st.error(
            f"Revision {revision_no} was finally rejected. "
            "Review history and the PDF are preserved."
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
