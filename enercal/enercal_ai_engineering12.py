"""One-page Engineering 12 dashboard: prediction and case evaluation."""

import math
from pathlib import Path

import pandas as pd
import streamlit as st

from enercal.elasticnet_panel import render_panel

from services.engineering12 import (
    CASE_SQL, MODEL_PATH, _query, list_cases, load_case_features, predict,
)


def iso_reference(case):
    """Use ISO After only for comparison, never as a model input."""
    rows = _query(CASE_SQL, (case,))
    matches = [r["numeric_value"] for r in rows
               if r["document_type"] == "iso_excel"
               and r["phase"] == "proposed"
               and r["field_name"] == "energy_saving_percent"
               and r["numeric_value"] is not None]
    if len(matches) != 1:
        return None
    try:
        value = float(matches[0])
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def evaluate_cases():
    """Evaluate approved cases only; rejected reviews remain unchanged."""
    results, skipped = [], []
    for case in list_cases():
        try:
            features, evidence = load_case_features(case)
            reference = iso_reference(case)
            if reference is None:
                raise ValueError("ISO savings reference missing or ambiguous")
            output = predict(features)
        except ValueError as exc:
            skipped.append({"Case": case, "Reason": str(exc)})
            continue

        mod, ai = output["mod_savings"], output["ai_savings"]
        results.append({
            "Case": case, "Evidence": evidence,
            "MOD (%)": mod, "AI (%)": ai, "ISO (%)": reference,
            "MOD error (pp)": abs(mod - reference),
            "AI error (pp)": abs(ai - reference),
            "AI improved": abs(ai - reference) < abs(mod - reference),
        })
    return pd.DataFrame(results), pd.DataFrame(skipped)


# The saved Notebook 5 TEST predictions are research evidence, not database cases.
NOTEBOOK_TEST_CSV = Path(__file__).resolve().parents[1] / "data" / "notebook5_test_predictions.csv"


def load_notebook_test(path):
    """Check the held-out CSV and calculate MOD/AI errors against ISO."""
    data = pd.read_csv(path)
    columns = [
        "case_id", "split", "ISO_calculated_savings_reference_pct",
        "MOD_baseline_savings_pct", "MOD_baseline_error_pp",
        "engineering12_accuracy_savings_pct", "engineering12_accuracy_error_pp",
    ]
    if not set(columns).issubset(data.columns):
        raise ValueError("Notebook 5 CSV is missing required columns")
    if data.empty or data["case_id"].isna().any() or data["case_id"].duplicated().any():
        raise ValueError("Expected distinct, nonempty TEST case IDs")
    if not data["split"].eq("TEST").all():
        raise ValueError("Only TEST rows may be included in Notebook 5 evaluation")

    numbers = data[columns[2:]].apply(pd.to_numeric, errors="raise")
    if not numbers.map(math.isfinite).all().all():
        raise ValueError("Notebook 5 contains non-finite predictions")
    iso = numbers["ISO_calculated_savings_reference_pct"]
    mod = numbers["MOD_baseline_savings_pct"]
    ai = numbers["engineering12_accuracy_savings_pct"]
    if ((mod - iso - numbers["MOD_baseline_error_pp"]).abs() > 1e-6).any():
        raise ValueError("Notebook 5 MOD error does not match its ISO reference")
    if ((ai - iso - numbers["engineering12_accuracy_error_pp"]).abs() > 1e-6).any():
        raise ValueError("Notebook 5 AI error does not match its ISO reference")

    result = pd.DataFrame({
        "Case": data["case_id"], "MOD (%)": mod, "AI (%)": ai,
        "ISO (%)": iso, "MOD error (pp)": (mod - iso).abs(),
        "AI error (pp)": (ai - iso).abs(),
    })
    result["AI improved"] = result["AI error (pp)"] < result["MOD error (pp)"]
    return result


if st.session_state.get("role") != "Admin":
    st.error("Admin access is required.")
    st.stop()

st.title("AI Dashboard")
st.caption("Frozen XGBoost · 12 inputs · ISO-calculated, not measured, savings")

if not MODEL_PATH.is_file():
    st.warning(f"Missing frozen model: models/{MODEL_PATH.name}")
    st.stop()

st.subheader("1. Predict savings")
try:
    cases = list_cases()
except Exception as exc:
    st.error(f"Cannot list cases: {exc}")
    cases = []

if not cases:
    st.info("Upload a MOD PDF and an ISO workbook to start.")
