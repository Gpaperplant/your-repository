
"""Display and review extracted data from the 12 page EnerCal MOD report."""

from datetime import date
from decimal import Decimal

import pandas as pd
import pymupdf
import streamlit as st

from services.enercal_review_repository import (
    CALCULATED_FIELD,
    EDITABLE_STATUSES,
    EXPECTED_FIELDS,
    FIELD_LABELS,
    get_review_details,
    get_review_documents,
    get_specifications,
    resubmit_revision,
    review_revision,
    submit_verification,
)


STATUS_LABELS = {
    "pending": "Pending review",
    "changes_requested": "Changes requested",
    "further_verification_required": "Further verification required",
    "approved": "Approved",
    "rejected": "Rejected",
}

PAGE_LABELS = {
    2: "General information",
    3: "Energy performance",
    4: "Energy comparison",
    5: "Energy breakdown",
    6: "Electricity expenditure",
    7: "Carbon emissions",
    8: "Engineering inputs",
}

SPEC_LABELS = {
    "P5 existing lighting kwh": "Existing car lighting",
    "P5 existing non running kwh": "Existing energy while stationary",
    "P5 existing running kwh": "Existing running energy",
    "P5 proposed lighting kwh": "Proposed car lighting",
    "P5 proposed non running kwh": "Proposed energy while stationary",
    "P5 proposed running kwh": "Proposed running energy",
    "P5 energy without regen kwh": "Energy without regeneration",
    "P5 regen saving percent": "Regenerative energy saving",
}

MEASURES = (
    ("Elevator technology", "elevator_technology", ""),
    ("Annual energy", "annual_energy_consumption", "kWh/year"),
    ("Regenerative energy", "annual_regenerative_energy", "kWh/year"),
    ("Annual electricity cost", "annual_electricity_expenditure", None),
    ("Lifetime electricity cost", "lifetime_electricity_expenditure", None),
    ("Operational CO₂e", "operational_co2e", None),
    ("Embodied CO₂e", "embodied_co2e", "kg CO₂e"),
)


