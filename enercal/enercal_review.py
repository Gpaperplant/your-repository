"""Unified MOD and ISO case workspace, using the five-table architecture."""

from decimal import Decimal, InvalidOperation
from contextlib import nullcontext

import pandas as pd
import pymupdf
import streamlit as st

from services.unified_review_actions import (
    CALCULATED_FIELD, FIELD_LABELS,
    submit_comparison_review,
    submit_mod_correction,
)
from services.unified_case_workspace_repository import (
    get_document_file,
    get_revision_snapshot,
    list_case_documents,
    list_document_revisions,
    list_workspace_cases,
    mod_comparison_from_snapshot,
)


STATUS_LABELS = {
    "pending": "Pending review",
    "approved": "Approved",
    "rejected": "Rejected",
    "changes_requested": "Changes requested",
    "further_verification_required": "Further verification required",
}

DOCUMENT_LABELS = {
    "mod_pdf": "EnerCal MOD PDF",
    "iso_excel": "ISO Excel workbook",
    "iso_test_pdf": "ISO test PDF",
    "meter_csv": "Meter CSV",
}


def _format(value):
    if value is None:
        return "N/A"
    if isinstance(value, (Decimal, float, int)):
        number = Decimal(str(value))
        if not number.is_finite():
            return "N/A"
        result = format(number, ",f")
        return result.rstrip("0").rstrip(".") if "." in result else result
    return str(value)


def _value(item):
    """Display both full value rows and MOD comparison adapter rows.

    Engineering snapshots use ``raw_source_value``; comparison snapshots use
    ``raw_value``. Missing numeric/text values must not cause a KeyError.
    """
    value = item.get("numeric_value")
    if value is not None:
        return value
    value = item.get("text_value")
    if value is not None:
        return value
    raw = item.get("raw_source_value")
    return raw if raw is not None else item.get("raw_value")


def _comparison(snapshot):
    try:
        comparison = mod_comparison_from_snapshot(snapshot)
    except ValueError as error:
        st.warning(f"Comparison unavailable: {error}")
        return None
    table = []
    for label, name in (
        ("Elevator technology", "elevator_technology"),
        ("Annual energy", "annual_energy_consumption"),
        ("Regenerative energy", "annual_regenerative_energy"),
        ("Annual electricity cost", "annual_electricity_expenditure"),
        ("Lifetime electricity cost", "lifetime_electricity_expenditure"),
        ("Operational CO₂e", "operational_co2e"),
        ("Embodied CO₂e", "embodied_co2e"),
    ):
        before = comparison[f"existing_{name}"]
        after = comparison[f"proposed_{name}"]
        table.append({
            "Measure": label,
            "Existing": _format(_value(before)),
            "Proposed": _format(_value(after)),
            "Unit": before["unit"] or after["unit"] or "",
        })
    st.dataframe(table, hide_index=True, width="stretch")
    return comparison



# Presentation helpers: values and provenance remain in the unified repository.
MOD_PAGE_LABELS = {
    2: "General information", 3: "Energy performance",
    4: "Energy comparison", 5: "Energy breakdown",
    6: "Electricity expenditure", 7: "Carbon emissions",
    8: "Engineering inputs",
}
MOD_SPEC_LABELS = {
    "p5_existing_lighting_kwh": "Existing car lighting",
    "p5_existing_non_running_kwh": "Existing energy while stationary",
    "p5_existing_running_kwh": "Existing running energy",
    "p5_proposed_lighting_kwh": "Proposed car lighting",
    "p5_proposed_non_running_kwh": "Proposed energy while stationary",
    "p5_proposed_running_kwh": "Proposed running energy",
    "p5_energy_without_regen_kwh": "Energy without regeneration",
    "p5_regen_saving_percent": "Regenerative energy saving",
}
ISO_ENERGY_FIELDS = {
    "annual_energy_kwh", "annual_energy_wh", "efficiency_class",
    "daily_running_energy_wh", "daily_nonrunning_energy_wh",
    "average_cycle_energy_wh", "reference_energy_wh",
    "energy_per_m_wh", "daily_trips_actual", "idle_power_w",
}


def _field_label(value):
    """Human-readable label without losing the exact database field key."""
    return MOD_SPEC_LABELS.get(value, value.replace("_", " ").replace("co2e", "CO₂e").capitalize())


