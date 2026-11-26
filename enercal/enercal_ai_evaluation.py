"""Engineering 12 evaluation against ISO-calculated (not measured) savings."""

import math

import pandas as pd
import streamlit as st

from services.engineering12 import (
    CASE_SQL, MODEL_PATH, _query, list_cases, load_case_features, predict,
)


def iso_savings(case):
    """ISO After is used for evaluation only, never as an AI input."""
    rows = _query(CASE_SQL, (case,))
    values = [row["numeric_value"] for row in rows
              if row["document_type"] == "iso_excel"
              and row["phase"] == "proposed"
              and row["field_name"] == "energy_saving_percent"
              and row["numeric_value"] is not None]
    if len(values) != 1 or not math.isfinite(float(values[0])):
        return None
    return float(values[0])


def evaluate_cases():
    """Use approved cases only; never change reviews or retrain the model."""
    results, skipped = [], []
    for case in list_cases():
        try:
            features, evidence = load_case_features(case)
            reference = iso_savings(case)
            if reference is None:
                raise ValueError("ISO savings reference is missing or ambiguous")
            prediction = predict(features)
        except ValueError as exc:
            skipped.append({"Case": case, "Reason": str(exc)})
            continue

        mod = prediction["mod_savings"]
        ai = prediction["ai_savings"]
        mod_error = abs(mod - reference)
        ai_error = abs(ai - reference)
        results.append({
            "Case": case, "Evidence": evidence,
            "MOD (%)": mod, "AI (%)": ai, "ISO (%)": reference,
            "MOD error (pp)": mod_error, "AI error (pp)": ai_error,
            "AI improved": ai_error < mod_error,
        })
    return pd.DataFrame(results), pd.DataFrame(skipped)


if st.session_state.get("role") != "Admin":
    st.error("Admin access is required.")
    st.stop()

st.title("AI Evaluation — Engineering 12")
st.caption("Frozen XGBoost · ISO-calculated reference, NOT measured energy savings")

if not MODEL_PATH.is_file():
    st.warning(f"Missing frozen model: models/{MODEL_PATH.name}")
    st.stop()

if st.button("Evaluate eligible cases", type="primary"):
    try:
        st.session_state["engineering12_evaluation"] = evaluate_cases()
    except Exception as exc:
        st.error(f"Evaluation failed: {exc}")

if "engineering12_evaluation" not in st.session_state:
    st.info("Click 'Evaluate eligible cases' to start.")
    st.stop()

results, skipped = st.session_state["engineering12_evaluation"]
if results.empty:
    st.warning("No eligible cases with an ISO reference were found.")
else:
    choice = st.selectbox("Evidence group", ["Test fixtures (demo)", "Verified real values"])
    label = "test_fixture_demo" if choice.startswith("Test") else "real_verified"
    shown = results.loc[results["Evidence"] == label].copy()

    if shown.empty:
        st.info("No evaluated cases in this group.")
    else:
        mod_mae = shown["MOD error (pp)"].mean()
        ai_mae = shown["AI error (pp)"].mean()
        improvement = 100 * (mod_mae - ai_mae) / mod_mae if mod_mae > 0 else None
        a, b, c, d = st.columns(4)
        a.metric("Cases", len(shown))
        b.metric("MOD MAE", f"{mod_mae:.2f} pp")
        c.metric("AI MAE", f"{ai_mae:.2f} pp")
        d.metric("MAE reduction", f"{improvement:+.1f}%" if improvement is not None else "N/A")
        st.write(f"AI closer to ISO in **{shown['AI improved'].sum()} of {len(shown)} cases**.")
        st.bar_chart(pd.DataFrame({"MAE (pp)": [mod_mae, ai_mae]},
                                  index=["MOD", "XGBoost AI"]))
        st.dataframe(shown.round(2), hide_index=True, width="stretch")
        st.download_button("Download evaluation CSV", shown.to_csv(index=False),
                           file_name="engineering12_evaluation.csv", mime="text/csv")
        if len(shown) == 1:
            st.caption("Only one case: this is not evidence of general model performance.")

if not skipped.empty:
    with st.expander(f"Excluded cases ({len(skipped)})"):
        st.dataframe(skipped, hide_index=True, width="stretch")

st.warning("Demo fixtures are not validated real elevators. Do not combine demo and verified results.")
st.caption("ISO After is a reference for checking predictions, never one of the 12 AI inputs.")