@st.cache_data(show_spinner=False, max_entries=24)
def render_page(pdf_bytes, page_number):
    """Render one PDF page only after a preview is requested."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if len(pdf) != 12 or not 1 <= page_number <= 12:
            raise ValueError("Expected a page from a 12 page MOD report")
        image = pdf[page_number - 1].get_pixmap(
            matrix=pymupdf.Matrix(1.6, 1.6), alpha=False
        )
        return image.tobytes("png")


def format_value(value):
    if value is None:
        return "N/A"
    if isinstance(value, (Decimal, int, float)):
        result = format(Decimal(str(value)), ",f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    return str(value)


def notify(message):
    st.session_state["enercal_review_notice"] = message
    st.rerun()


def comparison_table(candidates):
    # Keep the source units unchanged when presenting both technologies.
    rows = []
    for label, field, unit in MEASURES:
        existing = candidates[f"existing_{field}"]
        proposed = candidates[f"proposed_{field}"]
        rows.append({
            "Measure": label,
            "Existing": format_value(
                existing["numeric_value"] if existing["numeric_value"] is not None
                else existing["raw_value"]
            ),
            "Proposed": format_value(
                proposed["numeric_value"] if proposed["numeric_value"] is not None
                else proposed["raw_value"]
            ),
            "Unit": unit if unit is not None else existing["unit"] or "",
        })
    return rows


def show_specifications(specifications, document_id):
    if not specifications:
        st.info("No additional information was extracted")
        return

    groups = {
        "Report information": 2,
        "Energy breakdown": 5,
        "Engineering inputs": 8,
        "All information": None,
    }

    selected = st.selectbox(
        "Information section",
        list(groups),
        key=f"enercal_spec_group_{document_id}",
    )

    page = groups[selected]
    rows = []

    for item in specifications:
        if page is not None and item["Source page"] != page:
            continue

        name = item["Field"]
        value = item["Value"]

        if name == "Report date":
            value = date.fromisoformat(value).strftime("%d %B %Y").lstrip("0")

        row = {
            "Field": SPEC_LABELS.get(name, name),
            "Value": value,
            "Unit": item["Unit"],
        }

        if page is None:
            row["Source page"] = item["Source page"]

        rows.append(row)

    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        height=min(500, max(130, 38 * (len(rows) + 1))),
        column_config={
            "Field": st.column_config.TextColumn("Field", width="large"),
            "Value": st.column_config.TextColumn("Value", width="medium"),
            "Unit": st.column_config.TextColumn("Unit", width="small"),
            "Source page": st.column_config.NumberColumn(
                "Source page",
                width="small",
            ),
        },
    )

    st.caption(f"Showing {len(rows)} extracted fields")


def review_forms(document_id, revision_no, status, candidates):
    # Engineers edit source fields while the calculated saving stays read only.
    if status not in EDITABLE_STATUSES:
        st.info("This revision is read only and its decisions remain in History")
        return

    choices = (
        ["Approve", "Correct values", "Other decision"]
        if status == "pending"
        else ["Confirm unchanged", "Correct values"]
    )

    action = st.radio(
        "Choose a review action",
        choices,
        horizontal=True,
        key=f"enercal_action_{document_id}_{revision_no}",
    )

    if action in {"Approve", "Confirm unchanged"}:
        with st.form(f"enercal_confirm_{document_id}_{revision_no}"):
            note = st.text_input(
                "Review note optional"
                if action == "Approve"
                else "Reason for confirmation"
            )

            checked = st.checkbox(
                "I checked the comparison values against the source PDF"
            )

            submitted = st.form_submit_button(
                "Approve report"
                if action == "Approve"
                else "Confirm values",
                type="primary",
            )

        if submitted:
            if not checked:
                st.error("Confirm that you checked the source PDF")

            elif action == "Confirm unchanged" and not note.strip():
                st.error("Explain why the values need no correction")

            else:
                try:
                    if action == "Approve":
                        review_revision(
                            document_id,
                            "approved",
                            note.strip(),
                        )
                        message = f"Revision {revision_no} approved"

                    else:
                        submit_verification(
                            document_id,
                            note.strip(),
                        )
                        message = (
                            f"Revision {revision_no} returned to pending review"
                        )

                except Exception as error:
                    st.error(f"Review could not be saved {error}")

                else:
                    notify(message)

        return

    if action == "Correct values":
        editable = pd.DataFrame([
            {
                "Field key": key,
                "Field": label,
                "Source value": candidates[key]["raw_value"],
                "Corrected value": candidates[key]["raw_value"],
            }
            for key, label in FIELD_LABELS.items()
            if key != CALCULATED_FIELD
        ])

        st.caption(
            "Update only the incorrect values and explain each correction"
        )

        with st.form(f"enercal_edit_{document_id}_{revision_no}"):
            updated = st.data_editor(
                editable,
                hide_index=True,
                num_rows="fixed",
                width="stretch",
                height=480,
                disabled=[
                    "Field key",
                    "Field",
                    "Source value",
                ],
                column_config={
                    "Field key": None,
                    "Field": st.column_config.TextColumn(
                        "Field",
                        width="large",
                    ),
                    "Source value": st.column_config.TextColumn(
                        "Source value"
                    ),
                    "Corrected value": st.column_config.TextColumn(
                        "Corrected value"
                    ),
                },
                key=f"enercal_editor_{document_id}_{revision_no}",
            )

            note = st.text_area(
                "Reason for corrections",
                height=90,
            )

            checked = st.checkbox(
                "I checked the corrected values against the source PDF"
            )

            submitted = st.form_submit_button(
                "Save corrected values",
                type="primary",
            )

        if submitted:
            corrected = {
                row["Field key"]: str(row["Corrected value"]).strip()
                for row in updated.to_dict("records")
            }

            corrected[CALCULATED_FIELD] = (
                candidates[CALCULATED_FIELD]["raw_value"]
            )

            if not note.strip():
                st.error("Explain which values were corrected")

            elif not checked:
                st.error("Confirm that you checked the corrections")

            else:
                try:
                    new_revision = resubmit_revision(
                        document_id,
                        corrected,
                        note.strip(),
                    )

                except Exception as error:
                    st.error(f"Corrections could not be saved {error}")

                else:
                    notify(f"Revision {new_revision} created for review")

        return

    with st.form(f"enercal_decision_{document_id}_{revision_no}"):
        decisions = {
            "changes_requested": "Request changes",
            "further_verification_required": "Require further verification",
            "rejected": "Reject report",
        }

        decision = st.selectbox(
            "Decision",
            list(decisions),
            format_func=decisions.get,
        )

        reason = st.text_area(
            "Reason for decision",
            height=90,
        )

        submitted = st.form_submit_button("Save decision")

    if submitted:
        if not reason.strip():
            st.error("Explain this decision")

        else:
            try:
                review_revision(
                    document_id,
                    decision,
                    reason.strip(),
                )

            except Exception as error:
                st.error(f"Decision could not be saved {error}")

            else:
                notify(f"Revision {revision_no} updated")


# Only administrators can access review decisions and PDF source files.
if st.session_state.get("role") != "Admin":
    st.error("Admin access is required")
    st.stop()


st.subheader("EnerCal review")
st.caption("Check report information and approve or correct extracted values")

message = st.session_state.pop(
    "enercal_review_notice",
    None,
)

if message:
    st.success(message)


try:
    documents = get_review_documents()

except Exception as error:
    st.error(f"Could not load reports {error}")
    st.stop()


if not documents:
    st.info("No MOD reports have been uploaded")
    st.stop()


report_lookup = {
    row[0]: row
    for row in documents
}

document_id = st.selectbox(
    "Choose a MOD report",
    list(report_lookup),
    format_func=lambda key: (
        f"Report {key}    "
        f"Case {str(report_lookup[key][1]).replace('-', ' ')}"
    ),
)


try:
    details = get_review_details(document_id)
    specifications = get_specifications(document_id)

except Exception as error:
    st.error(f"Could not load report information {error}")
    st.stop()


candidate_rows = details["candidates"]

candidates = {
    row["field_name"]: row
    for row in candidate_rows
}

if len(candidate_rows) != 16 or set(candidates) != EXPECTED_FIELDS:
    st.error(
        "This revision does not contain the required 16 comparison values"
    )
    st.stop()


revision_no = details["revision_no"]
status = details["status"]

existing_energy = candidates[
    "existing_annual_energy_consumption"
]["numeric_value"]

proposed_energy = candidates[
    "proposed_annual_energy_consumption"
]["numeric_value"]

reported = candidates[
    "reported_energy_saving_percent"
]["numeric_value"]

calculated = candidates[
    CALCULATED_FIELD
]["numeric_value"]

reduction = (
    existing_energy - proposed_energy
    if existing_energy is not None
    and proposed_energy is not None
    else None
)


st.caption(f"Report {document_id}    Revision {revision_no}")

with st.container(border=True):
    status_col, saving_col, reduction_col = st.columns(
        [1.4, 1, 1],
        gap="medium",
    )

    with status_col:
        st.caption("Engineering status")
        st.markdown(
            f"**{STATUS_LABELS.get(status, status)}**"
        )
        st.caption(f"Revision {revision_no}")

    with saving_col:
        st.metric(
            "Energy saving",
            f"{format_value(calculated)}%",
        )

    with reduction_col:
        st.metric(
            "Annual energy reduction",
            f"{format_value(reduction)} kWh",
        )


st.caption(
    "Energy results are estimates and are not verified meter measurements"
)

overview_tab, data_tab, history_tab = st.tabs(
    ["Overview", "PDF and data", "History"]
)


with overview_tab:
    st.subheader("Annual energy performance")

    if existing_energy is not None and proposed_energy is not None:
        chart = pd.DataFrame({
            "Elevator": ["Existing", "Proposed"],
            "Annual energy": [
                float(existing_energy),
                float(proposed_energy),
            ],
        })

        # Keep the graph compact with the values beside it.
        chart_col, values_col = st.columns(
            [3, 2],
            gap="large",
        )

        with chart_col:
            st.bar_chart(
                chart,
                x="Elevator",
                y="Annual energy",
                horizontal=True,
                sort=False,
                height=245,
                width="stretch",
            )

        with values_col:
            st.metric(
                "Existing energy",
                f"{format_value(existing_energy)} kWh",
            )

            st.metric(
                "Proposed energy",
                f"{format_value(proposed_energy)} kWh",
            )

        st.caption(
            "Annual electricity consumption in kWh per year"
        )

    else:
        st.info("Annual energy data is unavailable")

    st.write(
        f"EnerCal reported saving **{format_value(reported)}%**"
    )

    if status == "approved":
        st.success(
            "This revision is approved for KPI reporting"
        )

    elif status == "rejected":
        st.warning("This revision was rejected")

    else:
        st.info(
            "Open PDF and data to check values and complete the review"
        )


with data_tab:
    st.subheader("Source PDF")

    st.download_button(
        "Download source PDF",
        data=details["pdf_bytes"],
        file_name=details["filename"],
        mime="application/pdf",
        key=f"enercal_download_{document_id}",
    )

    with st.expander("Preview a source page"):
        if st.checkbox(
            "Show source page",
            key=f"enercal_show_pdf_{document_id}",
        ):
            page_col, size_col = st.columns(
                [2, 1],
                gap="medium",
            )

            with page_col:
                page_number = st.selectbox(
                    "Choose PDF page",
                    list(range(1, 13)),
                    index=3,
                    format_func=lambda number: (
                        f"Page {number}    "
                        f"{PAGE_LABELS.get(number, '')}"
                    ).strip(),
                    key=f"enercal_pdf_page_{document_id}",
                )

            with size_col:
                size = st.selectbox(
                    "Preview size",
                    ["Compact", "Standard", "Large"],
                    index=1,
                    key=f"enercal_pdf_size_{document_id}",
                )

            preview_width = {
                "Compact": 560,
                "Standard": 760,
                "Large": 960,
            }[size]

            # Center the document preview inside the page.
            _, preview_col, _ = st.columns([1, 5, 1])

            with preview_col:
                try:
                    st.image(
                        render_page(
                            details["pdf_bytes"],
                            page_number,
                        ),
                        width=preview_width,
                    )

                    st.caption(
                        f"PDF page {page_number} of 12"
                    )

                except Exception as error:
                    st.error(
                        f"Could not render the source page {error}"
                    )

    st.subheader("Existing and proposed comparison")

    st.dataframe(
        comparison_table(candidates),
        hide_index=True,
        width="stretch",
        column_config={
            "Measure": st.column_config.TextColumn(
                "Measure",
                width="large",
            ),
            "Existing": st.column_config.TextColumn(
                "Existing",
                width="medium",
            ),
            "Proposed": st.column_config.TextColumn(
                "Proposed",
                width="medium",
            ),
            "Unit": st.column_config.TextColumn(
                "Unit",
                width="medium",
            ),
        },
    )

    st.subheader("Energy saving results")

    st.dataframe(
        [
            {
                "Measure": "Reported saving",
                "Value": f"{format_value(reported)}%",
            },
            {
                "Measure": "Calculated saving",
                "Value": f"{format_value(calculated)}%",
            },
        ],
        hide_index=True,
        width="stretch",
    )

    st.caption(
        "The comparison includes all 16 extracted values"
    )

    with st.expander("Profile and engineering information"):
        show_specifications(
            specifications,
            document_id,
        )

    st.divider()

    st.subheader("Engineering review")
    st.caption(
        "Confirm the PDF values before submitting a decision"
    )

    review_forms(
        document_id,
        revision_no,
        status,
        candidates,
    )


with history_tab:
    st.subheader("Revision history")

    if details["history"]:
        st.dataframe(
            [
                {
                    "Revision": row["revision_no"],
                    "Status": STATUS_LABELS.get(
                        row["review_status"],
                        row["review_status"],
                    ),
                    "Fields": row["field_count"],
                    "Review note": row["review_note"] or "",
                    "Created": row["created_at"],
                    "Reviewed": row["reviewed_at"],
                }
                for row in details["history"]
            ],
            hide_index=True,
            width="stretch",
        )

    else:
        st.info(
            "No revision history is available"
        )

    st.subheader("Review events")

    if details["events"]:
        st.dataframe(
            [
                {
                    "Revision": row["revision_no"],
                    "Action": row["event_type"]
                        .replace("_", " ")
                        .capitalize(),
                    "Previous status": STATUS_LABELS.get(
                        row["previous_status"],
                        row["previous_status"] or "",
                    ),
                    "New status": STATUS_LABELS.get(
                        row["new_status"],
                        row["new_status"],
                    ),
                    "Reason": row["reason"] or "",
                    "Date": row["created_at"],
                }
                for row in details["events"]
            ],
            hide_index=True,
            width="stretch",
        )

    else:
        st.info(
            "No review events have been recorded"
        )
