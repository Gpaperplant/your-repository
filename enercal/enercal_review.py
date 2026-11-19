
"""Display and review extracted data from the 12 page EnerCal MOD report."""

from datetime import date
from decimal import Decimal

import pandas as pd
import pymupdf
import streamlit as st

from services.iso_repository import review_iso_assessment
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
        st.write("No additional information was extracted")
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

        if name == "Report date" and value:
            try:
                value = date.fromisoformat(str(value)).strftime("%d %B %Y").lstrip("0")
            except ValueError:
                pass

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

def _save_review(operation, message, error_label="Review"):
    try:
        result = operation()
    except Exception as error:
        st.error(f"{error_label} could not be saved: {error}")
    else:
        notify(message(result) if callable(message) else message)


def _confirm_review(document_id, revision_no, action):
    with st.form(f"enercal_confirm_{document_id}_{revision_no}"):
        note = st.text_input(
            "Review note optional"
            if action == "Approve" else "Reason for confirmation"
        )
        checked = st.checkbox(
            "I checked the comparison values against the source PDF"
        )
        submitted = st.form_submit_button(
            "Approve report" if action == "Approve" else "Confirm values",
            type="primary",
        )

    if not submitted:
        return
    if not checked:
        st.error("Confirm that you checked the source PDF")
        return
    if action == "Confirm unchanged" and not note.strip():
        st.error("Explain why the values need no correction")
        return

    if action == "Approve":
        _save_review(
            lambda: review_revision(document_id, "approved", note.strip()),
            f"Revision {revision_no} approved",
        )
    else:
        _save_review(
            lambda: submit_verification(document_id, note.strip()),
            f"Revision {revision_no} returned to pending review",
        )


def _correct_review(document_id, revision_no, candidates):
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

    with st.form(f"enercal_edit_{document_id}_{revision_no}"):
        updated = st.data_editor(
            editable,
            hide_index=True,
            num_rows="fixed",
            width="stretch",
            height=480,
            disabled=["Field key", "Field", "Source value"],
            column_config={
                "Field key": None,
                "Field": st.column_config.TextColumn("Field", width="large"),
                "Source value": st.column_config.TextColumn("Source value"),
                "Corrected value": st.column_config.TextColumn("Corrected value"),
            },
            key=f"enercal_editor_{document_id}_{revision_no}",
        )
        note = st.text_area("Reason for corrections", height=90)
        checked = st.checkbox(
            "I checked the corrected values against the source PDF"
        )
        submitted = st.form_submit_button(
            "Save corrected values", type="primary"
        )

    if not submitted:
        return
    if not note.strip():
        st.error("Explain which values were corrected")
        return
    if not checked:
        st.error("Confirm that you checked the corrections")
        return

    corrected = {
        row["Field key"]: str(row["Corrected value"]).strip()
        for row in updated.to_dict("records")
    }
    corrected[CALCULATED_FIELD] = candidates[CALCULATED_FIELD]["raw_value"]
    _save_review(
        lambda: resubmit_revision(document_id, corrected, note.strip()),
        lambda new_number: f"Revision {new_number} created for review",
        error_label="Corrections",
    )


