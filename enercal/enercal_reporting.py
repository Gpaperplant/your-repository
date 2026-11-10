"""Display approved sustainability KPIs from the 12 page EnerCal MOD reports."""

from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from database import get_connection


# Use the unit neutral columns introduced in migration 008.
METRICS = (
    ("energy", "Annual energy", "annual_energy_kwh_year"),
    ("regen", "Regenerative energy", "regenerative_energy_kwh_year"),
    ("annual_cost", "Annual electricity cost", "annual_electricity_cost_per_year"),
    ("lifetime_cost", "Lifetime electricity cost", "lifetime_electricity_cost"),
    ("operational_co2e", "Operational CO₂e", "operational_co2e_kg_lifetime"),
    ("embodied_co2e", "Embodied CO₂e", "embodied_co2e_kg"),
)

PROFILE = {
    "client": "Client",
    "project_name": "Project",
    "site": "Site",
    "platform_type": "Platform type",
    "elevator_quantity": "Elevator quantity",
    "group_name": "Group name",
}

CONTACT = {
    "contact_first_name": "First name",
    "contact_last_name": "Last name",
    "contact_address": "Address",
    "contact_city": "City",
    "contact_country": "Country",
    "contact_phone": "Phone",
    "contact_email": "Email",
}


def _rows(cur):
    columns = [column.name for column in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def load_case_statuses():
    """Include cases with no PDF and reports awaiting engineering review."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                m.case_ref,
                m.status AS case_status,
                d.document_id,
                d.filename,
                r.revision_no,
                r.review_status
            FROM modernisation_cases m
            LEFT JOIN source_documents d
                ON d.case_id = m.case_id
            LEFT JOIN LATERAL (
                SELECT revision_no, review_status
                FROM extraction_revisions
                WHERE document_id = d.document_id
                ORDER BY revision_no DESC
                LIMIT 1
            ) r ON TRUE
            ORDER BY m.case_ref, d.document_id DESC
            """
        )
        return _rows(cur)


