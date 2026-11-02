"""KONE Elevator Modernisation Energy Platform — prototype navigation."""
import streamlit as st
from ui_theme import apply_theme, heading, prototype_notice

st.set_page_config(page_title="KONE | Modernisation Energy", page_icon="images/icon_blue.png", layout="wide")
apply_theme()
st.logo("images/horizontal_blue.png", icon_image="images/icon_blue.png")
if "role" not in st.session_state:
    st.session_state.role = None

def logout():
    st.session_state.role = None
    st.rerun()

def select_role():
    heading("KONE · Engineering platform", "Elevator Modernisation Energy",
            "Manage project requests, preserve EnerCal source reports and review estimated energy performance.")
    st.info("Prototype role selection only. No verified authentication is provided.")
    with st.form("select_role"):
        chosen = st.radio("Choose a workspace", ["Requester", "Responder", "Admin"],
                          captions=["Submit requests and follow their status.",
                                    "Respond to requests and update statuses.",
                                    "Manage cases and review EnerCal estimates."])
        if st.form_submit_button("Continue to workspace", type="primary"):
            st.session_state.role = chosen
            st.rerun()
    prototype_notice()

role = st.session_state.role
if role is None:
    st.navigation([st.Page(select_role, title="Select workspace", icon=":material/login:")]).run()
else:
    dashboard = st.Page("dashboard.py", title="Dashboard", icon=":material/dashboard:", default=True)
    request = st.Page("request/request_1.py", title="Submit & Track", icon=":material/edit_note:")
    respond = st.Page("respond/respond_1.py", title="Response Inbox", icon=":material/mail:")
    cases = st.Page("enercal/cases.py", title="Modernisation Cases", icon=":material/apartment:")
    upload = st.Page("enercal/enercal_upload.py", title="Upload EnerCal PDF", icon=":material/upload_file:")
    workspace = st.Page("enercal/enercal_review.py", title="Case Workspace", icon=":material/fact_check:")
    future = st.Page("future_modules.py", title="Analytics & AI (Future)", icon=":material/monitoring:")
    switch = st.Page(logout, title="Change prototype role", icon=":material/swap_horiz:")
    navigation = {"Workspace": [dashboard]}
    if role in ("Requester", "Admin"):
        navigation["Requests"] = [request]
    if role in ("Responder", "Admin"):
        navigation.setdefault("Requests", []).append(respond)
    if role == "Admin":
        navigation["Engineering"] = [cases, upload, workspace]
        navigation["Roadmap"] = [future]
    navigation["Prototype"] = [switch]
    with st.sidebar:
        st.caption("ENERGY PLATFORM · PROTOTYPE")
        st.markdown(f"**Workspace:** {role}")
        prototype_notice()
    st.navigation(navigation).run()