def review_forms(document_id, revision_no, status, candidates):
    if status not in EDITABLE_STATUSES:
        st.write("This revision is read only and its decisions remain in History")
        return

    choices = (
        ["Approve", "Correct values"]
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
        _confirm_review(document_id, revision_no, action)
    elif action == "Correct values":
        _correct_review(document_id, revision_no, candidates)


def _iso_rows(cur):
    columns = [item.name for item in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _saved_iso_assessments(case_ref):
    from database import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.assessment_id, a.lift_id, a.imported_at,
                   a.review_status, a.reviewed_at, a.review_note
            FROM iso_assessments a
            JOIN modernisation_cases m ON m.case_id = a.case_id
            WHERE m.case_ref = %s
              AND EXISTS (
                  SELECT 1 FROM iso_documents d
                  WHERE d.assessment_id = a.assessment_id
                    AND d.document_type = 'iso_excel'
              )
            ORDER BY a.imported_at DESC, a.assessment_id DESC
            """,
            (case_ref,),
        )
        return _iso_rows(cur)


def _saved_iso_details(case_ref, assessment_id):
    from database import get_connection

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.document_id, d.filename, d.content
            FROM iso_documents d
            JOIN iso_assessments a ON a.assessment_id = d.assessment_id
            JOIN modernisation_cases m ON m.case_id = a.case_id
            WHERE a.assessment_id = %s AND m.case_ref = %s
              AND d.document_type = 'iso_excel'
            """,
            (assessment_id, case_ref),
        )
        documents = _iso_rows(cur)

        cur.execute(
            """
            SELECT v.field_name, v.value_text, v.unit,
                   v.excel_sheet, v.excel_cell
            FROM iso_extracted_values v
            JOIN iso_documents d ON d.document_id = v.document_id
            JOIN iso_assessments a ON a.assessment_id = d.assessment_id
            JOIN modernisation_cases m ON m.case_id = a.case_id
            WHERE a.assessment_id = %s AND m.case_ref = %s
              AND d.document_type = 'iso_excel'
            ORDER BY v.field_name
            """,
            (assessment_id, case_ref),
        )
        return documents, _iso_rows(cur)


def show_saved_iso(case_ref):
    st.subheader("Saved ISO Excel calculations")
    st.caption("Review original ISO workbooks and their extracted values.")

    assessments = _saved_iso_assessments(case_ref)
    if not assessments:
        st.write(f"No ISO Excel saved for {case_ref}")
        return

    lookup = {row["assessment_id"]: row for row in assessments}
    assessment_id = st.selectbox(
        "Saved assessment",
        list(lookup),
        format_func=lambda key: (
            f"Assessment {key} | {lookup[key]['lift_id']} | "
            f"{lookup[key]['imported_at']}"
        ),
        key=f"iso_assessment_{case_ref}",
    )

    documents, values = _saved_iso_details(case_ref, assessment_id)
    st.caption(f"Assessment {assessment_id} | Case {case_ref}")
    review = lookup[assessment_id]
    review_status = review["review_status"]
    st.write(
        f"ISO engineering status: **{STATUS_LABELS.get(review_status, review_status.title())}**"
    )
    if review.get("reviewed_at"):
        st.caption(f"Reviewed: {review['reviewed_at']}")
    if review.get("review_note"):
        st.write(f"Review note: {review['review_note']}")
    st.metric("ISO Excel fields", len(values))

    if documents:
        source = documents[0]
        st.caption(source["filename"])
        st.download_button(
            "Download ISO Excel",
            data=bytes(source["content"]),
            file_name=source["filename"],
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"iso_source_{assessment_id}",
        )
    else:
        st.warning("The saved ISO Excel file is unavailable.")

    st.subheader("Extracted ISO values")
    st.dataframe(
        [
            {
                "Field": item["field_name"],
                "Value": item["value_text"],
                "Unit": item["unit"] or "",
                "Excel sheet": item["excel_sheet"],
                "Excel cell": item["excel_cell"],
            }
            for item in values
        ],
        hide_index=True,
        width="stretch",
        height=min(620, max(180, 36 * (len(values) + 1))),
    )
    st.caption(f"Showing {len(values)} saved fields")

    # Only the latest pending ISO workbook can receive a new decision.
    if assessment_id != assessments[0]["assessment_id"]:
        st.caption("An older ISO assessment is read only.")
    elif review_status == "pending" and documents and values:
        st.subheader("ISO engineering review")
        st.caption("Check the original workbook and extracted values before approving.")
        decision = st.selectbox(
            "ISO review decision", ["Choose decision", "Approve", "Reject"],
            key=f"iso_decision_{assessment_id}",
        )
        note = st.text_input("ISO review note", key=f"iso_note_{assessment_id}")
        if st.button(
            "Submit ISO review", type="primary",
            disabled=decision == "Choose decision",
            key=f"iso_submit_{assessment_id}",
        ):
            try:
                review_iso_assessment(
                    case_ref, assessment_id,
                    "approved" if decision == "Approve" else "rejected", note,
                )
            except Exception as error:
                st.error(f"Could not submit ISO review: {error}")
            else:
                result = "approved" if decision == "Approve" else "rejected"
                notify(f"ISO assessment {assessment_id} {result}.")


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
    st.write(f":green[{message}]")


try:
    documents = get_review_documents()

except Exception as error:
    st.error(f"Could not load reports {error}")
    st.stop()


if not documents:
    st.write("No MOD reports have been uploaded")
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
            f"{format_value(calculated)}%" if calculated is not None else "N/A",
        )

    with reduction_col:
        st.metric(
            "Annual energy reduction",
            f"{format_value(reduction)} kWh" if reduction is not None else "N/A",
        )


st.caption(
    "Energy results are estimates and are not verified meter measurements"
)

overview_tab, data_tab, iso_tab, history_tab = st.tabs(
    ["Overview", "PDF and data", "ISO Excel data", "History"]
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
        st.write("Annual energy data is unavailable")

    st.write(
        f"EnerCal reported saving **{format_value(reported)}%**"
        if reported is not None else "EnerCal reported saving **N/A**"
    )

    if status == "approved":
        st.write(
            ":green[This revision is approved for KPI reporting]"
        )

    elif status == "rejected":
        st.warning("This revision was rejected")

    else:
        st.write(
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
            "Measure": st.column_config.TextColumn("Measure", width="large"),
            **{
                name: st.column_config.TextColumn(name, width="medium")
                for name in ("Existing", "Proposed", "Unit")
            },
        },
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


with iso_tab:
    try:
        show_saved_iso(str(report_lookup[document_id][1]))
    except Exception as error:
        st.error(f"Could not load saved ISO Excel data: {error}")

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
        st.write(
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
        st.write(
            "No review events have been recorded"
        )
