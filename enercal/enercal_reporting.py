"""Engineer-approved EnerCal estimates and original report information."""

import pandas as pd
import streamlit as st

from database import get_connection


if st.session_state.get("role") != "Admin":
    st.error("Admin access required.")
    st.stop()


REPORT_FIELDS = {
    "report_date": "Report date",
    "client": "Client",
    "project_name": "Project",
    "site": "Site",
    "platform_type": "Platform type",
    "elevator_quantity": "Quantity of identical elevators",
    "group_name": "Group name",
}

CONTACT_FIELDS = {
    "contact_first_name": "Contact first name",
    "contact_last_name": "Contact last name",
    "contact_address": "Address",
    "contact_city": "City",
    "contact_country": "Country",
    "contact_phone": "Phone",
    "contact_email": "Email",
}


@st.cache_data(ttl=60)
def load_reports():
    """Use only the latest approved revision and its source PDF metadata."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT document_id, case_ref, filename, revision_no,
                MAX(annual_energy_consumption_kwh_year) FILTER (
                    WHERE technology_role = 'existing') AS existing_energy,
                MAX(annual_energy_consumption_kwh_year) FILTER (
                    WHERE technology_role = 'proposed') AS proposed_energy,
                MAX(annual_regenerative_energy_kwh_year) FILTER (
                    WHERE technology_role = 'existing') AS existing_regen,
                MAX(annual_regenerative_energy_kwh_year) FILTER (
                    WHERE technology_role = 'proposed') AS proposed_regen,
                MAX(annual_electricity_expenditure_bhd_year) FILTER (
                    WHERE technology_role = 'existing') AS existing_annual_cost,
                MAX(annual_electricity_expenditure_bhd_year) FILTER (
                    WHERE technology_role = 'proposed') AS proposed_annual_cost,
                MAX(lifetime_electricity_expenditure_bhd) FILTER (
                    WHERE technology_role = 'existing') AS existing_lifetime_cost,
                MAX(lifetime_electricity_expenditure_bhd) FILTER (
                    WHERE technology_role = 'proposed') AS proposed_lifetime_cost,
                MAX(operational_co2e_kg_lifetime_20_years) FILTER (
                    WHERE technology_role = 'existing') AS existing_operational_co2e,
                MAX(operational_co2e_kg_lifetime_20_years) FILTER (
                    WHERE technology_role = 'proposed') AS proposed_operational_co2e,
                MAX(embodied_co2e_kg) FILTER (
                    WHERE technology_role = 'existing') AS existing_embodied_co2e,
                MAX(embodied_co2e_kg) FILTER (
                    WHERE technology_role = 'proposed') AS proposed_embodied_co2e,
                MAX(calculated_energy_saving_percent) AS saving_percent
            FROM enercal_approved_comparison_view
            GROUP BY document_id, case_ref, filename, revision_no
            HAVING COUNT(DISTINCT technology_role) = 2
            ORDER BY document_id DESC
        """)
        columns = [col.name for col in cur.description]
        reports = [dict(zip(columns, row)) for row in cur.fetchall()]
        if not reports:
            return []

        keys = list(REPORT_FIELDS) + list(CONTACT_FIELDS)
        cur.execute("""
            SELECT document_id, field_name, field_value
            FROM enercal_specifications
            WHERE document_id = ANY(%s) AND field_name = ANY(%s)
        """, ([row["document_id"] for row in reports], keys))
        metadata = {}
        for document_id, key, value in cur.fetchall():
            metadata.setdefault(document_id, {})[key] = value

    for report in reports:
        # Missing PDF values remain blank, never zero or invented data.
        report.update({key: metadata.get(report["document_id"], {}).get(key, "")
                       for key in keys})
    return reports


def display(value):
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, (int, float)) or hasattr(value, "as_tuple"):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return str(value)


def difference(first, second):
    return first - second if first is not None and second is not None else None


st.title("EnerCal | Approved KPI Reporting")
st.caption("Engineer-approved report estimates, not verified energy measurements.")

try:
    reports = load_reports()
except Exception as exc:
    st.error(f"Could not load approved reports: {exc}")
    st.stop()

if not reports:
    st.info("No approved EnerCal reports yet. Approve a report in Case Workspace first.")
    st.stop()

