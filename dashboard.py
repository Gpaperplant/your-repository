"""Proposed activity dashboard backed by existing PostgreSQL records."""
import streamlit as st
from database import get_connection
from ui_theme import heading, status_tag, prototype_notice

heading("Workspace / Dashboard", "Overview",
        "A live summary of requests and modernisation activity from the prototype database.")
role = st.session_state.get("role")
prototype_notice()

def get_snapshot():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM requests")
            requests_total = cur.fetchone()[0]
            cur.execute("""
                SELECT COUNT(*) FROM requests
                WHERE status IN ('Pending', 'In Progress')
            """)
            outstanding = cur.fetchone()[0]
            cur.execute("""
                SELECT id, title, status, updated_at FROM requests
                ORDER BY updated_at DESC LIMIT 6
            """)
            recent_requests = cur.fetchall()
            cases = pending = approved = 0
            review_rows = []
            if role == "Admin":
                cur.execute("SELECT COUNT(*) FROM modernisation_cases")
                cases = cur.fetchone()[0]
                # Do not count old approved revisions when a newer revision is pending.
                cur.execute("""
                    WITH newest AS (
                        SELECT DISTINCT ON (document_id)
                            document_id, revision_no, review_status
                        FROM extraction_revisions
                        ORDER BY document_id, revision_no DESC
                    )
                    SELECT
                      COUNT(*) FILTER (WHERE review_status = 'pending'),
                      COUNT(*) FILTER (WHERE review_status = 'approved')
                    FROM newest
                """)
                pending, approved = cur.fetchone()
                cur.execute("""
                    SELECT m.case_ref, d.filename, r.revision_no,
                           r.review_status, d.uploaded_at
                    FROM source_documents d
                    JOIN modernisation_cases m ON m.case_id = d.case_id
                    JOIN LATERAL (
                        SELECT revision_no, review_status
                        FROM extraction_revisions
                        WHERE document_id = d.document_id
                        ORDER BY revision_no DESC LIMIT 1
                    ) r ON TRUE
                    ORDER BY d.uploaded_at DESC LIMIT 8
                """)
                review_rows = cur.fetchall()
    return requests_total, outstanding, recent_requests, cases, pending, approved, review_rows

try:
    total, outstanding, requests, cases, pending, approved, reports = get_snapshot()
except Exception as error:
    st.error("Dashboard data could not be loaded. Check PostgreSQL and applied migrations.")
    st.caption(str(error))
    st.stop()

if role == "Admin":
    cols = st.columns(4)
    for col, (label, value) in zip(cols, [
        ("Modernisation cases", cases),
        ("Reports pending review", pending),
        ("Latest revisions approved", approved),
        ("Open requests", outstanding),
    ]):
        col.metric(label, value)
else:
    a, b = st.columns(2)
    a.metric("Submitted requests", total)
    b.metric("Requests in progress / pending", outstanding)

st.subheader("Quick actions")
if role == "Admin":
    a, b, c = st.columns(3)
    if a.button("Register a case", use_container_width=True):
        st.switch_page("enercal/cases.py")
    if b.button("Upload EnerCal report", use_container_width=True):
        st.switch_page("enercal/enercal_upload.py")
    if c.button("Review extracted data", use_container_width=True):
        st.switch_page("enercal/enercal_review.py")
elif role == "Requester":
    if st.button("Create a request", type="primary"):
        st.switch_page("request/request_1.py")
else:
    if st.button("Open response inbox", type="primary"):
        st.switch_page("respond/respond_1.py")

if role == "Admin":
    st.subheader("Recent EnerCal reports")
    st.caption("Latest review state per document. Approval means reviewed estimates, not verified measured savings.")
    if reports:
        st.dataframe([
            {"Case": case, "Report": filename, "Revision": rev,
             "Engineering status": status.replace("_", " ").title(),
             "Uploaded": uploaded}
            for case, filename, rev, status, uploaded in reports
        ], hide_index=True, use_container_width=True)
    else:
        st.info("No saved EnerCal reports yet. Register a case and upload a PDF.")

st.subheader("Recently updated requests")
if requests:
    st.dataframe([
        {"ID": ident, "Request": title, "Status": status, "Updated": updated}
        for ident, title, status, updated in requests
    ], hide_index=True, use_container_width=True)
else:
    st.info("No requests submitted yet.")