def _iso_energy(values, phase):
    """Use saved ISO kWh, or convert Wh; never interpret missing as zero."""
    selected = {(v["phase"], v["field_name"]): v for v in values}
    for field, divisor in (("annual_energy_kwh", Decimal(1)),
                           ("annual_energy_wh", Decimal(1000))):
        entry = selected.get((phase, field))
        if entry is None:
            continue
        try:
            value = _value(entry)
            if value is None:
                continue
            number = Decimal(str(value).replace(",", "")) / divisor
            if number.is_finite() and number >= 0:
                return number
        except (InvalidOperation, TypeError, ValueError, ArithmeticError):
            pass
    return None


def _iso_overview(snapshot):
    values = snapshot["values"]
    before = _iso_energy(values, "existing")
    after = _iso_energy(values, "proposed")
    saving = ((before - after) / before * 100
              if before is not None and before > 0 and after is not None else None)
    st.subheader("ISO Excel energy assessment")
    a, b, c = st.columns(3)
    a.metric("Before annual energy", f"{before:,.2f} kWh/year" if before is not None else "N/A")
    b.metric("After annual energy", f"{after:,.2f} kWh/year" if after is not None else "N/A")
    c.metric("Estimated ISO saving", f"{saving:,.2f}%" if saving is not None else "N/A")
    classes = {(v["phase"], v["field_name"]): _value(v)
               for v in values if v["field_name"] == "efficiency_class"}
    before_class = classes.get(("existing", "efficiency_class"))
    after_class = classes.get(("proposed", "efficiency_class"))
    if before_class is not None or after_class is not None:
        st.caption(f"Energy class: {before_class or 'N/A'} → {after_class or 'N/A'}")
    st.caption("ISO figures are workbook estimates, not verified meter savings.")


def _iso_friendly_values(snapshot):
    """Compact, source-located values; hide formulas in the audit expander."""
    values = snapshot["values"]
    st.subheader("Extracted ISO values")
    st.caption(f"{len(values)} fields saved. Choose a section to inspect the workbook results.")
    section = st.selectbox(
        "Information section", ["Energy performance", "Engineering inputs", "All information"],
        key=f"iso_info_{snapshot['revision']['revision_id']}",
    )
    if section == "Energy performance":
        visible = [v for v in values if v["field_name"] in ISO_ENERGY_FIELDS]
        if not visible:  # Support smaller workbooks containing only input fields.
            visible = values
    elif section == "Engineering inputs":
        visible = [v for v in values if v["field_name"] not in ISO_ENERGY_FIELDS]
    else:
        visible = values
    rows = []
    for v in sorted(visible, key=lambda item: (
            {"existing": 0, "proposed": 1, "common": 2}.get(item["phase"], 3), item["field_name"])):
        rows.append({
            "Configuration": {"existing": "Before", "proposed": "After", "common": "Common"}.get(v["phase"], v["phase"]),
            "Field": _field_label(v["field_name"]),
            "Value": _format(_value(v)),
            "Unit": v.get("unit") or "",
            "Excel sheet": v.get("excel_sheet") or "",
            "Excel cell": v.get("excel_cell") or "",
        })
    st.dataframe(rows, hide_index=True, width="stretch",
                 height=min(510, max(180, 35 * (len(rows) + 1))))
    st.caption(f"Showing {len(rows)} of {len(values)} saved values.")


def _mod_specifications(snapshot):
    """Re-create the old grouped engineering-information table."""
    excluded = {"reported_energy_saving_percent", "calculated_energy_saving_percent"}
    specs = [v for v in snapshot["values"] if v.get("source_page") in {2, 5, 8}
             and v["field_name"] not in excluded]
    if not specs:
        st.write("No additional engineering information was extracted.")
        return
    groups = {"Report information": 2, "Energy breakdown": 5,
              "Engineering inputs": 8, "All information": None}
    selected = st.selectbox("Information section", list(groups),
                            key=f"mod_spec_group_{snapshot['revision']['revision_id']}")
    page = groups[selected]
    rows = [{
        "Field": _field_label(v["field_name"]),
        "Configuration": {"existing": "Existing", "proposed": "Proposed", "common": "Common"}.get(v["phase"], v["phase"]),
        "Value": _format(_value(v)),
        "Unit": v.get("unit") or "",
        "Source page": v.get("source_page"),
    } for v in specs if page is None or v["source_page"] == page]
    st.dataframe(rows, hide_index=True, width="stretch",
                 height=min(510, max(160, 35 * (len(rows) + 1))))
    st.caption(f"Showing {len(rows)} extracted engineering fields.")


