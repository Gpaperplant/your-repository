
"""Entry point for the KONE modernisation workspace."""

import streamlit as st


ROLES = ("Requester", "Responder", "Admin")


st.set_page_config(
    page_title="KONE Modernisation",
    page_icon="images/icon_blue.png",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.logo("images/horizontal_blue.png", icon_image="images/icon_blue.png")
st.session_state.setdefault("role", None)


def login():
    # Role selection is for the prototype and is not authentication.
    st.subheader("Welcome to KONE Modernisation")
    st.caption("Choose your workspace role to continue")

    role = st.selectbox(
        "Workspace role",
        ROLES,
        index=None,
        placeholder="Choose a role",
    )

    if st.button("Continue", type="primary", disabled=role is None):
        st.session_state.role = role
        st.rerun()


def logout():
    st.session_state.role = None
    st.rerun()


def build_navigation(role):
    if role not in ROLES:
        return [st.Page(login, title="Choose role", icon=":material/person:")]

    pages = {}

    if role in ("Requester", "Admin"):
        pages["Requests"] = [
            st.Page(
                "request/request_1.py",
                title="Submit and view requests",
                icon=":material/assignment:",
                default=role == "Requester",
            ),
        ]

    if role in ("Responder", "Admin"):
        pages["Responses"] = [
            st.Page(
                "respond/respond_1.py",
                title="Manage responses",
                icon=":material/forum:",
                default=role == "Responder",
            ),
        ]

    if role == "Admin":
        pages["EnerCal"] = [
            st.Page(
                "enercal/enercal_upload.py",
                title="Upload report",
                icon=":material/upload_file:",
                default=True,
            ),
            st.Page(
                "enercal/enercal_review.py",
                title="Case workspace",
                icon=":material/fact_check:",
            ),
            st.Page(
                "enercal/enercal_measurements.py",
                title="Energy measurements",
                icon=":material/monitor_heart:",
            ),
            st.Page(
                "enercal/enercal_measurement_pairs.py",
                title="Measurement pair review",
                icon=":material/rule:",
            ),
            st.Page(
                "enercal/iso_assessment.py",
                title="ISO energy assessment",
                icon=":material/description:",
            ),
            st.Page(
                "enercal/enercal_reporting.py",
                title="Approved KPI reporting",
                icon=":material/monitoring:",
            ),
            st.Page(
                "enercal/enercal_ai_training.py",
                title="AI training & evaluation",
                icon=":material/model_training:",
            ),
            st.Page(
                "enercal/enercal_ai_prediction.py",
                title="AI prediction",
                icon=":material/online_prediction:",
            ),
        ]

    pages["Account"] = [
        st.Page(logout, title="Log out", icon=":material/logout:")
    ]

    return pages


role = st.session_state.role

if role in ROLES:
    st.sidebar.caption(f"Current role  {role}")

st.navigation(build_navigation(role)).run()
