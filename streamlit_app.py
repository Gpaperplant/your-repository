import streamlit as st

ROLES = ("Requester", "Responder", "Admin")
st.session_state.setdefault("role", None)


def login():
    # Role selection is a prototype, not verified authentication.
    role = st.selectbox("Select your role", ROLES, index=None, placeholder="Choose a role")
    if st.button("Continue", type="primary", disabled=role is None):
        st.session_state.role = role
        st.rerun()


def logout():
    st.session_state.role = None
    st.rerun()


role = st.session_state.role
st.title("KONE Modernisation")
st.logo("images/horizontal_blue.png", icon_image="images/icon_blue.png")

pages = {"Account": [st.Page(logout, title="Log out", icon=":material/logout:")]}
if role in ("Requester", "Admin"):
    pages["Requests"] = [
        st.Page("request/request_1.py", title="Submit & View Requests",
                icon=":material/assignment:", default=role == "Requester")
    ]
if role in ("Responder", "Admin"):
    pages["Responses"] = [
        st.Page("respond/respond_1.py", title="Manage Responses",
                icon=":material/forum:", default=role == "Responder")
    ]
if role == "Admin":
    pages["EnerCal"] = [
        st.Page("enercal/enercal_upload.py", title="Upload EnerCal Report",
                icon=":material/upload_file:", default=True),
        st.Page("enercal/enercal_review.py", title="Case Workspace",
                icon=":material/fact_check:")
    ]

navigation = pages if role else [st.Page(login, title="Choose role")]
st.navigation(navigation).run()
