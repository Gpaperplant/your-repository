"""Admin page for case-level predictions from saved, trusted FYP models."""

from pathlib import Path

import streamlit as st

from services.ai_dataset import load_ai_dataset
from services.ai_prediction import MODEL_FILES, predict_case_from_database


MODEL_PARENT = Path(__file__).resolve().parents[1] / "models"
FEATURE_LABELS = {
    26: "26 MOD features",
    49: "49 MOD + ISO Before features",
}


def available_model_folders(model_root, feature_count):
    """Discover saved local models without loading any joblib content."""
    if feature_count not in FEATURE_LABELS:
        raise ValueError("Feature count must be 26 or 49.")
    root = Path(model_root)
    if not root.is_dir():
        return []
    matches = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink():
            continue
        metadata = folder / "evaluation.json"
        model = folder / MODEL_FILES[feature_count]
        if (
            metadata.is_file() and not metadata.is_symlink()
            and model.is_file() and not model.is_symlink()
        ):
            matches.append(folder)
    return sorted(matches, key=lambda folder: folder.name, reverse=True)


def eligible_cases(feature_count):
    """List only real, fully verified cases from the prediction views."""
    rows = load_ai_dataset(feature_count, training=False)
    cases = {}
    for row in rows:
        case_ref = row["case_ref"]
        if case_ref in cases:
            raise ValueError(f"Duplicate eligible case reference: {case_ref}.")
        cases[case_ref] = row["elevator_ref"]
    return dict(sorted(cases.items()))


def show_prediction(result):
    st.subheader("Estimated ISO energy savings")
    columns = st.columns(3)
    columns[0].metric(
        "MOD estimated saving",
        f"{result['mod_estimated_savings_percent']:.2f}%",
    )
    columns[1].metric(
        "Predicted savings gap",
        f"{result['predicted_savings_gap_pp']:+.2f} pp",
    )
    columns[2].metric(
        "Predicted ISO saving",
        f"{result['predicted_iso_savings_percent']:.2f}%",
    )
    st.caption(
        "Savings gap = predicted ISO saving minus MOD estimated saving. "
        "The ISO figure is a model prediction, not an ISO test measurement."
    )
    st.write(
        f"Case: {result['case_ref']} · Elevator: {result['elevator_ref']} · "
        f"Model: {FEATURE_LABELS[result['feature_count']]}"
    )
    st.caption(
        f"Held-out model MAE: {result['evaluation_mae_pp']:.2f} percentage points · "
        f"Training cases: {result['trained_case_count']} · "
        f"Training fingerprint: {result['training_fingerprint']}"
    )
    if result.get("warning"):
        st.warning(result["warning"])


def render_prediction_page():
    st.subheader("AI savings prediction")
    st.caption(
        "Predict the ISO-minus-MOD savings gap for an approved, fully verified "
        "real-evidence case. ISO After data is never used as a predictor."
    )

    feature_count = st.selectbox(
        "Prediction model",
        (26, 49),
        index=1,
        format_func=lambda count: FEATURE_LABELS[count],
    )
    folders = available_model_folders(MODEL_PARENT, feature_count)
    if not folders:
        st.info(
            "No saved compatible model was found under models/. "
            "Train and save a verified real-evidence model on the AI Model Training page first."
        )
        return

    folder = st.selectbox(
        "Saved model run",
        folders,
        format_func=lambda selected: selected.name,
    )
    try:
        cases = eligible_cases(feature_count)
    except Exception as error:
        st.error(f"Could not load eligible prediction cases: {error}")
        return

    if not cases:
        st.info(
            "No fully verified real cases are eligible for this feature set. "
            "Verify and approve the source engineering values before predicting."
        )
        return

    case_ref = st.selectbox(
        "Approved modernisation case",
        list(cases),
        format_func=lambda ref: f"{ref} · {cases[ref]}",
    )
    st.caption(
        "Only saved local models and database-approved case data are accepted. "
        "No manual or synthetic inputs are used on this page."
    )
    if not st.button("Predict ISO energy savings", type="primary"):
        return

    try:
        with st.spinner("Predicting from approved engineering values"):
            result = predict_case_from_database(
                case_ref, folder, feature_count=feature_count
            )
    except Exception as error:
        st.error(f"Prediction could not complete: {error}")
    else:
        show_prediction(result)


if st.session_state.get("role") != "Admin":
    st.error("Admin access is required")
    st.stop()

render_prediction_page()
