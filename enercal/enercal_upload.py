import hashlib
import re
from decimal import Decimal, InvalidOperation

import streamlit as st
from psycopg.errors import UniqueViolation

from database import get_connection
from services.enercal_extractor import extract_full_energy_comparison
from services.enercal_repository import save_complete_enercal_report


# -----------------------------------------------------
# Database helpers
# -----------------------------------------------------


def get_cases():
    """Get all registered modernisation projects."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT case_ref
                FROM modernisation_cases
                ORDER BY case_ref
                """
            )
            return [row[0] for row in cur.fetchall()]


def get_case_schema():
    """Inspect actual case-table requirements before inserting a case."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, is_nullable, column_default,
                       is_identity, is_generated
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'modernisation_cases'
                ORDER BY ordinal_position
                """
            )
            rows = cur.fetchall()
    return [
        {
            "name": name,
            "nullable": nullable == "YES",
            "default": default,
            "identity": identity == "YES",
            "generated": generated == "ALWAYS",
        }
        for name, nullable, default, identity, generated in rows
    ]


def get_elevators():
    """List registered elevators without assuming a specific label column."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM elevators ORDER BY elevator_id")
            columns = [col.name for col in cur.description]
            records = [dict(zip(columns, row)) for row in cur.fetchall()]

    labels = {}
    for elevator in records:
        elevator_id = elevator["elevator_id"]
        readable = None
        for column in (
            "elevator_reference",
            "elevator_ref",
            "elevator_code",
            "unit_reference",
            "equipment_id",
            "building_name",
        ):
            if elevator.get(column):
                readable = str(elevator[column])
                break
        labels[elevator_id] = (
            f"{readable} (ID {elevator_id})"
            if readable else f"Elevator ID {elevator_id}"
        )
    return labels


def create_modernisation_case(case_ref, elevator_id, uses_elevator):
    """Create one case; foreign keys and unique constraints remain enforced."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            if uses_elevator:
                cur.execute(
                    """
                    INSERT INTO modernisation_cases (case_ref, elevator_id)
                    VALUES (%s, %s)
                    RETURNING case_id
                    """,
                    (case_ref, elevator_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO modernisation_cases (case_ref)
                    VALUES (%s)
                    RETURNING case_id
                    """,
                    (case_ref,),
                )
            return cur.fetchone()[0]


def find_existing_document(case_ref, pdf_bytes):
    """Check whether this exact PDF was saved within this case before."""
    file_hash = hashlib.sha256(pdf_bytes).hexdigest()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.document_id
                FROM source_documents d
                JOIN modernisation_cases m ON d.case_id = m.case_id
                WHERE m.case_ref = %s
                  AND d.file_sha256 = %s
                """,
                (case_ref, file_hash),
            )
            result = cur.fetchone()
    return result[0] if result else None


def format_number(value):
    """Show exact stored numeric precision without rounding away decimals."""
    if value is None:
        return "N/A"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return "N/A"
    display = format(number, ",f")
    return display.rstrip("0").rstrip(".") if "." in display else display


def build_preview_rows(comparison):
    """Format the two elevator rows for Streamlit."""
    rows = []
    for role in ("existing", "proposed"):
        data = comparison["rows"][role]
        rows.append({
            "Role": role.title(),
            "Elevator Technology": data["elevator_technology"],
            "Annual Energy (kWh/year)":
                format_number(data["annual_energy_consumption"]),
            "Regenerative Energy (kWh/year)":
                format_number(data["annual_regenerative_energy"]),
            "Annual Expenditure (BHD/year)":
                format_number(data["annual_electricity_expenditure"]),
            "Lifetime Expenditure (BHD)":
                format_number(data["lifetime_electricity_expenditure"]),
            "Operational CO2e (kg/lifetime)":
                format_number(data["operational_co2e"]),
            "Embodied CO2e (kg)": format_number(data["embodied_co2e"]),
        })
    return rows


# -----------------------------------------------------
# Streamlit interface
# -----------------------------------------------------

st.title("EnerCal Report Upload")
st.write(
    "Register a modernisation case, then upload an EnerCal PDF "
    "to extract and review its elevator energy comparison."
)

if st.session_state.get("role") != "Admin":
    st.error("Admin access is required. Open this page from the main application after signing in.")
    st.stop()

if "enercal_case_created" in st.session_state:
    st.success(st.session_state.pop("enercal_case_created"))

try:
    cases = get_cases()
    case_schema = get_case_schema()
except Exception as error:
    st.error(f"Cannot load case information: {error}")
    st.stop()

case_columns = {column["name"] for column in case_schema}
uses_elevator = "elevator_id" in case_columns

# Do not attempt an incomplete INSERT if the deployed schema requires
# additional information that this simple case form does not collect.
required_other_columns = [
    column["name"] for column in case_schema
    if (not column["nullable"]
        and column["default"] is None
        and not column["identity"]
        and not column["generated"]
        and column["name"] not in ("case_ref", "elevator_id"))
]

if "case_ref" not in case_columns:
    st.error("modernisation_cases does not have a case_ref column.")
    st.stop()

with st.expander("Create New Modernisation Case", expanded=not bool(cases)):
    st.caption(
        "Add a case once, then select it below to attach an EnerCal report. "
        "For prototype tests, use references such as TEST-MOD-002."
    )

    if required_other_columns:
        st.error(
            "The database also requires these case fields: "
            + ", ".join(required_other_columns)
            + ". The registration form needs to be extended to match your schema."
        )

    elevator_options = {}
    if uses_elevator:
        try:
            elevator_options = get_elevators()
        except Exception as error:
            st.error(f"Cannot load registered elevators: {error}")

        if not elevator_options:
            st.warning(
                "No registered elevators were found. A case must be linked "
                "to an elevator. Register an elevator first."
            )

    with st.form("new_modernisation_case_form", clear_on_submit=True):
        new_case_ref = st.text_input(
            "New Case Reference",
            placeholder="TEST-MOD-002",
            help="3 to 40 characters. Letters, numbers, hyphens and underscores.",
        )

        selected_elevator = None
        if uses_elevator:
            selected_elevator = st.selectbox(
                "Linked Elevator",
                options=list(elevator_options),
                format_func=lambda value: elevator_options[value],
                disabled=not elevator_options,
                help="Choose the elevator that this modernisation case concerns.",
            )

        submitted_case = st.form_submit_button(
            "Create Modernisation Case",
            type="primary",
            disabled=bool(required_other_columns)
                or (uses_elevator and not elevator_options),
        )

    if submitted_case:
        cleaned_ref = new_case_ref.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,39}", cleaned_ref):
            st.error(
                "Use 3 to 40 letters, numbers, hyphens or underscores "
                "for the case reference."
            )
        elif cleaned_ref in cases:
            st.warning(f"Case {cleaned_ref} already exists. Select it below.")
        else:
            try:
                new_case_id = create_modernisation_case(
                    cleaned_ref, selected_elevator, uses_elevator
                )
            except UniqueViolation:
                st.warning(
                    f"Case {cleaned_ref} already exists. "
                    "Refresh and select it from the case list."
                )
            except Exception as error:
                st.error(f"Case creation failed: {error}")
            else:
                st.session_state["enercal_upload_case_selector"] = cleaned_ref
                st.session_state["enercal_case_created"] = (
                    f"Created {cleaned_ref} (case ID {new_case_id}). "
                    "You can upload its EnerCal PDF below."
                )
                st.rerun()