def _mod_overview(snapshot):
    """Restore the older MOD performance chart and KPI-first experience."""
    try:
        comparison = mod_comparison_from_snapshot(snapshot)
    except ValueError as error:
        st.warning(f"Comparison unavailable: {error}")
        return
    before = comparison["existing_annual_energy_consumption"].get("numeric_value")
    after = comparison["proposed_annual_energy_consumption"].get("numeric_value")
    reported = comparison["reported_energy_saving_percent"].get("numeric_value")
    st.subheader("Annual energy performance")
    if before is not None and after is not None:
        chart = pd.DataFrame({"Elevator": ["Existing", "Proposed"],
                              "Annual energy": [float(before), float(after)]})
        chart_col, values_col = st.columns([3, 2], gap="large")
        with chart_col:
            st.bar_chart(chart, x="Elevator", y="Annual energy", horizontal=True,
                         sort=False, height=245, width="stretch")
        with values_col:
            st.metric("Existing energy", f"{_format(before)} kWh/year")
            st.metric("Proposed energy", f"{_format(after)} kWh/year")
    else:
        st.write("Annual energy data is unavailable.")
    st.caption("Energy values are engineering estimates, not metered measurements.")
    st.write(f"EnerCal reported saving **{_format(reported)}%**"
             if reported is not None else "EnerCal reported saving **N/A**")
    st.subheader("Existing and proposed comparison")
    _comparison(snapshot)
    if snapshot["revision"]["review_status"] == "approved":
        st.caption("Comparison approved. Full engineering source verification is separate.")


def _summary(snapshot):
    revision = snapshot["revision"]
    st.caption(
        f"{DOCUMENT_LABELS.get(revision['document_type'], revision['document_type'])} · "
        f"Revision {revision['revision_no']} · {revision['source_classification']}"
    )
    a, b, c = st.columns(3)
    a.metric("Engineering status", STATUS_LABELS.get(revision["review_status"], revision["review_status"]))
    if revision["document_type"] == "mod_pdf":
        try:
            comparison = mod_comparison_from_snapshot(snapshot)
            saving = comparison["calculated_energy_saving_percent"]["numeric_value"]
            before = comparison["existing_annual_energy_consumption"]["numeric_value"]
            after = comparison["proposed_annual_energy_consumption"]["numeric_value"]
            reduction = before - after if before is not None and after is not None else None
        except ValueError:
            saving = reduction = None
        b.metric("Estimated energy saving", f"{_format(saving)}%" if saving is not None else "N/A")
        c.metric("Annual energy reduction", f"{_format(reduction)} kWh/year" if reduction is not None else "N/A")
    else:
        b.metric("Engineering values", len(snapshot["values"]))
        c.metric("Source-verified fields", f"{sum(v['source_verified'] for v in snapshot['values'])}/{len(snapshot['values'])}")
    verified = sum(bool(v.get("source_verified")) for v in snapshot["values"])
    st.caption(
        f"Review scope: {revision['review_scope'].replace('_', ' ').title()} · "
        f"Source-verified fields: {verified}/{len(snapshot['values'])}"
    )
    if revision["source_classification"] != "real":
        st.warning("This document is not classified as real engineering evidence.")
    st.caption("Comparison approval is not full engineering source verification.")


def _show_values(snapshot):
    values = snapshot["values"]
    if not values:
        st.write("This revision has no engineering values.")
        return
    phase = st.selectbox("Engineering phase", ["All", "existing", "proposed", "common"])
    visible = [v for v in values if phase == "All" or v["phase"] == phase]
    st.dataframe(
        [{
            "Phase": v["phase"],
            "Field": v["field_name"],
            "Value": _format(_value(v)),
            "Original PDF/Excel text": v["raw_source_value"] or "",
            "Unit": v["unit"] or "",
            "Source page": v["source_page"],
            "Excel sheet": v["excel_sheet"] or "",
            "Excel cell": v["excel_cell"] or "",
            "Formula": v["excel_formula"] or "",
            "Source verified": v["source_verified"],
            "Corrected": v["is_corrected"],
            "Correction reason": v["correction_reason"] or "",
        } for v in visible],
        hide_index=True, width="stretch", height=min(640, max(200, 33 * (len(visible) + 1))),
    )


@st.cache_data(show_spinner=False, max_entries=20)
def _pdf_preview(pdf_bytes, page_number):
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if not 1 <= page_number <= len(pdf):
            raise ValueError("PDF page is out of range.")
        pixmap = pdf[page_number - 1].get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4), alpha=False)
        return pixmap.tobytes("png")


