
import streamlit as st
from database import create_request, get_requests

st.title("Submit a Request")
st.write("Submit a request and track its status.")

# Request submission form
with st.form("request_form"):
    requester_name = st.text_input("Your name")
    title = st.text_input("Request title")
    description = st.text_area("Request description")

    submitted = st.form_submit_button("Submit Request")

    if submitted:
        if not requester_name.strip():
            st.error("Please enter your name.")
        elif not title.strip():
            st.error("Please enter a request title.")
        elif not description.strip():
            st.error("Please enter a request description.")
        else:
            try:
                request_id = create_request(
                    requester_name.strip(),
                    title.strip(),
                    description.strip(),
                )
                st.success(f"Request submitted! Your ID is {request_id}.")
            except Exception as e:
                st.error(f"Could not submit request: {e}")

st.divider()

# Display saved requests
st.subheader("Submitted Requests")

try:
    requests = get_requests()

    if requests:
        for request in requests:
            request_id, name, title, description, status, created_at, updated_at = request

            with st.expander(f"Request #{request_id}: {title} — {status}"):
                st.write(f"**Requester:** {name}")
                st.write(f"**Description:** {description}")
                st.write(f"**Created:** {created_at}")
                st.write(f"**Last updated:** {updated_at}")
    else:
        st.info("No requests have been submitted yet.")

except Exception as e:
    st.error(f"Could not load requests: {e}")
