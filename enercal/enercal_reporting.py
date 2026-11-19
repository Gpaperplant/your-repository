"""Show approved EnerCal KPIs and saved ISO Excel assessments."""

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

# Read only the ISO result fields needed for the overview.
ISO_FIELDS = (
    "before_annual_energy_kwh", "after_annual_energy_kwh",
    "before_annual_energy_wh", "after_annual_energy_wh",
    "before_efficiency_class", "after_efficiency_class",
    "annual_energy_kwh", "annual_energy_wh", "efficiency_class",
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


def load_iso_reports():
    """Load the latest saved ISO Excel assessment for every modernisation case."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT m.case_ref, a.assessment_id, a.lift_id, a.imported_at,
                   a.review_status, d.filename, v.field_name, v.numeric_value, v.value_text
            FROM iso_assessments a
            JOIN modernisation_cases m ON m.case_id = a.case_id
            JOIN iso_documents d ON d.assessment_id = a.assessment_id
                                AND d.document_type = 'iso_excel'
            LEFT JOIN iso_extracted_values v ON v.document_id = d.document_id
                                            AND v.field_name = ANY(%s)
            WHERE a.assessment_id = (
                SELECT latest.assessment_id
                FROM iso_assessments latest
                WHERE latest.case_id = a.case_id
                ORDER BY latest.imported_at DESC, latest.assessment_id DESC
                LIMIT 1
            )
            ORDER BY m.case_ref, v.field_name
        """, (list(ISO_FIELDS),))
        rows = _rows(cur)

    reports = {}
    for row in rows:
        case = row["case_ref"]
        report = reports.setdefault(case, {
            "case_ref": case,
            "assessment_id": row["assessment_id"],
            "lift_id": row["lift_id"],
            "imported_at": row["imported_at"],
            "review_status": row["review_status"],
            "filename": row["filename"],
            "values": {},
        })
        if row["field_name"] is not None:
            report["values"][row["field_name"]] = (
                row["numeric_value"] if row["numeric_value"] is not None
                else row["value_text"]
            )
    return list(reports.values())


def iso_annual_energy(report, role=""):
    """Use kWh where available; otherwise convert stored Wh to kWh."""
    values = report["values"]
    kwh = values.get(f"{role}annual_energy_kwh")
    wh = values.get(f"{role}annual_energy_wh")
    try:
        if kwh is not None:
            number = Decimal(str(kwh))
        elif wh is not None:
            number = Decimal(str(wh)) / Decimal("1000")
        else:
            return None
        return number if number.is_finite() and number >= 0 else None
    except (ArithmeticError, ValueError):
        return None


def iso_saving(report):
    """Calculate the ISO estimate from stored before/after annual energy."""
    before = iso_annual_energy(report, "before_")
    after = iso_annual_energy(report, "after_")
    if before is None or before == 0 or after is None:
        return None
    return (before - after) * 100 / before


def iso_display(value):
    return f"{value:,.2f}" if value is not None else "N/A"


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


def case_readiness(statuses, iso_reports):
    """Only the latest MOD PDF and latest ISO workbook can make a case Ready."""
    latest_mod = {}
    for row in statuses:
        case, document_id = row["case_ref"], row["document_id"]
        if document_id is not None and (
            case not in latest_mod or document_id > latest_mod[case]["document_id"]
        ):
            latest_mod[case] = row

    iso_by_case = {report["case_ref"]: report for report in iso_reports}
    return {
        case: (
            latest_mod.get(case, {}).get("review_status") == "approved"
            and iso_by_case.get(case, {}).get("review_status") == "approved"
        )
        for case in {row["case_ref"] for row in statuses}
    }


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