st.divider()
st.subheader("Upload EnerCal PDF")

if not cases:
    st.info("Create a modernisation case above to continue.")
    st.stop()

case_ref = st.selectbox(
    "Modernisation Case",
    options=cases,
    key="enercal_upload_case_selector",
)

uploaded_file = st.file_uploader(
    "Choose an EnerCal PDF",
    type=["pdf"],
    key=f"enercal_pdf_upload_{case_ref}",
)

# Streamlit returns None until a user selects a file. Never read it before this check.
if uploaded_file is None:
    st.info("Select an EnerCal PDF to preview its calculated energy comparison.")
    st.stop()

pdf_bytes = uploaded_file.getvalue()

if not pdf_bytes:
    st.error("The uploaded PDF is empty.")
    st.stop()

# The file extension alone does not confirm a PDF; reject obvious wrong types.
if not pdf_bytes.startswith(b"%PDF-"):
    st.error("The selected file does not have a valid PDF header.")
    st.stop()

if len(pdf_bytes) > 15 * 1024 * 1024:
    st.error("PDF exceeds the 15 MB prototype limit.")
    st.stop()

try:
    comparison = extract_full_energy_comparison(pdf_bytes)
except Exception as error:
    st.error(f"PDF extraction failed: {error}")
    st.stop()