def _show_source(document):
    source = get_document_file(document["document_id"])
    file_type = document["document_type"]
    st.download_button(
        "Download source PDF" if file_type == "mod_pdf" else "Download ISO Excel"
        if file_type == "iso_excel" else "Download original document",
        data=source["file_content"],
        file_name=source["filename"], mime=source["mime_type"],
        key=f"unified_source_{document['document_id']}",
    )
    if file_type == "mod_pdf":
        with st.expander("Preview a source page"):
            if st.checkbox("Preview MOD PDF page 4", key=f"preview_{document['document_id']}"):
                page_number = st.selectbox(
                    "Choose PDF page", list(range(1, 13)), index=3,
                    format_func=lambda number: (
                        f"Page {number} · {MOD_PAGE_LABELS.get(number, '')}"
                    ).strip(),
                    key=f"pdf_page_{document['document_id']}",
                )
                preview_size = st.selectbox(
                    "Preview size", ["Compact", "Standard", "Large"], index=1,
                    key=f"pdf_size_{document['document_id']}",
                )
                try:
                    st.image(_pdf_preview(source["file_content"], page_number),
                             width={"Compact": 560, "Standard": 760, "Large": 960}[preview_size])
                    st.caption(f"PDF page {page_number} of 12")
                except Exception as error:
                    st.error(f"Could not render the PDF: {error}")


def _edit_mod(document, snapshot):
    comparison = mod_comparison_from_snapshot(snapshot)
    editable = []
    for name, label in FIELD_LABELS.items():
        if name == CALCULATED_FIELD:
            continue
        item = comparison[name]
        editable.append({
            "Field key": name,
            "Parameter": label,
            "Original source": item["raw_value"] or "",
            "Current value": _format(_value(item)),
            "Corrected value": _format(_value(item)),
        })

    with st.form(f"unified_correct_{snapshot['revision']['revision_id']}"):
        edited = st.data_editor(
            pd.DataFrame(editable),
            num_rows="fixed", hide_index=True, width="stretch", height=460,
            disabled=["Field key", "Parameter", "Original source", "Current value"],
        )
        reason = st.text_area("Reason for corrections")
        checked = st.checkbox("I checked the source PDF and documented any intentional test changes")
        submitted = st.form_submit_button("Create corrected revision")
    if not submitted:
        return
    if not checked:
        st.error("Review the source PDF before submitting corrections.")
        return
    corrected = {row["Field key"]: str(row["Corrected value"]) for row in edited.to_dict("records")}
    corrected[CALCULATED_FIELD] = str(_value(comparison[CALCULATED_FIELD]))
    try:
        result = submit_mod_correction(document["document_id"], corrected, reason)
    except Exception as error:
        st.error(f"Correction could not be saved: {error}")
    else:
        st.session_state["unified_review_notice"] = (
            f"Created pending revision {result['revision_no']}. The original revision was preserved."
        )
        st.rerun()


def _review_controls(document, snapshot, is_latest):
    revision = snapshot["revision"]
    if not is_latest:
        st.info("Historical revisions are read-only.")
        return
    if revision["document_type"] not in {"mod_pdf", "iso_excel"}:
        st.info("This document type is currently read-only in the workspace.")
        return
    if revision["review_status"] == "pending":
        st.subheader("Engineering comparison review")
        st.caption("Review the original source. Approval here does not mark engineering values source-verified.")
        with st.form(f"unified_review_{revision['revision_id']}"):
            decision = st.selectbox("Decision", ["Choose decision", "Approve comparison", "Reject", "Request changes"])
            note = st.text_area("Review reason or note")
            checked = st.checkbox("I checked the comparison against the original source")
            submitted = st.form_submit_button("Submit review", type="primary")
        if submitted:
            if not checked:
                st.error("Confirm that you checked the original source.")
            elif decision == "Choose decision":
                st.error("Choose a review decision.")
            else:
                status = {
                    "Approve comparison": "approved", "Reject": "rejected", "Request changes": "changes_requested"
                }[decision]
                try:
                    result = submit_comparison_review(document["document_id"], status, note)
                except Exception as error:
                    st.error(f"Review could not be saved: {error}")
                else:
                    st.session_state["unified_review_notice"] = (
                        f"Revision {result['revision_no']} {status.replace('_', ' ')}."
                    )
                    st.rerun()
    else:
        st.info("This revision cannot receive another review decision.")

    if revision["document_type"] == "mod_pdf" and revision["review_status"] in {
        "pending", "changes_requested", "further_verification_required"
    }:
        if st.checkbox("Correct MOD comparison values", key=f"edit_{revision['revision_id']}"):
            try:
                _edit_mod(document, snapshot)
            except ValueError as error:
                st.error(f"Correction unavailable: {error}")


