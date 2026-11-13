import streamlit as st

from database import get_connection
from services.measurement_pair_repository import (
    create_measurement_pair,
    list_measurement_candidates,
    list_measurement_pair_revisions,
    review_measurement_pair,
)


def _read_rows(query, params=()):
    # Use the existing PostgreSQL connection for read-only page queries.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            columns = [item.name for item in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def _completed_cases():
    return _read_rows(
        """
        SELECT m.case_id, m.case_ref, m.elevator_id, e.elevator_ref
        FROM modernisation_cases m
        JOIN elevators e ON e.elevator_id = m.elevator_id
        WHERE m.status = 'completed'
        ORDER BY m.case_ref
        """
    )


def _all_cases():
    """Show every registered project without changing its eligibility."""
    return _read_rows(
        """
        SELECT
            m.case_id,
            m.case_ref,
            m.status,
            m.elevator_id,
            e.elevator_ref,
            COUNT(d.document_id) AS document_count
        FROM modernisation_cases m
        LEFT JOIN elevators e ON e.elevator_id = m.elevator_id
        LEFT JOIN source_documents d ON d.case_id = m.case_id
        GROUP BY
            m.case_id, m.case_ref, m.status, m.elevator_id, e.elevator_ref
        ORDER BY m.case_ref
        """
    )


def _show_case_overview(all_cases, completed_cases):
    """Read-only status overview; does not grant approval permissions."""
    st.subheader("Modernisation cases")

    first, second, third = st.columns(3)
    first.metric("Total cases", len(all_cases))
    completed_count = sum(row["status"] == "completed" for row in all_cases)
    second.metric("Completed", completed_count)
    third.metric("Not completed", len(all_cases) - completed_count)

    if not all_cases:
        st.info("No modernisation cases have been registered yet.")
        return

    completed_ids = {row["case_id"] for row in completed_cases}
    st.dataframe(
        [
            {
                "Case": row["case_ref"],
                "Project status": row["status"],
                "Elevator": row["elevator_ref"] or "Not linked",
                "Uploaded EnerCal PDFs": row["document_count"],
                "Measurement pair review": (
                    "Eligible for evidence checks"
                    if row["case_id"] in completed_ids
                    else (
                        "Link elevator before review"
                        if row["status"] == "completed"
                        else "Locked until completed"
                    )
                ),
            }
            for row in all_cases
        ],
        hide_index=True,
        use_container_width=True,
    )
    st.caption(
        "All cases appear for tracking. Only completed cases with a linked "
        "elevator can continue to measurement-pair submission and review. "
        "Completing a project does not automatically verify meter evidence."
    )


def _approved_documents(case_id):
    return _read_rows(
        """
        SELECT DISTINCT d.document_id, d.filename
        FROM source_documents d
        JOIN enercal_approved_comparison_view v
            ON v.document_id = d.document_id
        WHERE d.case_id = %s
        ORDER BY d.document_id DESC
        """,
        (case_id,),
    )


def _existing_pair(case_id):
    # Select the latest revision for this case.
    rows = _read_rows(
        """
        SELECT
            p.pair_id, p.revision_no, p.document_id,
            p.review_status, p.submission_note, p.review_note,
            p.before_observation_id, p.after_observation_id,
            p.before_days, p.after_days,
            p.conditions_verified, p.period_energy_verified,
            p.specifications_verified,
            b.source_id AS before_source_id,
            b.metric_name AS before_metric,
            b.numeric_value AS before_kwh,
            b.period_start AS before_start,
            b.period_end AS before_end,
            a.source_id AS after_source_id,
            a.metric_name AS after_metric,
            a.numeric_value AS after_kwh,
            a.period_start AS after_start,
            a.period_end AS after_end
        FROM verified_measurement_pairs p
        JOIN energy_observations b
            ON b.observation_id = p.before_observation_id
        JOIN energy_observations a
            ON a.observation_id = p.after_observation_id
        WHERE p.case_id = %s
        ORDER BY p.revision_no DESC
        LIMIT 1
        """,
        (case_id,),
    )
    return rows[0] if rows else None


def _show_history(case_id):
    # Display earlier submissions without modifying historical records.
    revisions = list_measurement_pair_revisions(case_id)

    if not revisions:
        return

    with st.expander(f"Revision history ({len(revisions)})"):
        st.dataframe(
            [
                {
                    "Revision": row["revision_no"],
                    "Pair ID": row["pair_id"],
                    "Status": row["review_status"],
                    "EnerCal document": row["document_id"],
                    "Before observation": row["before_observation_id"],
                    "After observation": row["after_observation_id"],
                    "Submission note": row["submission_note"],
                    "Review note": row["review_note"],
                    "Submitted": row["created_at"],
                    "Reviewed": row["reviewed_at"],
                }
                for row in revisions
            ],
            hide_index=True,
            use_container_width=True,
        )


def _observation_label(row):
    return (
        f"#{row['observation_id']} | source {row['source_id']} | "
        f"{row['metric_name']} | {row['numeric_value']} {row['unit']} | "
        f"{row['period_start']} to {row['period_end']}"
    )


