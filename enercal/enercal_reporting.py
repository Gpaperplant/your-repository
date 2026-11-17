"""EnerCal case tracking, approved KPI estimates, and verified energy savings."""

from decimal import Decimal

import pandas as pd
import streamlit as st

from database import get_connection


# Approved KPI columns are provided by migration 008_enercal_reporting_units.sql.
METRICS = (
    ("energy", "Annual energy", "annual_energy_kwh_year"),
    ("regen", "Regenerative energy", "regenerative_energy_kwh_year"),
    ("annual_cost", "Annual electricity cost", "annual_electricity_cost_per_year"),
    ("lifetime_cost", "Lifetime electricity cost", "lifetime_electricity_cost"),
    ("operational_co2e", "Operational CO₂e", "operational_co2e_kg_lifetime"),
    ("embodied_co2e", "Embodied CO₂e", "embodied_co2e_kg"),
)


def _rows(cur):
    names = [column.name for column in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def load_case_statuses():
    """All modernisation cases, including PDFs without a review and cases without PDFs."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT m.case_ref, m.status AS case_status, d.document_id,
                   d.filename, r.revision_no, r.review_status
            FROM modernisation_cases m
            LEFT JOIN source_documents d ON d.case_id = m.case_id
            LEFT JOIN LATERAL (
                SELECT revision_no, review_status
                FROM extraction_revisions
                WHERE document_id = d.document_id
                ORDER BY revision_no DESC LIMIT 1
            ) r ON TRUE
            ORDER BY m.case_ref, d.document_id DESC
        """)
        return _rows(cur)