def load_reports():
    """Read approved results and their profile information in two queries."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM enercal_approved_kpis_view ORDER BY document_id DESC"
        )
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

        if not reports:
            return []

        # The specifications table contains profile and contact fields.
        fields = list(PROFILE) + list(CONTACT)
        cur.execute(
            """
            SELECT document_id, field_name, field_value
            FROM enercal_specifications
            WHERE document_id = ANY(%s) AND field_name = ANY(%s)
            """,
            (list(reports), fields),
        )
        for document_id, name, value in cur.fetchall():
            reports[document_id][name] = value

    for report in reports.values():
        for field in fields:
            report.setdefault(field, "")

    # A complete report must contain both elevator technologies.
    return [
        report for report in reports.values()
        if "existing_technology" in report and "proposed_technology" in report
    ]


def display(value):
    """Format numbers without changing the values stored in PostgreSQL."""
    if value is None or value == "":
        return "N/A"
    if isinstance(value, (Decimal, int, float)):
        number = Decimal(str(value))
        if not number.is_finite():
            return "N/A"
        formatted = format(number, ",f")
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted
    return str(value)


def difference(existing, proposed):
    return existing - proposed if existing is not None and proposed is not None else None


def metric_unit(key, report):
    currency = report["currency_symbol"] or "Currency not specified"
    years = report["lifetime_years"]
    units = {
        "energy": "kWh/year",
        "regen": "kWh/year",
        "annual_cost": f"{currency}/year",
        "lifetime_cost": f"{currency}/lifetime",
        "operational_co2e": f"kg CO₂e over {years} years" if years else "kg CO₂e",
        "embodied_co2e": "kg CO₂e",
    }
    return units[key]


def comparison_rows(report):
    """Prepare one table from the six comparison measurements."""
    return [
        {
            "Measure": label,
            "Existing": display(report[f"existing_{key}"]),
            "Proposed": display(report[f"proposed_{key}"]),
            "Difference": display(difference(
                report[f"existing_{key}"], report[f"proposed_{key}"]
            )),
            "Unit": metric_unit(key, report),
        }
        for key, label, _ in METRICS
    ]


def export_rows(reports):
    """Exclude contact data and retain one record per approved PDF."""
    result = []
    for report in reports:
        row = {key: value for key, value in report.items() if key not in CONTACT}
        for key, _, _ in METRICS:
            row[f"{key}_difference"] = difference(
                report[f"existing_{key}"], report[f"proposed_{key}"]
            )
        result.append(row)
    return result


def report_information(report):
    try:
        report_date = date.fromisoformat(str(report["report_date"])).strftime("%d %B %Y")
    except (TypeError, ValueError):
        report_date = display(report["report_date"])

    price = display(report["electricity_price"])
    if price != "N/A" and report["electricity_price_unit"]:
        price = f'{price} {report["electricity_price_unit"]}'

    fields = [
        ("Report date", report_date),
        ("Country", display(report["country"])),
        ("Electricity price", price),
        ("Lifetime in years", display(report["lifetime_years"])),
    ]
    fields.extend((label, display(report[key])) for key, label in PROFILE.items())
    return [{"Field": label, "Value": value} for label, value in fields]


def status_label(row, approved_ids):
    # Report registration and PDF engineering approval are different states.
    if row["document_id"] is None:
        return "No PDF uploaded"
    if row["review_status"] is None:
        return "Extraction missing"
    if row["review_status"] == "approved" and row["document_id"] not in approved_ids:
        return "Approved (KPI data unavailable)"
    return str(row["review_status"]).replace("_", " ").title()


def show_case_overview(statuses, reports):
    """Track every case without exposing unapproved sustainability estimates."""
    approved_ids = {report["document_id"] for report in reports}
    no_pdf = {row["case_ref"] for row in statuses if row["document_id"] is None}
    pending = sum(row["review_status"] == "pending" for row in statuses)

    st.subheader("Modernisation cases and report status")
    first, second, third, fourth = st.columns(4)
    first.metric("Modernisation cases", len({row["case_ref"] for row in statuses}))
    second.metric("Without PDF", len(no_pdf))
    third.metric("Pending reports", pending)
    fourth.metric("Approved KPI reports", len(reports))

    if not statuses:
        st.info("No modernisation cases are registered yet.")
        return

    # Filters affect workflow visibility, never the approved KPI dataset.
    case_options = ["All cases", *sorted({row["case_ref"] for row in statuses})]
    selected_case = st.selectbox(
        "Filter cases by reference", case_options, key="enercal_overview_case"
    )
    labels = {status_label(row, approved_ids) for row in statuses}
    selected_status = st.selectbox(
        "Filter engineering review status",
        ["All statuses", *sorted(labels)],
        key="enercal_overview_review_status",
    )
    visible = [
        row for row in statuses
        if (selected_case == "All cases" or row["case_ref"] == selected_case)
        and (
            selected_status == "All statuses"
            or status_label(row, approved_ids) == selected_status
        )
    ]

    st.caption(f"Showing {len(visible)} of {len(statuses)} case/report records.")
    if not visible:
        st.info("No cases match those status filters.")
    else:
        st.dataframe(
            [
                {
                    "Case": row["case_ref"],
                    "Case status": row["case_status"],
                    "Document ID": row["document_id"],
                    "PDF": row["filename"] or "",
                    "Revision": row["revision_no"],
                    "Engineering status": status_label(row, approved_ids),
                }
                for row in visible
            ],
            hide_index=True,
            width="stretch",
        )

    st.caption(
        "Only engineering-approved, complete EnerCal reports can contribute to "
        "KPI calculations. Missing, pending, rejected and incomplete reports "
        "remain visible for workflow tracking only."
    )


def comparable_unit(key, reports):
    """Do not directly compare costs in mixed currencies or different lifetimes."""
    if not reports:
        return False, ""
    units = {metric_unit(key, report) for report in reports}
    if key in {"annual_cost", "lifetime_cost", "operational_co2e"} and len(units) != 1:
        return False, (
            "These reports use different currencies or assessment lifetimes. "
            "Choose a single compatible group before comparing this KPI."
        )
    if key in {"annual_cost", "lifetime_cost"}:
        if any(not report["currency_symbol"] for report in reports):
            return False, "Some reports do not specify a currency."
    if key == "lifetime_cost":
        # Currency per lifetime is insufficient: 20-year and 25-year costs
        # must not be compared directly even when the symbols are identical.
        years = [report["lifetime_years"] for report in reports]
        if any(year in (None, "", 0) for year in years) or len({str(year) for year in years}) != 1:
            return False, (
                "Lifetime electricity costs require the same specified "
                "assessment period in years across all reports."
            )
    if key == "operational_co2e":
        if any(not report["lifetime_years"] for report in reports):
            return False, "Some reports do not specify an assessment lifetime."
    return True, ""


def show_approved_comparison(reports):
    """Compare approved PDFs individually, never adding alternative proposals."""
    if len(reports) < 2:
        return

    st.subheader("Compare approved EnerCal reports")
    options = [label for _, label, _ in METRICS]
    chosen_label = st.selectbox(
        "Choose a sustainability KPI", options,
        key="enercal_comparison_metric",
    )
    metric_key = next(key for key, label, _ in METRICS if label == chosen_label)
    compatible, explanation = comparable_unit(metric_key, reports)

    if not compatible:
        st.info(explanation)
    else:
        unit = metric_unit(metric_key, reports[0])
        existing_label = f"Existing ({unit})"
        proposed_label = f"Proposed ({unit})"
        data = pd.DataFrame([
            {
                "Report": f'{report["case_ref"]} · {report["document_id"]}',
                existing_label: report[f"existing_{metric_key}"],
                proposed_label: report[f"proposed_{metric_key}"],
            }
            for report in reports
        ])
        for column in (existing_label, proposed_label):
            data[column] = pd.to_numeric(data[column], errors="coerce")
        if data[[existing_label, proposed_label]].notna().any().any():
            st.bar_chart(
                data, x="Report", y=[existing_label, proposed_label]
            )
            st.dataframe(data, hide_index=True, width="stretch")
        else:
            st.info(f"Approved reports have no usable {chosen_label.lower()} values.")

    # Calculated savings remain comparable across reports as percentages.
    savings = pd.DataFrame([
        {
            "Report": f'{report["case_ref"]} · {report["document_id"]}',
            "Estimated energy saving (%)": report["calculated_saving"],
        }
        for report in reports
    ])
    savings["Estimated energy saving (%)"] = pd.to_numeric(
        savings["Estimated energy saving (%)"], errors="coerce"
    )
    if savings["Estimated energy saving (%)"].notna().any():
        st.bar_chart(savings, x="Report", y="Estimated energy saving (%)")

    st.caption(
        "Each PDF is shown separately; alternative proposals for a single "
        "case are not added together. These are approved estimates, not "
        "verified measured savings."
    )


def show_verified_measurements():
    """Show eligible metered pairs separately from approved EnerCal estimates.

    The measurement service is read-only. A problem in that service must not
    prevent engineers from accessing the existing approved KPI dashboard.
    """
    st.subheader("Measured vs estimated energy savings")
    st.caption(
        "Observed before/after meter use is normalized to kWh/day. "
        "EnerCal results are estimates in kWh/year; only percentage savings "
        "are compared, and only for the same approved PDF."
    )

    try:
        # Lazy import keeps the existing KPI page usable even if this optional
        # measurement module has not been installed in a deployment yet.
        from services.verified_savings_reporting import load_verified_savings
        measured = load_verified_savings()
    except ModuleNotFoundError as error:
        if error.name == "services.verified_savings_reporting":
            st.info(
                "Verified-measurement reporting is not installed yet. "
                "Add services/verified_savings_reporting.py to enable it."
            )
        else:
            st.error(f"Could not import verified measurement dependencies: {error}")
        return
    except Exception as error:
        st.error(
            "Could not load verified measurements. The approved EnerCal KPI "
            f"reporting below is still available. Details: {error}"
        )
        return

    st.metric("Eligible verified measurement pairs", len(measured))
    if not measured:
        st.info(
            "No independently verified before-and-after meter pairs are "
            "eligible yet. This is expected for registered or incomplete cases; "
            "pending EnerCal PDFs are not measured savings."
        )
        return

    case_options = ["All measured cases", *sorted({
        row["case_ref"] for row in measured
    })]
    chosen_case = st.selectbox(
        "Filter measured results by case",
        case_options,
        key="enercal_verified_case_filter",
    )
    visible = [
        row for row in measured
        if chosen_case == "All measured cases" or row["case_ref"] == chosen_case
    ]

    # Every table row represents one verified meter-reading pair, not a
    # project's aggregated energy saving or a synthetic annual estimate.
    st.dataframe(
        [
            {
                "Case": row["case_ref"],
                "Pair ID": row["pair_id"],
                "EnerCal PDF ID": row["document_id"],
                "Elevator ID": row["elevator_id"],
                "Before period": f'{row["before_start"]} to {row["before_end"]}',
                "After period": f'{row["after_start"]} to {row["after_end"]}',
                "Before (kWh/day)": display(row["measured_before_kwh_per_day"]),
                "After (kWh/day)": display(row["measured_after_kwh_per_day"]),
                "Observed saving (%)": display(row["measured_saving_percent"]),
                "EnerCal estimate (%)": display(row["enercal_estimated_saving_percent"]),
                "Observed − estimated (pp)": display(row["measured_minus_estimated_pp"]),
                "Estimate availability": row["estimate_status"],
            }
            for row in visible
        ],
        hide_index=True,
        width="stretch",
    )

    # A measured pair may have no matching approved EnerCal estimate; do not
    # create a placeholder estimate or turn missing values into zero.
    comparable = [
        row for row in visible
        if row.get("enercal_estimated_saving_percent") is not None
        and row.get("measured_minus_estimated_pp") is not None
    ]
    if comparable:
        chart_rows = pd.DataFrame([
            {
                "Verified pair": f'{row["case_ref"]} · {row["pair_id"]}',
                "Observed saving (%)": float(row["measured_saving_percent"]),
                "EnerCal estimate (%)": float(row["enercal_estimated_saving_percent"]),
            }
            for row in comparable
        ])
        st.bar_chart(
            chart_rows,
            x="Verified pair",
            y=["Observed saving (%)", "EnerCal estimate (%)"],
        )
    else:
        st.info(
            "Verified meter readings are available, but no matching complete "
            "approved EnerCal estimate is available for these PDF IDs."
        )

    st.caption(
        "Observed saving is the percentage change in kWh/day between the "
        "two approved meter-reading periods, not causal proof of savings. "
        "The difference is in percentage points (pp); multiple pairs for "
        "one project must not be added together."
    )


# Restrict reporting to administrators.
if st.session_state.get("role") != "Admin":
    st.error("Admin access is required")
    st.stop()

st.subheader("EnerCal | Sustainability KPI Reporting")
st.caption("Modernisation case tracking and engineer-approved energy estimates")

try:
    statuses = load_case_statuses()
    reports = load_reports()
except Exception as error:
    st.error(f"Could not load reporting data: {error}")
    st.stop()

show_case_overview(statuses, reports)

# Verified meter readings are independent of EnerCal report approval.
# Render this section even when there are zero approved PDF estimates.
show_verified_measurements()

if not reports:
    st.info("No approved MOD reports yet. Pending or missing reports remain visible above. Review and approve a PDF in Case Workspace when its data has been checked.")
    st.stop()

show_approved_comparison(reports)

cases = sorted({report["case_ref"] for report in reports})
selected_case = st.selectbox(
    "Choose a case", ["All cases", *cases],
    format_func=lambda value: str(value).replace("-", " "),
)
filtered = [
    report for report in reports
    if selected_case == "All cases" or report["case_ref"] == selected_case
]
report_lookup = {report["document_id"]: report for report in filtered}

selected_id = st.selectbox(
    "Choose an approved report",
    list(report_lookup),
    format_func=lambda document_id: (
        f'Report {document_id}    '
        f'Case {str(report_lookup[document_id]["case_ref"]).replace("-", " ")}    '
        f'Revision {report_lookup[document_id]["revision_no"]}'
    ),
)
report = report_lookup[selected_id]

st.caption(f'Approved reports in selection {len(filtered)}')
first, second, third = st.columns(3)
first.metric("Calculated energy saving", f'{display(report["calculated_saving"])}%')
second.metric(
    "Annual energy reduction",
    f'{display(difference(report["existing_energy"], report["proposed_energy"]))} kWh',
)
third.metric(
    "Annual electricity cost reduction",
    f'{display(difference(report["existing_annual_cost"], report["proposed_annual_cost"]))} '
    f'{report["currency_symbol"]}',
)

st.caption(
    f'{report["existing_technology"]} → {report["proposed_technology"]}    '
    f'{display(report["country"])}    '
    f'{display(report["lifetime_years"])} years'
)

results_tab, information_tab, export_tab = st.tabs(
    ["KPI comparison", "Report information", "Export data"]
)

with results_tab:
    st.dataframe(comparison_rows(report), hide_index=True, width="stretch")
    st.caption("Positive differences indicate a lower value for the proposed elevator")
    st.caption("Unavailable values are displayed as N/A and are not treated as zero")

with information_tab:
    st.dataframe(report_information(report), hide_index=True, width="stretch")
    with st.expander("KONE contact information"):
        st.dataframe(
            [{"Field": label, "Value": display(report[key])}
             for key, label in CONTACT.items()],
            hide_index=True,
            width="stretch",
        )

with export_tab:
    st.caption("Export one row per approved PDF without personal contact information")
    csv_data = pd.DataFrame(export_rows(filtered)).to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "Download approved KPI data",
        data=csv_data,
        file_name="enercal_approved_kpis.csv",
        mime="text/csv",
    )
    st.caption("Do not add together alternative proposals for the same project")