def _history(revisions, snapshot):
    st.subheader("Revision history")
    st.dataframe(
        [{
            "Revision": r["revision_no"], "Scope": r["review_scope"],
            "Status": r["review_status"], "Values": r["value_count"],
            "Verified": r["verified_count"], "Corrected": r["corrected_count"],
            "Events": r["review_event_count"], "Created": r["created_at"],
        } for r in sorted(revisions, key=lambda r: r["revision_no"])],
        hide_index=True, width="stretch",
    )
    st.subheader("Selected revision's review events")
    if snapshot["reviews"]:
        st.dataframe(
            [{
                "Action": item["action"], "Previous status": item["previous_status"] or "",
                "New status": item["new_status"], "Reason": item["reason"] or "",
                "Date": item["created_at"],
            } for item in snapshot["reviews"]],
            hide_index=True, width="stretch",
        )
    else:
        st.write("No review events recorded for this revision.")


def main():
    if st.session_state.get("role") != "Admin":
        st.error("Admin access is required")
        st.stop()
        return

    st.subheader("Case Workspace")
    st.caption("Unified MOD and ISO documents, engineering snapshots and review history")
    notice = st.session_state.pop("unified_review_notice", None)
    if notice:
        st.success(notice)

    try:
        cases = list_workspace_cases()
        if not cases:
            st.info("No documents have been saved in the unified database yet.")
            return
        refs = [item["case_ref"] for item in cases]
        case_ref = st.selectbox("Modernisation case", refs)
        documents = list_case_documents(case_ref)
        if not documents:
            st.info("This case has no unified evidence.")
            return
        by_id = {doc["document_id"]: doc for doc in documents}
        document_id = st.selectbox(
            "Evidence document", list(by_id),
            format_func=lambda key: (
                f"{DOCUMENT_LABELS.get(by_id[key]['document_type'], by_id[key]['document_type'])} "
                f"· {by_id[key]['filename']} · ID {key}"
            ),
        )
        document = by_id[document_id]
        revisions = list_document_revisions(document_id)
        if not revisions:
            st.warning("The selected document has no engineering revision.")
            return
        by_revision = {r["revision_id"]: r for r in revisions}
        revision_id = st.selectbox(
            "Revision", list(by_revision),
            format_func=lambda key: (
                f"Revision {by_revision[key]['revision_no']} · "
                f"{STATUS_LABELS.get(by_revision[key]['review_status'], by_revision[key]['review_status'])}"
            ),
        )
        snapshot = get_revision_snapshot(revision_id)
        if snapshot is None:
            st.error("Selected engineering revision not found.")
            return
    except Exception as error:
        st.error(f"Could not load unified workspace: {error}")
        return

    _summary(snapshot)
    source_label = ("PDF and data" if document["document_type"] == "mod_pdf"
                    else "ISO Excel data" if document["document_type"] == "iso_excel"
                    else "Source and values")
    overview_tab, source_tab, review_tab, history_tab = st.tabs(
        ["Overview", source_label, "Engineering review", "History"]
    )
    with overview_tab:
        if document["document_type"] == "mod_pdf":
            _mod_overview(snapshot)
        elif document["document_type"] == "iso_excel":
            _iso_overview(snapshot)
        else:
            st.write(f"{len(snapshot['values'])} engineering values saved in this revision.")
    with source_tab:
        try:
            if document["document_type"] == "mod_pdf":
                st.subheader("Source PDF")
            elif document["document_type"] == "iso_excel":
                st.subheader("Saved ISO Excel calculations")
            _show_source(document)
        except Exception as error:
            st.error(f"Could not load source document: {error}")
        if document["document_type"] == "mod_pdf":
            st.subheader("Existing and proposed comparison")
            _comparison(snapshot)
            with st.expander("Profile and engineering information"):
                _mod_specifications(snapshot)
        elif document["document_type"] == "iso_excel":
            _iso_friendly_values(snapshot)
        else:
            st.caption("Document-specific summary is not available.")
        # Preserve all original Excel formulas, source text and audit flags, but
        # keep the 12-column raw database viewer out of the main review screen.
        with (st.expander("Advanced: full engineering values and source provenance")
              if hasattr(st, "expander") else nullcontext()):
            _show_values(snapshot)
    with review_tab:
        latest_doc_id = max(
            item["document_id"] for item in documents
            if item["document_type"] == document["document_type"]
        )
        is_latest = (
            revision_id == revisions[0]["revision_id"]
            and document_id == latest_doc_id
        )
        _review_controls(document, snapshot, is_latest)
    with history_tab:
        _history(revisions, snapshot)


main()