def load_reports():
    """Only approved PDF reports with BOTH technology roles contribute to KPIs."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM enercal_approved_kpis_view ORDER BY document_id DESC")
        reports = {}
        for row in _rows(cur):
            role = row["technology_role"]
            if role not in {"existing", "proposed"}:
                continue
            document_id = row["document_id"]
            report = reports.setdefault(document_id, {
                "document_id": document_id,
                "case_ref": row["case_ref"],
                "filename": row["filename"],
                "revision_no": row["revision_no"],
                "country": row["country"],
                "report_date": row["report_date"],
                "currency_symbol": row["currency_symbol"] or "",
                "lifetime_years": row["lifetime_years"],
                "electricity_price": row["electricity_price_per_kwh"],
                "electricity_price_unit": row["electricity_price_unit"] or "",
                "reported_saving": row["reported_energy_saving_percent"],
                "calculated_saving": row["calculated_energy_saving_percent"],
            })
            report[f"{role}_technology"] = row["elevator_technology"]
            for key, _, column in METRICS:
                report[f"{role}_{key}"] = row[column]

    return [
        report for report in reports.values()
        if "existing_technology" in report and "proposed_technology" in report
    ]


def display(value):
    """Missing values are N/A, never zero."""
    if value is None or value == "":
        return "N/A"
    if isinstance(value, (Decimal, int, float)):
        number = Decimal(str(value))
        if not number.is_finite():
            return "N/A"
        formatted = format(number, ",f")
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted
    return str(value)


def difference(before, after):
    return before - after if before is not None and after is not None else None


def status_label(row, approved_ids):
    if row["document_id"] is None:
        return "No PDF uploaded"
    if row["review_status"] is None:
        return "Extraction missing"
    if row["review_status"] == "approved" and row["document_id"] not in approved_ids:
        return "Approved (KPI data unavailable)"
    return str(row["review_status"]).replace("_", " ").title()


def metric_unit(key, report):
    currency = report["currency_symbol"] or "Currency not specified"
    years = report["lifetime_years"]
    return {
        "energy": "kWh/year",
        "regen": "kWh/year",
        "annual_cost": f"{currency}/year",
        "lifetime_cost": f"{currency}/lifetime",
        "operational_co2e": f"kg CO₂e over {years} years" if years else "kg CO₂e",
        "embodied_co2e": "kg CO₂e",
    }[key]


def comparable_unit(key, reports):
    """Reject comparisons across mixed currencies or assessment lifetimes."""
    if not reports:
        return False, ""
    if key in {"annual_cost", "lifetime_cost", "operational_co2e"} and len({
        metric_unit(key, report) for report in reports
    }) != 1:
        return False, "Reports use different currencies or assessment lifetimes."
    if key in {"annual_cost", "lifetime_cost"} and any(
        not report["currency_symbol"] for report in reports
    ):
        return False, "Some reports do not specify a currency."
    if key in {"lifetime_cost", "operational_co2e"} and any(
        not report["lifetime_years"] for report in reports
    ):
        return False, "Some reports do not specify an assessment lifetime."
    if key == "lifetime_cost" and len({
        str(report["lifetime_years"]) for report in reports
    }) != 1:
        return False, "Lifetime electricity costs require equal assessment periods."
    return True, ""


def show_case_overview(statuses, reports):
    """Dropdowns filter the table only; summary cards use unfiltered data."""
    approved_ids = {r["document_id"] for r in reports}
    st.subheader("Modernisation cases and report status")
    a, b, c, d = st.columns(4)
    a.metric("Modernisation cases", len({r["case_ref"] for r in statuses}))
    b.metric("Without PDF", len({r["case_ref"] for r in statuses if r["document_id"] is None}))
    c.metric("Pending reports", sum(r["review_status"] == "pending" for r in statuses))
    d.metric("Approved KPI reports", len(reports))

    if not statuses:
        st.info("No modernisation cases are registered yet.")
        return

    chosen_case = st.selectbox(
        "Filter cases by reference",
        ["All cases", *sorted({r["case_ref"] for r in statuses})],
        key="enercal_overview_case",
    )
    chosen_status = st.selectbox(
        "Filter engineering review status",
        ["All statuses", *sorted({status_label(r, approved_ids) for r in statuses})],
        key="enercal_overview_review_status",
    )
    visible = [
        r for r in statuses
        if (chosen_case == "All cases" or r["case_ref"] == chosen_case)
        and (chosen_status == "All statuses" or status_label(r, approved_ids) == chosen_status)
    ]
    st.caption(f"Showing {len(visible)} of {len(statuses)} case/report records.")
    if not visible:
        st.info("No cases match those status filters.")
        return
    st.dataframe([
        {
            "Case": r["case_ref"],
            "Case status": r["case_status"],
            "Document ID": r["document_id"],
            "PDF": r["filename"] or "",
            "Revision": r["revision_no"],
            "Engineering status": status_label(r, approved_ids),
        }
        for r in visible
    ], hide_index=True, width="stretch")


def show_approved_comparison(reports):
    """Show approved reports in a compact table without charting estimates."""
    if len(reports) < 2:
        return

    st.subheader("Compare approved EnerCal reports")
    label = st.selectbox(
        "Choose a sustainability KPI",
        [name for _, name, _ in METRICS],
        key="enercal_comparison_metric",
    )
    key = next(key for key, name, _ in METRICS if name == label)
    compatible, reason = comparable_unit(key, reports)
    if not compatible:
        st.info(reason)

    unit = metric_unit(key, reports[0]) if compatible else None
    rows = []
    for report in reports:
        row = {
            "Case": report["case_ref"],
            "Report": report["document_id"],
            "Estimated saving (%)": display(report["calculated_saving"]),
        }
        if compatible:
            before = report[f"existing_{key}"]
            after = report[f"proposed_{key}"]
            row.update({
                f"Existing ({unit})": display(before),
                f"Proposed ({unit})": display(after),
                f"Difference ({unit})": display(difference(before, after)),
            })
        rows.append(row)

    if compatible and not any(
        report[f"existing_{key}"] is not None
        or report[f"proposed_{key}"] is not None
        for report in reports
    ):
        st.info("No usable values for this KPI.")

    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption(
        "Each report is listed separately. Figures are EnerCal estimates, "
        "not verified meter savings; alternative proposals are not summed."
    )


def show_verified_measurements():
    """Read verified pairs independently; a failure must not block approved PDFs."""
    st.subheader("Measured vs estimated energy savings")
    try:
        from services.verified_savings_reporting import load_verified_savings
        measured = load_verified_savings()
    except Exception as error:
        st.error(f"Could not load verified measurements: {error}")
        return

    st.metric("Eligible verified measurement pairs", len(measured))
    if not measured:
        st.info("No eligible verified meter-reading pairs yet.")
        return

    chosen_case = st.selectbox(
        "Filter measured results by case",
        ["All measured cases", *sorted({r["case_ref"] for r in measured})],
        key="enercal_verified_case_filter",
    )
    visible = [
        r for r in measured
        if chosen_case == "All measured cases" or r["case_ref"] == chosen_case
    ]
    st.dataframe([
        {
            "Case": r["case_ref"],
            "Pair ID": r["pair_id"],
            "EnerCal PDF ID": r["document_id"],
            "Elevator ID": r["elevator_id"],
            "Before period": f'{r["before_start"]} to {r["before_end"]}',
            "After period": f'{r["after_start"]} to {r["after_end"]}',
            "Before (kWh/day)": display(r["measured_before_kwh_per_day"]),
            "After (kWh/day)": display(r["measured_after_kwh_per_day"]),
            "Observed saving (%)": display(r["measured_saving_percent"]),
            "EnerCal estimate (%)": display(r["enercal_estimated_saving_percent"]),
            "Observed − estimated (pp)": display(r["measured_minus_estimated_pp"]),
            "Estimate availability": r["estimate_status"],
        }
        for r in visible
    ], hide_index=True, width="stretch")

    st.caption(
        "Observed and estimated savings are compared as percentages for the "
        "same PDF; differences are percentage points (pp)."
    )


def show_report_details(reports):
    """One report selector replaces redundant case+report selectors and tabs."""
    st.subheader("Approved report details")
    lookup = {r["document_id"]: r for r in reports}
    selected_id = st.selectbox(
        "Choose an approved report", list(lookup),
        format_func=lambda doc_id: f'{lookup[doc_id]["case_ref"]} · Report {doc_id}',
    )
    report = lookup[selected_id]

    a, b, c = st.columns(3)
    saving = display(report["calculated_saving"])
    a.metric("Calculated energy saving", f"{saving}%" if saving != "N/A" else saving)

    energy = display(difference(report["existing_energy"], report["proposed_energy"]))
    b.metric("Annual energy reduction", f"{energy} kWh/year" if energy != "N/A" else energy)

    cost = display(difference(report["existing_annual_cost"], report["proposed_annual_cost"]))
    currency = report["currency_symbol"] or "currency unspecified"
    c.metric("Annual electricity cost reduction", f"{cost} {currency}" if cost != "N/A" else cost)
    st.caption(
        f'{report["existing_technology"]} → {report["proposed_technology"]} · '
        f'{display(report["country"])} · {display(report["lifetime_years"])} years'
    )
    st.dataframe([
        {
            "Measure": label,
            "Existing": display(report[f"existing_{key}"]),
            "Proposed": display(report[f"proposed_{key}"]),
            "Difference": display(difference(report[f"existing_{key}"], report[f"proposed_{key}"])),
            "Unit": metric_unit(key, report),
        }
        for key, label, _ in METRICS
    ], hide_index=True, width="stretch")

    # No contact/profile data are loaded; export only the approved KPI records.
    exported = []
    for r in reports:
        row = dict(r)
        row.update({
            f"{key}_difference": difference(r[f"existing_{key}"], r[f"proposed_{key}"])
            for key, _, _ in METRICS
        })
        exported.append(row)
    st.download_button(
        "Download approved KPI data",
        data=pd.DataFrame(exported).to_csv(index=False).encode("utf-8-sig"),
        file_name="enercal_approved_kpis.csv",
        mime="text/csv",
    )


def main():
    if st.session_state.get("role") != "Admin":
        st.error("Admin access is required")
        st.stop()

    st.subheader("EnerCal | Sustainability KPI Reporting")
    try:
        statuses, reports = load_case_statuses(), load_reports()
    except Exception as error:
        st.error(f"Could not load reporting data: {error}")
        st.stop()

    show_case_overview(statuses, reports)
    show_verified_measurements()
    if not reports:
        st.info("No complete engineering-approved EnerCal reports yet.")
        return
    show_approved_comparison(reports)
    show_report_details(reports)


main()
