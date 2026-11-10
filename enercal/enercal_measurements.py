
"""Upload and review independent elevator energy measurements."""

from io import BytesIO

import pymupdf
import streamlit as st
from openpyxl import load_workbook

from services.energy_measurement_extractor import MAX_SIZE, extract_measurements
from services.energy_measurement_repository import (
    find_existing_measurement,
    get_measurement_details,
    get_measurement_elevators,
    get_measurement_sources,
    link_approved_measurement,
    review_measurement_source,
    save_measurement_source,
)


STATUS = {
    "pending": "Pending review",
    "approved": "Approved",
    "rejected": "Rejected",
}

KINDS = {
    "engineering_input": "Engineering input",
    "test_measurement": "Test measurement",
    "iso_estimate": "ISO estimate",
    "enercal_estimate": "EnerCal estimate",
    "unverified_entry": "Unverified entry",
    "meter_measurement": "Meter measurement",
}


@st.cache_data(show_spinner=False, max_entries=8)
def extract_preview(file_bytes, filename):
    # Avoid repeated extraction when Streamlit reruns
    return extract_measurements(file_bytes, filename)


@st.cache_data(show_spinner=False, max_entries=20)
def render_test_page(file_bytes, page_number):
    # Render only the selected PDF page
    with pymupdf.open(stream=file_bytes, filetype="pdf") as pdf:
        return pdf[page_number - 1].get_pixmap(
            matrix=pymupdf.Matrix(1.5, 1.5),
            alpha=False,
        ).tobytes("png")


def observation_rows(observations):
    return [
        {
            "Measurement": item["metric_name"].replace("_", " ").capitalize(),
            "Value": item["raw_value"],
            "Unit": item["unit"] or "",
            "Type": KINDS.get(item["value_kind"], item["value_kind"]),
            "Source": item["source_location"],
        }
        for item in observations
    ]


def show_observations(observations, key):
    groups = ["All values", *KINDS.values()]
    selected = st.selectbox("Show information", groups, key=key)

    filtered = [
        item for item in observations
        if selected == "All values"
        or KINDS.get(item["value_kind"]) == selected
    ]

    st.dataframe(
        observation_rows(filtered),
        hide_index=True,
        width="stretch",
        height=min(520, max(130, 37 * (len(filtered) + 1))),
    )

    st.caption(
        f"Showing {len(filtered)} of {len(observations)} extracted values"
    )


def show_original(source):
    # Open the original evidence only when requested
    with st.expander("Inspect original source"):
        if not st.checkbox(
            "Show original document",
            key=f"measure_show_{source['source_id']}",
        ):
            return

        file_bytes = bytes(source["file_bytes"])

        if source["source_type"] == "iso_test_pdf":
            with pymupdf.open(stream=file_bytes, filetype="pdf") as pdf:
                page_count = len(pdf)

            page_number = st.selectbox(
                "PDF page",
                range(1, page_count + 1),
                index=min(4, page_count - 1),
            )

            _, center, _ = st.columns([1, 5, 1])

            with center:
                st.image(
                    render_test_page(file_bytes, page_number),
                    width="stretch",
                )
            return

        if source["source_type"] == "iso_excel":
            workbook = load_workbook(
                BytesIO(file_bytes),
                read_only=True,
                data_only=True,
                keep_links=False,
            )

            try:
                sheets = workbook.sheetnames
                selected = st.selectbox(
                    "Worksheet",
                    sheets,
                    index=(
                        sheets.index("Data measured")
                        if "Data measured" in sheets
                        else 0
                    ),
                )

                values = workbook[selected].iter_rows(
                    min_row=1,
                    max_row=35,
                    max_col=16,
                    values_only=True,
                )

                rows = [
                    {
                        "Row": number,
                        **{
                            chr(65 + column): value
                            for column, value in enumerate(row)
                        },
                    }
                    for number, row in enumerate(values, 1)
                    if any(value is not None for value in row)
                ]

            finally:
                workbook.close()

            st.dataframe(
                rows,
                hide_index=True,
                width="stretch",
                height=420,
            )

            st.caption("Worksheet preview shows stored cell values")


def notify(message):
    st.session_state["measurement_notice"] = message
    st.rerun()


if st.session_state.get("role") != "Admin":
    st.error("Admin access is required")
    st.stop()


st.subheader("Energy measurements")
st.caption(
    "Upload ISO test evidence and verify measurements "
    "separately from EnerCal estimates"
)

notice = st.session_state.pop("measurement_notice", None)

if notice:
    st.success(notice)


upload_tab, review_tab = st.tabs([
    "Upload source",
    "Review measurements",
])


with upload_tab:
    uploaded = st.file_uploader(
        "Choose an ISO Excel workbook or test PDF",
        type=["xlsx", "pdf"],
    )

    if uploaded is not None:
        file_bytes = uploaded.getvalue()

        if not file_bytes or len(file_bytes) > MAX_SIZE:
            st.error("Choose a nonempty file no larger than 15 MB")

        else:
            try:
                duplicate = find_existing_measurement(file_bytes)

                extracted = (
                    None if duplicate
                    else extract_preview(file_bytes, uploaded.name)
                )

            except Exception as error:
                st.error(
                    f"Could not read measurement source  {error}"
                )

            else:
                if duplicate:
                    st.info(
                        f"This file is already saved as source {duplicate}"
                    )

                else:
                    st.caption(
                        f"Elevator reference  "
                        f"{extracted['reported_elevator_ref']}"
                    )

                    st.caption(
                        f"Extracted values  "
                        f"{len(extracted['observations'])}"
                    )

                    st.info(
                        "Measured test results and calculated annual "
                        "estimates are different types of evidence"
                    )

                    show_observations(
                        extracted["observations"],
                        "measurement_upload_group",
                    )

                    if st.button(
                        "Save measurement source",
                        type="primary",
                    ):
                        try:
                            result = save_measurement_source(
                                uploaded.name,
                                file_bytes,
                                extracted=extracted,
                            )

                        except Exception as error:
                            st.error(
                                f"Could not save measurement source  {error}"
                            )

                        else:
                            notify(
                                f"Source {result['source_id']} "
                                "saved for engineering review"
                            )