else:
    case = st.selectbox("Modernisation case", cases)
    try:
        features, evidence = load_case_features(case)
    except ValueError as exc:
        st.warning(f"Prediction blocked: {exc}")
    except Exception as exc:
        st.error(f"Cannot load case: {exc}")
    else:
        if evidence == "test_fixture_demo":
            st.warning("RESEARCH DEMO ONLY - MOD and ISO are assumed to match; not verified evidence.")

        with st.expander("Show the 12 model inputs"):
            st.dataframe(pd.DataFrame([
                {"Feature": key, "Value": value} for key, value in features.items()
            ]), hide_index=True, width="stretch")

        if st.button("Predict ISO-calculated savings", type="primary"):
            try:
                output = predict(features)
                reference = iso_reference(case)
                st.session_state["engineering12_prediction"] = (case, output, reference)
            except Exception as exc:
                st.session_state.pop("engineering12_prediction", None)
                st.error(f"Prediction unavailable: {exc}")

        saved = st.session_state.get("engineering12_prediction")
        if saved is not None and saved[0] == case:
            _, output, reference = saved
            mod, ai = output["mod_savings"], output["ai_savings"]
            a, b, c = st.columns(3)
            a.metric("MOD estimate", f"{mod:.2f}%")
            b.metric("Predicted gap", f"{output['gap_pp']:+.2f} pp")
            c.metric("AI-adjusted estimate", f"{ai:.2f}%")
            st.caption(f"Optional conservative estimate: {output['conservative_savings']:.2f}%")

            if reference is not None:
                st.subheader("MOD vs AI vs ISO")
                comparison = pd.DataFrame({
                    "Savings (%)": [mod, ai, reference],
                }, index=["MOD", "XGBoost AI", "ISO reference"])
                st.bar_chart(comparison)
                mod_error, ai_error = abs(mod - reference), abs(ai - reference)
                st.write(f"MOD error: **{mod_error:.2f} pp** | AI error: **{ai_error:.2f} pp**")
                if ai_error < mod_error:
                    st.success("AI is closer to the ISO calculation for this case.")
                elif ai_error > mod_error:
                    st.info("MOD is closer to the ISO calculation for this case.")
                else:
                    st.info("MOD and AI have equal errors for this case.")
            else:
                st.info("ISO reference is unavailable for this case.")

if cases:
    render_panel(case)

st.divider()
st.subheader("2. Evaluate all eligible cases")
if st.button("Evaluate eligible cases"):
    try:
        st.session_state["engineering12_evaluation"] = evaluate_cases()
    except Exception as exc:
        st.error(f"Evaluation failed: {exc}")

if "engineering12_evaluation" in st.session_state:
    results, skipped = st.session_state["engineering12_evaluation"]
    if results.empty:
        st.warning("No eligible cases with ISO references found.")
    else:
        group = st.selectbox("Evidence group", ["Test fixtures (demo)", "Verified real values"])
        label = "test_fixture_demo" if group.startswith("Test") else "real_verified"
        shown = results.loc[results["Evidence"] == label].copy()

        if shown.empty:
            st.info("No evaluated cases in this group.")
        else:
            mod_mae = shown["MOD error (pp)"].mean()
            ai_mae = shown["AI error (pp)"].mean()
            change = 100 * (mod_mae - ai_mae) / mod_mae if mod_mae else None
            a, b, c, d = st.columns(4)
            a.metric("Cases", len(shown))
            b.metric("MOD MAE", f"{mod_mae:.2f} pp")
            c.metric("AI MAE", f"{ai_mae:.2f} pp")
            d.metric("MAE reduction", f"{change:+.1f}%" if change is not None else "N/A")
            st.write(f"AI closer to ISO in **{int(shown['AI improved'].sum())} of {len(shown)} cases**.")
            st.bar_chart(pd.DataFrame({"MAE (pp)": [mod_mae, ai_mae]},
                                      index=["MOD", "XGBoost AI"]))
            st.dataframe(shown.round(2), hide_index=True, width="stretch")
            st.download_button("Download evaluation CSV", shown.to_csv(index=False),
                               file_name="engineering12_evaluation.csv", mime="text/csv")
            if len(shown) == 1:
                st.caption("One test case cannot establish general model performance.")

    if not skipped.empty:
        with st.expander(f"Excluded cases ({len(skipped)})"):
            st.dataframe(skipped, hide_index=True, width="stretch")

st.divider()
st.subheader("3. Notebook 5 - 84-case research TEST evaluation")
st.caption("Recorded frozen Engineering 12 predictions from Notebook 5, separate from PostgreSQL cases.")
if not NOTEBOOK_TEST_CSV.is_file():
    st.info("Add data/notebook5_test_predictions.csv to view the held-out research results.")
else:
    try:
        research = load_notebook_test(NOTEBOOK_TEST_CSV)
    except (ValueError, OSError) as exc:
        st.warning(f"Cannot evaluate Notebook 5 CSV: {exc}")
    else:
        mod_mae = research["MOD error (pp)"].mean()
        ai_mae = research["AI error (pp)"].mean()
        reduction = 100 * (mod_mae - ai_mae) / mod_mae if mod_mae else None
        a, b, c, d = st.columns(4)
        a.metric("TEST cases", len(research))
        b.metric("MOD MAE", f"{mod_mae:.3f} pp")
        c.metric("AI MAE", f"{ai_mae:.3f} pp")
        d.metric("MAE reduction", f"{reduction:+.1f}%" if reduction is not None else "N/A")
        st.write(f"AI closer to ISO in **{int(research['AI improved'].sum())} of {len(research)} cases**.")
        st.bar_chart(pd.DataFrame({"MAE (pp)": [mod_mae, ai_mae]},
                                  index=["MOD", "XGBoost AI"]))
        with st.expander("View Notebook 5 TEST cases"):
            st.dataframe(research.round(2), hide_index=True, width="stretch")
        st.download_button("Download Notebook 5 evaluation CSV", research.to_csv(index=False),
                           file_name="notebook5_engineering12_evaluation.csv", mime="text/csv")
        st.caption("Research TEST partition; earlier notebooks may have inspected these cases. "
                   "Synthetic-labelled IDs, unverified real-project provenance; not independent external validation.")

st.caption("ISO After is a comparison reference, never one of the 12 AI inputs.")
st.caption("Research model: Notebook TEST MAE 9.638 pp; not independently validated.")
