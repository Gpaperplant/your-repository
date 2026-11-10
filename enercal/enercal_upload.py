
"""Register modernisation cases and upload the supported 12 page MOD reports."""

import hashlib
import re
from decimal import Decimal

import streamlit as st
from psycopg.errors import UniqueViolation

from database import get_connection
from services.enercal_extractor import extract_mod_document
from services.enercal_repository import save_complete_enercal_report
from services.enercal_specs import extract_specs


MAX_PDF_SIZE = 15 * 1024 * 1024


# Every modernisation case must be linked to an elevator.
def load_options():
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT case_ref FROM modernisation_cases ORDER BY case_ref"
        )
        cases = [row[0] for row in cur.fetchall()]

        cur.execute(
            """
            SELECT elevator_id, elevator_ref, building_name
            FROM elevators
            ORDER BY elevator_ref
            """
        )
        elevators = {
            elevator_id: f"{reference}    {building or ''}".strip()
            for elevator_id, reference, building in cur.fetchall()
        }

    return cases, elevators


def create_case(case_ref, elevator_id):
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO modernisation_cases (case_ref, elevator_id)
            VALUES (%s, %s)
            RETURNING case_id
            """,
            (case_ref, elevator_id),
        )
        return cur.fetchone()[0]


def find_existing_document(case_ref, pdf_bytes):
    checksum = hashlib.sha256(pdf_bytes).hexdigest()

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT d.document_id
            FROM source_documents d
            JOIN modernisation_cases m ON m.case_id = d.case_id
            WHERE m.case_ref = %s AND d.file_sha256 = %s
            """,
            (case_ref, checksum),
        )
        row = cur.fetchone()

    return row[0] if row else None


def format_value(value):
    if value is None:
        return "N/A"

    if isinstance(value, (Decimal, int, float)):
        number = format(Decimal(str(value)), ",f")
        return number.rstrip("0").rstrip(".") if "." in number else number

    return str(value)


def comparison_rows(comparison):
    currency = comparison["currency_symbol"]
    years = comparison["lifetime_years"]

    fields = (
        ("Elevator technology", "elevator_technology", ""),
        ("Annual energy", "annual_energy_consumption", "kWh/year"),
        ("Regenerative energy", "annual_regenerative_energy", "kWh/year"),
        (
            "Annual electricity cost",
            "annual_electricity_expenditure",
            f"{currency}/year",
        ),
        (
            "Lifetime electricity cost",
            "lifetime_electricity_expenditure",
            currency,
        ),
        (
            "Operational CO₂e",
            "operational_co2e",
            f"kg CO₂e over {years} years",
        ),
        ("Embodied CO₂e", "embodied_co2e", "kg CO₂e"),
    )

    existing = comparison["rows"]["existing"]
    proposed = comparison["rows"]["proposed"]

    return [
        {
            "Measure": label,
            "Existing": format_value(existing[key]),
            "Proposed": format_value(proposed[key]),
            "Unit": unit,
        }
        for label, key, unit in fields
    ]


@st.cache_data(show_spinner=False, max_entries=8)
def extract_report(pdf_bytes):
    # Cache extracted results to avoid repeated PDF processing.
    result = extract_mod_document(pdf_bytes)
    result["specifications"] = extract_specs(pdf_bytes)
    return result


if st.session_state.get("role") != "Admin":
    st.error("Admin access is required")
    st.stop()


st.subheader("EnerCal upload")
st.caption(
    "Register a case and upload a 12 page MOD report for engineering review"
)

message = st.session_state.pop("enercal_case_created", None)

if message:
    st.success(message)


try:
    cases, elevators = load_options()
except Exception as error:
    st.error(f"Could not load cases and elevators {error}")
    st.stop()


