"""Admin page for leakage-safe 26/49-feature model comparisons."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import streamlit as st

from services.ai_dataset import dataset_fingerprint, load_ai_dataset
from services.ai_training import (
    MIN_CASES,
    MIN_ELEVATORS,
    save_training_artifacts,
    train_model_comparison,
)


MODEL_PARENT = Path(__file__).resolve().parents[1] / "models"
RESULT_KEY = "fyp_ai_training_result"
FINGERPRINT_KEY = "fyp_ai_training_fingerprints"
ARTIFACTS_KEY = "fyp_ai_training_artifacts"


def eligible_summary(mod_rows, combined_rows):
    mod_cases = {row["case_id"] for row in mod_rows}
    matched = [row for row in combined_rows if row["case_id"] in mod_cases]
    elevators = {row["elevator_ref"] for row in matched}
    return {
        "mod_cases": len(mod_cases),
        "combined_cases": len(combined_rows),
        "matched_cases": len(matched),
        "matched_elevators": len(elevators),
        "ready": len(matched) >= MIN_CASES and len(elevators) >= MIN_ELEVATORS,
    }


def show_results(result):
    st.subheader("Held-out model comparison")
    rows = []
    for label, score in (
        ("Mean baseline", result["baseline_metrics"]),
        ("Ridge · 26 MOD features", result["metrics"][26]),
        ("Ridge · 49 MOD + ISO Before features", result["metrics"][49]),
    ):
        rows.append({
            "Model": label,
            "MAE (pp)": round(score["mae_pp"], 4),
            "RMSE (pp)": round(score["rmse_pp"], 4),
            "R²": round(score["r2"], 4),
        })
    st.dataframe(rows, hide_index=True, width="stretch")
    st.caption(
        "Both Ridge models and the mean baseline use the same held-out cases. "
        "MAE and RMSE are measured in percentage points; lower is better."
    )
    st.write(
        f"Matched cases: {result['matched_case_count']} · "
        f"Training cases: {len(result['train_case_ids'])} · "
        f"Test cases: {len(result['test_case_ids'])} · "
        f"MOD-only cases excluded from comparison: "
        f"{result['excluded_mod_only_case_count']}"
    )
    st.caption(
        "The split keeps elevator references separate. "
        f"Seed: {result['random_state']} · "
        f"Test fraction: {result['test_size']:.2f} · "
        f"Ridge alpha: {result['ridge_alpha']:g}"
    )


def save_current_models(result):
    MODEL_PARENT.mkdir(parents=True, exist_ok=True)
    name = (
        f"real_evidence_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_"
        f"{uuid4().hex[:8]}"
    )
    return save_training_artifacts(result, MODEL_PARENT / name)


def render_training_page():
    st.subheader("AI model training")
    st.caption(
        "Compare MOD-only (26 features) with MOD + ISO Before (49 features) "
        "using approved, verified real evidence. ISO After is used only for "
        "the supervised training target."
    )

    try:
        mod_rows = load_ai_dataset(26, training=True)
        combined_rows = load_ai_dataset(49, training=True)
    except Exception as error:
        st.error(f"Could not load verified training data: {error}")
        return

    fingerprints = (
        dataset_fingerprint(mod_rows, 26, training=True),
        dataset_fingerprint(combined_rows, 49, training=True),
    )
    if st.session_state.get(FINGERPRINT_KEY) != fingerprints:
        st.session_state.pop(RESULT_KEY, None)
        st.session_state.pop(ARTIFACTS_KEY, None)
        st.session_state[FINGERPRINT_KEY] = fingerprints

    overview = eligible_summary(mod_rows, combined_rows)
    columns = st.columns(3)
    columns[0].metric("Eligible MOD cases", overview["mod_cases"])
    columns[1].metric("Matched 49-feature cases", overview["matched_cases"])
    columns[2].metric("Matched elevators", overview["matched_elevators"])

    if not overview["ready"]:
        st.info(
            f"Training requires at least {MIN_CASES} matched real labeled "
            f"cases across {MIN_ELEVATORS} distinct elevators. "
            "Verify the source values and approve full-value revisions first."
        )
        st.caption(
            "The 560-row synthetic workbook is not included in this page's "
            "real-evidence training dataset."
        )
        return

    with st.form("fyp_ai_training_form"):
        test_size = st.slider(
            "Held-out test fraction", min_value=0.10,
            max_value=0.40, value=0.20, step=0.05,
        )
        random_state = st.number_input(
            "Random seed", min_value=0, max_value=2147483647,
            value=42, step=1,
        )
        alpha = st.number_input(
            "Ridge alpha", min_value=0.01,
            max_value=100000.0, value=10.0, step=1.0,
        )
        submitted = st.form_submit_button(
            "Train and compare models", type="primary"
        )

    if submitted:
        st.session_state.pop(RESULT_KEY, None)
        st.session_state.pop(ARTIFACTS_KEY, None)
        try:
            result = train_model_comparison(
                mod_rows, combined_rows,
                test_size=float(test_size),
                random_state=int(random_state),
                alpha=float(alpha),
            )
        except Exception as error:
            st.error(f"Training could not complete: {error}")
        else:
            st.session_state[RESULT_KEY] = result
            st.success("Both models were trained and evaluated on the same split.")

    result = st.session_state.get(RESULT_KEY)
    if result is None:
        return

    show_results(result)

    if st.button("Save trained model files"):
        try:
            st.session_state[ARTIFACTS_KEY] = save_current_models(result)
        except Exception as error:
            st.error(f"Could not save model files: {error}")
        else:
            st.success("Model files and evaluation metadata saved.")

    artifacts = st.session_state.get(ARTIFACTS_KEY)
    if artifacts:
        st.caption("Saved files on this application's server")
        st.code("\n".join(artifacts.values()))


if st.session_state.get("role") != "Admin":
    st.error("Admin access is required")
    st.stop()

render_training_page()
