"""Visible product roadmap; not connected to measured data or ML."""
import streamlit as st
from ui_theme import heading, future_notice

heading("Platform roadmap", "Analytics & AI",
        "Planned extensions for energy verification, reporting and predictive analysis.")
future_notice()
st.info("These pages are design concepts only. No measured savings or model outputs are available here.")
a, b = st.tabs(["Reporting & verification", "AI energy analysis"])
with a:
    st.subheader("Predicted versus achieved energy savings")
    st.write("Future reporting will compare eligible approved EnerCal estimates with independently reviewed, comparable before-and-after meter data.")
    st.markdown("**Planned:** import and validate meter readings, examine prediction gaps, visualize historical project trends, and export reviewed reporting datasets to Power BI.")
    st.caption("Approved EnerCal extraction ≠ verified energy savings. Future analytics must preserve the distinction between latest and approved revisions.")
with b:
    st.subheader("Measurement quality and prediction")
    st.write("Planned process: import metered data → check gaps, duplicates, unusual values and resets → flag potential anomalies with Isolation Forest → engineering review.")
    st.write("Regression models may then be evaluated against EnerCal estimates and verified historical outcomes, with uncertainty displayed only when validated.")
    st.caption("Anomaly flags are not engineering decisions; model predictions are not approved facts.")