def show_case_overview(statuses, reports, iso_reports=()):
    """Dropdowns filter the table only; summary cards use unfiltered data."""
    approved_ids = {r["document_id"] for r in reports}
    iso_by_case = {r["case_ref"]: r for r in iso_reports}
    ready_by_case = case_readiness(statuses, iso_reports)
    st.subheader("Modernisation cases and report status")
    a, b, c, d, e, f = st.columns(6)
    a.metric("Modernisation cases", len({r["case_ref"] for r in statuses}))
    b.metric("Without PDF", len({r["case_ref"] for r in statuses if r["document_id"] is None}))
    c.metric("Pending reports", sum(r["review_status"] == "pending" for r in statuses))
    d.metric("Approved KPI reports", len(reports))
    e.metric("Cases with ISO Excel", len(iso_by_case))
    f.metric("Ready cases", sum(ready_by_case.values()))

    if not statuses:
        st.write("No modernisation cases are registered yet.")
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
        st.write("No cases match those status filters.")
        return
    st.dataframe([
        {
            "Case": r["case_ref"],
            "Case status": r["case_status"],
            "Document ID": r["document_id"],
            "PDF": r["filename"] or "",
            "Revision": r["revision_no"],
            "Engineering status": status_label(r, approved_ids),
            "ISO Excel": iso_by_case[r["case_ref"]]["filename"]
            if r["case_ref"] in iso_by_case else "",
            "ISO status": (
                iso_by_case[r["case_ref"]]["review_status"].replace("_", " ").title()
                if r["case_ref"] in iso_by_case else "Not uploaded"
            ),
            "Case readiness": "Ready" if ready_by_case[r["case_ref"]] else "Not ready",
        }
        for r in visible
    ], hide_index=True, width="stretch")
    st.caption("Ready requires approval of the latest MOD PDF and latest ISO Excel workbook for the case.")


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
        st.write(reason)

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
        st.write("No usable values for this KPI.")

    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption(
        "Each report is listed separately. Figures are EnerCal estimates, "
        "not verified meter savings; alternative proposals are not summed."
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


def show_iso_reports(iso_reports, ready_by_case=None):
    """Show ISO estimates, review decisions and case readiness."""
    if not iso_reports:
        return
    st.subheader("ISO Excel energy assessments")
    rows = []
    ready_by_case = ready_by_case or {}
    has_single_column = any(iso_annual_energy(r) is not None for r in iso_reports)
    for report in sorted(iso_reports, key=lambda r: r["case_ref"]):
        values = report["values"]
        rows.append({
            "Case": report["case_ref"],
            "ISO assessment": report["assessment_id"],
            "Excel workbook": report["filename"],
            "Lift": report["lift_id"],
            "Before (kWh/year)": iso_display(iso_annual_energy(report, "before_")),
            "After (kWh/year)": iso_display(iso_annual_energy(report, "after_")),
            "ISO saving (%)": iso_display(iso_saving(report)),
            "Before class": display(values.get("before_efficiency_class")),
            "After class": display(values.get("after_efficiency_class")),
            **({"Single-column energy (kWh/year)": iso_display(iso_annual_energy(report))}
               if has_single_column else {}),
            "Evidence status": report["review_status"].replace("_", " ").title(),
            "Case readiness": "Ready" if ready_by_case.get(report["case_ref"]) else "Not ready",
        })
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption(
        "Latest ISO Excel workbook per case. Values are estimates, not verified "
        "meter savings. ISO approval is a separate engineering decision."
    )
    st.download_button(
        "Download ISO Excel summary",
        data=pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig"),
        file_name="iso_excel_assessments.csv",
        mime="text/csv",
    )


def main():
    if st.session_state.get("role") != "Admin":
        st.error("Admin access is required")
        st.stop()

    st.subheader("Modernisation | Sustainability KPI Overview")
    try:
        statuses, reports, iso_reports = (
            load_case_statuses(), load_reports(), load_iso_reports()
        )
    except Exception as error:
        st.error(f"Could not load reporting data: {error}")
        st.stop()

    show_case_overview(statuses, reports, iso_reports)
    if reports:
        st.markdown("<div style='height: 2rem'></div>", unsafe_allow_html=True)
        if len(reports) >= 2:
            show_approved_comparison(reports)
            st.markdown("<div style='height: 2rem'></div>", unsafe_allow_html=True)
        show_report_details(reports)
    else:
        st.write("No complete engineering-approved EnerCal reports yet.")

    if iso_reports:
        st.markdown("<div style='height: 2rem'></div>", unsafe_allow_html=True)
        show_iso_reports(iso_reports, case_readiness(statuses, iso_reports))


main()