st.subheader("Extracted Energy Comparison")
st.dataframe(build_preview_rows(comparison), hide_index=True, width="stretch")

col1, col2 = st.columns(2)
with col1:
    st.metric(
        "EnerCal Reported Saving",
        f"{comparison['reported_energy_saving_percent']}%",
    )
with col2:
    st.metric(
        "Calculated Saving",
        f"{comparison['calculated_energy_saving_percent']}%",
    )

st.caption(
    f"Source: PDF page {comparison['source_page']}. "
    "These are calculated EnerCal values, not verified meter measurements."
)
st.info(
    "Extracted values will be saved with pending review status. "
    "Saving is not engineering approval."
)

try:
    document_id = find_existing_document(case_ref, pdf_bytes)
except Exception as error:
    st.error(f"Database lookup failed: {error}")
    st.stop()

if document_id is not None:
    st.info(
        f"This PDF is already stored under document "
        f"ID {document_id}. No duplicate was created."
    )
else:
    if st.button("Save Report to PostgreSQL", type="primary"):
        try:
            result = save_complete_enercal_report(
                case_ref=case_ref,
                filename=uploaded_file.name,
                pdf_bytes=pdf_bytes,
            )
        except Exception as error:
            st.error(f"Database save failed: {error}")
        else:
            document_id = result["document_id"]
            st.success(
                f"Document {document_id} saved successfully. "
                f"{result['pages_saved']} pages and "
                f"{result['fields_saved']} fields stored."
            )

if document_id is not None:
    st.subheader("Stored PostgreSQL Comparison")
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        technology_role,
                        elevator_technology,
                        annual_energy_consumption_kwh_year,
                        annual_regenerative_energy_kwh_year,
                        annual_electricity_expenditure_bhd_year,
                        lifetime_electricity_expenditure_bhd,
                        operational_co2e_kg_lifetime_20_years,
                        embodied_co2e_kg
                    FROM enercal_comparison_view
                    WHERE document_id = %s
                    ORDER BY
                        CASE technology_role
                            WHEN 'existing' THEN 1
                            ELSE 2
                        END
                    """,
                    (document_id,),
                )
                stored_rows = cur.fetchall()
    except Exception as error:
        st.error(f"Could not load stored comparison: {error}")
    else:
        if stored_rows:
            st.dataframe(
                [
                    {
                        "Role": row[0].title(),
                        "Elevator Technology": row[1],
                        "Annual Energy (kWh/year)": format_number(row[2]),
                        "Regenerative Energy (kWh/year)": format_number(row[3]),
                        "Annual Expenditure (BHD/year)": format_number(row[4]),
                        "Lifetime Expenditure (BHD)": format_number(row[5]),
                        "Operational CO2e (kg/lifetime)": format_number(row[6]),
                        "Embodied CO2e (kg)": format_number(row[7]),
                    }
                    for row in stored_rows
                ],
                hide_index=True,
                width="stretch",
            )
        else:
            st.warning(
                "Document exists, but the comparison view returned no rows. "
                "Check that the extraction and view are complete."
            )
