"""Case registry and source-document review overview."""
import re
import streamlit as st
from database import get_connection
from ui_theme import heading, prototype_notice

if st.session_state.get("role") != "Admin":
    st.error("Admin prototype role required.")
    st.stop()

heading("Engineering / Cases", "Modernisation Cases",
        "Register an elevator-linked modernisation project, browse cases and review stored reports.")
prototype_notice()

def elevators():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT elevator_id, elevator_ref FROM elevators ORDER BY elevator_ref")
            return cur.fetchall()

def register_case(reference, elevator_id):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO modernisation_cases (case_ref, elevator_id) VALUES (%s, %s) RETURNING case_id",
                (reference, elevator_id),
            )
            return cur.fetchone()[0]

def load_cases():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT m.case_ref, e.elevator_ref, m.status,
                       COUNT(DISTINCT d.document_id) AS report_count,
                       MAX(d.uploaded_at) AS latest_upload
                FROM modernisation_cases m
                LEFT JOIN elevators e ON e.elevator_id = m.elevator_id
                LEFT JOIN source_documents d ON d.case_id = m.case_id
                GROUP BY m.case_id, e.elevator_ref
                ORDER BY m.created_at DESC
            """)
            rows = cur.fetchall()
            cur.execute("""
                SELECT m.case_ref, d.document_id, d.filename, x.revision_no, x.review_status
                FROM source_documents d
                JOIN modernisation_cases m ON m.case_id = d.case_id
                LEFT JOIN LATERAL (
                  SELECT revision_no, review_status FROM extraction_revisions
                  WHERE document_id = d.document_id ORDER BY revision_no DESC LIMIT 1
                ) x ON TRUE ORDER BY d.uploaded_at DESC
            """)
            return rows, cur.fetchall()

try:
    options = elevators()
except Exception as exc:
    st.error(f"Could not read registered elevators: {exc}")
    st.stop()

with st.expander("Create Modernisation Case", expanded=not bool(options)):
    st.caption("Each modernisation case links to an existing registered elevator.")
    with st.form("case_form", clear_on_submit=True):
        new_ref = st.text_input("Case reference", placeholder="MOD-2026-001")
        elevator_map = {ident: ref for ident, ref in options}
        elevator_id = st.selectbox(
            "Existing elevator",
            options=list(elevator_map),
            format_func=lambda ident: f"{elevator_map[ident]} · ID {ident}",
            disabled=not options,
        ) if options else None
        if not options:
            st.warning("No elevators are registered. Create an elevator record first.")
        submit = st.form_submit_button("Create Case", type="primary", disabled=not options)
    if submit:
        ref = new_ref.strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{2,39}", ref):
            st.error("Use 3–40 letters, digits, underscores or hyphens.")
        else:
            try:
                ident = register_case(ref, elevator_id)
            except Exception as error:
                st.error(f"Case creation failed: {error}")
            else:
                st.success(f"Created {ref} (case ID {ident}).")
                st.rerun()

try:
    cases, reports = load_cases()
except Exception as error:
    st.error(f"Could not load modernisation cases: {error}")
    st.stop()

st.subheader("Case directory")
search = st.text_input("Search by case reference or elevator", placeholder="Search cases...")
filtered = [
    row for row in cases
    if search.lower().strip() in (str(row[0]) + " " + str(row[1])).lower()
]
if filtered:
    st.dataframe([
        {"Case reference": ref, "Elevator": elevator or "—",
         "Case status": status.replace("_", " ").title(), "Reports": n,
         "Last report upload": uploaded}
        for ref, elevator, status, n, uploaded in filtered
    ], hide_index=True, use_container_width=True)
else:
    st.info("No matching cases.")

if cases:
    st.subheader("Associated EnerCal reports")
    selected = st.selectbox("Choose a case", [row[0] for row in cases])
    selected_reports = [r for r in reports if r[0] == selected]
    if selected_reports:
        st.dataframe([
            {"Document ID": doc_id, "Filename": name,
             "Latest revision": rev or "—",
             "Review status": (status or "No revision").replace("_", " ").title()}
            for _, doc_id, name, rev, status in selected_reports
        ], hide_index=True, use_container_width=True)
    else:
        st.info("No reports uploaded for this case.")
    a, b = st.columns(2)
    if a.button("Upload report for a case", use_container_width=True):
        st.session_state["enercal_upload_case_selector"] = selected
        st.switch_page("enercal/enercal_upload.py")
    if b.button("Open engineering workspace", use_container_width=True):
        if selected_reports:
            st.session_state["enercal_workspace_case"] = selected
        st.switch_page("enercal/enercal_review.py")