with st.expander("Register a new case", expanded=not bool(cases)):
    if not elevators:
        st.info("Register an elevator before creating a case")

    with st.form("enercal_create_case", clear_on_submit=True):
        new_ref = st.text_input(
            "Case reference",
            placeholder="MOD1001",
        )

        elevator_id = st.selectbox(
            "Select elevator",
            list(elevators),
            format_func=lambda key: elevators[key],
            disabled=not bool(elevators),
        )

        create_submitted = st.form_submit_button(
            "Create case",
            type="primary",
            disabled=not bool(elevators),
        )

    if create_submitted:
        clean_ref = new_ref.strip().upper()

        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,39}", clean_ref):
            st.error(
                "Use 3 to 40 letters numbers underscores or hyphens"
            )

        elif clean_ref in cases:
            st.warning("This case already exists")

        else:
            try:
                create_case(clean_ref, elevator_id)

            except UniqueViolation:
                st.warning("This case already exists")

            except Exception as error:
                st.error(f"Could not create case {error}")

            else:
                st.session_state["enercal_upload_selected_case"] = clean_ref
                st.session_state["enercal_case_created"] = (
                    f"Case {clean_ref} created"
                )
                st.rerun()


st.divider()
st.subheader("Upload report")

if not cases:
    st.info("Create a modernisation case to continue")
    st.stop()


case_ref = st.selectbox(
    "Choose a case",
    cases,
    key="enercal_upload_selected_case",
)

uploaded = st.file_uploader(
    "Choose a MOD PDF",
    type="pdf",
    key=f"enercal_pdf_{case_ref}",
)

if uploaded is None:
    st.info("Choose a PDF to preview its energy comparison")
    st.stop()


pdf_bytes = uploaded.getvalue()

if not pdf_bytes.startswith(b"%PDF-") or len(pdf_bytes) > MAX_PDF_SIZE:
    st.error("Choose a valid PDF no larger than 15 MB")
    st.stop()


# Check for duplicates before extracting the PDF.
try:
    existing_id = find_existing_document(case_ref, pdf_bytes)
except Exception as error:
    st.error(f"Could not check existing reports {error}")
    st.stop()


if existing_id is not None:
    st.info(
        f"This report is already saved as document {existing_id}"
    )
    st.stop()


# Read the PDF and reuse the results during saving.
try:
    with st.spinner("Reading MOD report"):
        extracted = extract_report(pdf_bytes)

except Exception as error:
    st.error(f"Could not read the MOD report {error}")
    st.stop()


comparison = extracted["comparison"]
specifications = extracted["specifications"]


st.subheader("Report preview")

left, right = st.columns(2)

left.metric(
    "Reported energy saving",
    f'{format_value(comparison["reported_energy_saving_percent"])}%',
)

right.metric(
    "Calculated energy saving",
    f'{format_value(comparison["calculated_energy_saving_percent"])}%',
)


st.dataframe(
    comparison_rows(comparison),
    hide_index=True,
    width="stretch",
)

st.caption(
    f'Comparison from page {comparison["source_page"]}    '
    f'Lifetime {comparison["lifetime_years"]} years    '
    f'{len(specifications)} additional fields'
)

st.caption(
    "Results are estimates and require engineering approval"
)


with st.expander("Additional extracted information"):
    st.dataframe(
        [
            {
                "Field": key.replace("_", " ").capitalize(),
                "Value": value,
                "Unit": unit or "",
                "Page": page,
            }
            for key, value, unit, page in specifications
        ],
        hide_index=True,
        width="stretch",
        height=320,
    )


if st.button("Save report", type="primary"):
    try:
        result = save_complete_enercal_report(
            case_ref=case_ref,
            filename=uploaded.name,
            pdf_bytes=pdf_bytes,
            extracted=extracted,
        )

    except Exception as error:
        st.error(f"Could not save report {error}")

    else:
        st.success(
            f'Report {result["document_id"]} saved with '
            f'{result["fields_saved"]} comparison fields and '
            f'{result["additional_fields_saved"]} additional fields'
        )

        st.info(
            "Open Case Workspace to review and approve the extracted data"
        )
