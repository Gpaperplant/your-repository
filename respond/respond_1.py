
import streamlit as st
from database import get_requests, add_response, get_responses, update_request_status

st.title("Respond to Requests")
st.write("View submitted requests and manage their status.")

# Retrieve requests from PostgreSQL
try:
    requests = get_requests()

    if not requests:
        st.info("There are no requests yet.")
    else:
        for request in requests:
            (
                request_id,
                requester_name,
                title,
                description,
                status,
                created_at,
                updated_at,
            ) = request

            with st.expander(f"Request #{request_id}: {title} — {status}"):
                st.write(f"**Requester:** {requester_name}")
                st.write(f"**Description:** {description}")
                st.write(f"**Submitted:** {created_at}")

                # Display existing responses
                st.subheader("Response history")
                responses = get_responses(request_id)

                if responses:
                    for response in responses:
                        _, responder_name, message, response_time = response
                        st.markdown(f"**{responder_name}** — {response_time}")
                        st.write(message)
                else:
                    st.caption("No responses yet.")

                st.divider()

                # Add a new response
                st.subheader("Add a response")
                with st.form(f"response_form_{request_id}"):
                    responder_name = st.text_input(
                        "Your name",
                        key=f"responder_name_{request_id}",
                    )
                    message = st.text_area(
                        "Response message",
                        key=f"message_{request_id}",
                    )
                    response_submitted = st.form_submit_button(
                        "Submit response"
                    )

                    if response_submitted:
                        if not responder_name.strip():
                            st.error("Please enter your name.")
                        elif not message.strip():
                            st.error("Please enter a response.")
                        else:
                            try:
                                add_response(
                                    request_id,
                                    responder_name.strip(),
                                    message.strip(),
                                )
                                st.success("Response saved.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Could not save response: {e}")

                # Update request status
                st.subheader("Update status")
                with st.form(f"status_form_{request_id}"):
                    new_status = st.selectbox(
                        "Status",
                        ["Pending", "In Progress", "Completed", "Rejected"],
                        index=[
                            "Pending",
                            "In Progress",
                            "Completed",
                            "Rejected",
                        ].index(status),
                        key=f"status_{request_id}",
                    )
                    status_submitted = st.form_submit_button("Update status")

                    if status_submitted:
                        try:
                            update_request_status(request_id, new_status)
                            st.success("Status updated.")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Could not update status: {e}")

except Exception as e:
    st.error(f"Could not load requests: {e}")