with review_tab:
    try:
        sources = get_measurement_sources()

    except Exception as error:
        st.error(f"Could not load measurement sources  {error}")
        sources = []

    if not sources:
        st.info("No measurement sources have been saved")

    else:
        lookup = {
            item["source_id"]: item
            for item in sources
        }

        source_id = st.selectbox(
            "Choose a source",
            list(lookup),
            format_func=lambda key: (
                f"Source {key}    "
                f"{lookup[key]['reported_elevator_ref']}    "
                f"{STATUS.get(lookup[key]['review_status'], lookup[key]['review_status'])}"
            ),
        )

        try:
            details = get_measurement_details(source_id)

        except Exception as error:
            st.error(
                f"Could not open measurement source  {error}"
            )

        else:
            source = details["source"]
            observations = details["observations"]
            status = source["review_status"]

            st.caption(f"Source file  {source['filename']}")

            first, second, third = st.columns(3)

            first.caption("Review status")
            first.markdown(f"**{STATUS.get(status, status)}**")

            second.metric(
                "Elevator reference",
                source["reported_elevator_ref"] or "Unknown",
            )

            third.metric(
                "Extracted values",
                len(observations),
            )

            if source["verified_elevator_ref"]:
                st.success(
                    f"Linked to elevator {source['verified_elevator_ref']}"
                )
            else:
                st.info("No verified elevator link")

            st.download_button(
                "Download original source",
                data=bytes(source["file_bytes"]),
                file_name=source["filename"],
                mime=(
                    "application/pdf"
                    if source["source_type"] == "iso_test_pdf"
                    else
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                ),
                key=f"measure_download_{source_id}",
            )

            show_original(source)

            st.subheader("Extracted information")

            show_observations(
                observations,
                f"measurement_review_group_{source_id}",
            )

            st.caption(
                "Source values are read only and cannot "
                "be changed during review"
            )

            if (
                status in {"pending", "approved"}
                and source["verified_elevator_id"] is None
            ):
                try:
                    elevators = get_measurement_elevators()

                except Exception as error:
                    st.error(
                        f"Could not load registered elevators  {error}"
                    )
                    elevators = []

                matches = [
                    item for item in elevators
                    if item["elevator_ref"].strip().upper()
                    == (
                        source["reported_elevator_ref"] or ""
                    ).strip().upper()
                ]

                elevator_lookup = {
                    item["elevator_id"]: item
                    for item in matches
                }

            else:
                elevator_lookup = {}

            if status == "pending":
                st.subheader("Engineering verification")

                st.caption(
                    "Approve the extracted source values "
                    "or reject the source"
                )

                with st.form(f"measure_review_{source_id}"):
                    decision = st.radio(
                        "Review action",
                        ["Approve", "Reject"],
                        horizontal=True,
                    )

                    elevator_id = st.selectbox(
                        "Confirmed elevator link optional",
                        [None, *elevator_lookup],
                        format_func=lambda key: (
                            "Leave unlinked"
                            if key is None
                            else elevator_lookup[key]["elevator_ref"]
                        ),
                    )

                    note = st.text_area(
                        "Review note",
                        height=80,
                    )

                    confirmed = st.checkbox(
                        "I checked the values against the original source"
                    )

                    submitted = st.form_submit_button(
                        "Save review",
                        type="primary",
                    )

                if submitted:
                    if not confirmed:
                        st.error(
                            "Confirm the source verification before saving"
                        )

                    elif decision == "Reject" and not note.strip():
                        st.error(
                            "Provide a reason for rejecting the source"
                        )

                    else:
                        try:
                            review_measurement_source(
                                source_id,
                                (
                                    "approved"
                                    if decision == "Approve"
                                    else "rejected"
                                ),
                                note,
                                (
                                    elevator_id
                                    if decision == "Approve"
                                    else None
                                ),
                            )

                        except Exception as error:
                            st.error(
                                f"Could not save review  {error}"
                            )

                        else:
                            notify("Measurement source review saved")

            elif (
                status == "approved"
                and source["verified_elevator_id"] is None
            ):
                st.subheader("Link verified elevator")

                if not elevator_lookup:
                    st.info(
                        "Register the matching elevator before "
                        "creating a verified link"
                    )

                else:
                    with st.form(f"measure_link_{source_id}"):
                        elevator_id = st.selectbox(
                            "Matching elevator",
                            list(elevator_lookup),
                            format_func=lambda key: (
                                elevator_lookup[key]["elevator_ref"]
                            ),
                        )

                        confirmed = st.checkbox(
                            "I confirmed the elevator identity"
                        )

                        submitted = st.form_submit_button(
                            "Link elevator",
                            type="primary",
                        )

                    if submitted:
                        if not confirmed:
                            st.error("Confirm the elevator identity")

                        else:
                            try:
                                link_approved_measurement(
                                    source_id,
                                    elevator_id,
                                )

                            except Exception as error:
                                st.error(
                                    f"Could not link elevator  {error}"
                                )

                            else:
                                notify(
                                    "Elevator linked to approved "
                                    "measurement source"
                                )

            elif status == "rejected":
                st.warning(
                    "This source was rejected and cannot be linked"
                )

            if source["review_note"]:
                st.caption(
                    f"Latest review note  {source['review_note']}"
                )