def _review_existing_pair(pair):
    st.subheader(
        f"Measurement pair #{pair['pair_id']} | Revision {pair['revision_no']}"
    )
    st.write(f"Status: **{pair['review_status'].replace('_', ' ').title()}**")

    st.table([
        {
            "Period": period.title(),
            "Observation ID": pair[f"{period}_observation_id"],
            "Source ID": pair[f"{period}_source_id"],
            "Metric": pair[f"{period}_metric"],
            "Energy (kWh)": pair[f"{period}_kwh"],
            "Start": pair[f"{period}_start"],
            "End": pair[f"{period}_end"],
            "Days": pair[f"{period}_days"],
        }
        for period in ("before", "after")
    ])

    if pair["submission_note"]:
        st.write(f"Submission note: {pair['submission_note']}")

    if pair["review_note"]:
        st.write(f"Engineering review note: {pair['review_note']}")

    if pair["review_status"] == "approved":
        st.success(
            "This pair is approved. The AI loader will "
            "still apply eligibility checks."
        )
        return

    if pair["review_status"] == "rejected":
        st.warning(
            "This revision was rejected. You can submit a new "
            "measurement pair below without deleting the old revision."
        )
        return

    st.warning(
        "Approval must be based on source evidence. An ISO reference-cycle "
        "reading or calculated annual estimate is not a measured period total."
    )

    with st.form(f"review_pair_{pair['pair_id']}"):
        period_verified = st.checkbox(
            "I checked that both values are actual metered totals "
            "for the complete periods shown."
        )

        conditions_verified = st.checkbox(
            "I verified the same elevator, measurement boundary, "
            "modernisation order and comparable operating conditions."
        )

        specifications_verified = st.checkbox(
            "I separately verified the EnerCal engineering specifications "
            "against the original PDF (optional)."
        )

        note = st.text_area("Engineering review explanation")

        approve = st.form_submit_button("Approve pair", type="primary")
        reject = st.form_submit_button("Reject pair")

    if not (approve or reject):
        return

    try:
        decision = "approved" if approve else "rejected"

        review_measurement_pair(
            pair["pair_id"],
            decision,
            note,
            conditions_verified=conditions_verified,
            period_energy_verified=period_verified,
            specifications_verified=specifications_verified,
        )

        st.session_state["pair_message"] = (
            f"Pair #{pair['pair_id']} {decision}."
        )
        st.rerun()

    except (ValueError, RuntimeError) as exc:
        st.error(str(exc))


def _create_pair(case, documents):
    st.subheader("Propose before-and-after measurements")

    observations = list_measurement_candidates(case["elevator_id"])

    if len({row["source_id"] for row in observations}) < 2:
        st.info(
            "At least two distinct approved measurement sources "
            "with metered kWh period totals are required. "
            "Pending ISO source files are excluded."
        )
        return

    st.dataframe(
        observations,
        hide_index=True,
        use_container_width=True,
    )

    with st.form(f"create_measurement_pair_{case['case_id']}"):
        document = st.selectbox(
            "Approved EnerCal document",
            documents,
            format_func=lambda row: (
                f"#{row['document_id']} - {row['filename']}"
            ),
        )

        before = st.selectbox(
            "Before modernisation observation",
            observations,
            format_func=_observation_label,
        )

        after = st.selectbox(
            "After modernisation observation",
            observations,
            format_func=_observation_label,
        )

        note = st.text_area("Why these periods are comparable")

        confirm = st.checkbox(
            "I checked the original files and am submitting "
            "this pair for review, not automatic approval."
        )

        submitted = st.form_submit_button(
            "Submit pending pair",
            type="primary",
        )

    if not submitted:
        return

    if not confirm:
        st.error("Check the source confirmation before submitting.")
        return

    try:
        pair_id = create_measurement_pair(
            case["case_id"],
            document["document_id"],
            before["observation_id"],
            after["observation_id"],
            note,
        )

        st.session_state["pair_message"] = (
            f"Pair #{pair_id} saved as pending."
        )
        st.rerun()

    except (ValueError, RuntimeError) as exc:
        st.error(str(exc))


def main():
    st.title("EnerCal | Measurement Pair Review")
    st.caption(
        "Link genuine period meter measurements "
        "to completed modernisation cases."
    )

    # Prototype role selection is not verified authentication.
    if st.session_state.get("role") != "Admin":
        st.error(
            "This prototype page is restricted to "
            "the Admin navigation role."
        )
        st.stop()

    st.info(
        "This role selector is not secure authentication. "
        "Engineering approvals require verified reviewer "
        "identity before production use."
    )

    if message := st.session_state.pop("pair_message", None):
        st.success(message)

    try:
        # Show every project, even if it is not yet eligible for meter review.
        all_cases = _all_cases()
        completed_cases = _completed_cases()
        _show_case_overview(all_cases, completed_cases)

        if not all_cases:
            return

        selected = st.selectbox(
            "Modernisation case",
            all_cases,
            format_func=lambda row: (
                f"{row['case_ref']} | "
                f"{str(row['status']).replace('_', ' ').title()}"
            ),
            key="measurement_pair_case_selector",
        )

        # The original completed-case query remains the eligibility gate.
        # Do not display approval forms for registered or ongoing projects.
        completed_by_id = {
            row["case_id"]: row for row in completed_cases
        }
        case = completed_by_id.get(selected["case_id"])

        if case is None:
            if selected["status"] == "completed":
                st.warning(
                    "This project is marked completed but has no valid linked "
                    "elevator. Link and verify its elevator record before "
                    "submitting measurement pairs."
                )
            else:
                st.info(
                    f"{selected['case_ref']} is currently "
                    f"'{selected['status']}'. Measurement-pair submission "
                    "and engineering approval stay locked until the actual "
                    "modernisation is completed. An approved EnerCal PDF "
                    "alone is not proof of project completion."
                )
            return

        pair = _existing_pair(case["case_id"])

        if pair:
            _review_existing_pair(pair)
            _show_history(case["case_id"])

            # An approved or pending pair blocks another submission.
            if pair["review_status"] != "rejected":
                return

        documents = _approved_documents(case["case_id"])

        if not documents:
            st.info("This case has no approved EnerCal document.")
            return

        _create_pair(case, documents)

    except Exception as exc:
        st.error(f"Could not load measurement review data: {exc}")


main()