cases = sorted({row["case_ref"] for row in reports})
selected_case = st.selectbox("Modernisation case", ["All cases", *cases])
filtered = [row for row in reports if selected_case == "All cases" or row["case_ref"] == selected_case]

st.metric("Approved reports", len(filtered))
chosen_id = st.selectbox(
    "Approved EnerCal PDF",
    [row["document_id"] for row in filtered],
    format_func=lambda doc_id: next(
        f'{row["case_ref"]} | {row["filename"]} | Revision {row["revision_no"]}'
        for row in filtered if row["document_id"] == doc_id
    ),
)
report = next(row for row in filtered if row["document_id"] == chosen_id)

existing_co2 = (report["existing_operational_co2e"] + report["existing_embodied_co2e"]
                if report["existing_operational_co2e"] is not None and report["existing_embodied_co2e"] is not None else None)
proposed_co2 = (report["proposed_operational_co2e"] + report["proposed_embodied_co2e"]
                if report["proposed_operational_co2e"] is not None and report["proposed_embodied_co2e"] is not None else None)

st.subheader("Sustainability performance")
st.metric("Estimated energy saving", f'{display(report["saving_percent"])}%' if report["saving_percent"] is not None else "")

indicators = [
    ("Annual energy", "kWh/year", "existing_energy", "proposed_energy"),
    ("Regenerative energy", "kWh/year", "existing_regen", "proposed_regen"),
    ("Annual electricity cost", "BHD/year", "existing_annual_cost", "proposed_annual_cost"),
    ("Lifetime electricity cost", "BHD", "existing_lifetime_cost", "proposed_lifetime_cost"),
    ("Operational CO₂e (20 years)", "kg CO₂e", "existing_operational_co2e", "proposed_operational_co2e"),
    ("Embodied CO₂e (A1–A5)", "kg CO₂e", "existing_embodied_co2e", "proposed_embodied_co2e"),
]
rows = [{
    "KPI": label,
    "Existing": display(report[old]),
    "Proposed": display(report[new]),
    "Difference (existing − proposed)": display(difference(report[old], report[new])),
    "Unit": unit,
} for label, unit, old, new in indicators]
rows.append({
    "KPI": "Combined operational + embodied CO₂e",
    "Existing": display(existing_co2),
    "Proposed": display(proposed_co2),
    "Difference (existing − proposed)": display(difference(existing_co2, proposed_co2)),
    "Unit": "kg CO₂e",
})
st.dataframe(rows, hide_index=True, width="stretch")
st.caption("Positive difference indicates a reduction. Embodied CO₂e is report-scoped A1–A5; operational CO₂e is a 20-year estimate. Blank means not stated or unavailable.")

st.subheader("Report information (PDF page 2)")
st.dataframe([{"Field": label, "Value": report[key]} for key, label in REPORT_FIELDS.items()],
             hide_index=True, width="stretch")
with st.expander("KONE contact information (internal use)"):
    st.dataframe([{"Field": label, "Value": report[key]} for key, label in CONTACT_FIELDS.items()],
                 hide_index=True, width="stretch")

# Export all approved reports, excluding personal contact information.
export = pd.DataFrame(filtered).drop(columns=list(CONTACT_FIELDS))
for label, old, new in (
    ("annual_energy_reduction_kwh", "existing_energy", "proposed_energy"),
    ("annual_cost_reduction_bhd", "existing_annual_cost", "proposed_annual_cost"),
    ("lifetime_cost_reduction_bhd", "existing_lifetime_cost", "proposed_lifetime_cost"),
    ("operational_co2e_reduction_kg", "existing_operational_co2e", "proposed_operational_co2e"),
):
    export[label] = export[old] - export[new]
export["combined_co2e_reduction_kg"] = (
    export["existing_operational_co2e"] + export["existing_embodied_co2e"]
    - export["proposed_operational_co2e"] - export["proposed_embodied_co2e"]
)
st.download_button("Download approved KPI data (CSV)",
                   export.to_csv(index=False).encode("utf-8-sig"),
                   file_name="enercal_approved_kpis.csv", mime="text/csv")
st.caption("One row per approved PDF. Do not sum alternative proposals for the same project.")
