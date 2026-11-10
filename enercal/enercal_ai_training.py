"""Review verified measurement data and evaluate AI saving predictions."""

from pathlib import Path
import tempfile

import streamlit as st

from services.ai_evaluation_export import (
    build_holdout_comparison,
    evaluation_metrics,
    write_evaluation_files,
)
from services.verified_savings_reporting import load_verified_savings

from services.ai_measurement_loader import prepare_ai_dataset
from services.ai_training import MIN_ELEVATORS, train_and_evaluate


DATA_KEY = "enercal_ai_dataset"
EXCLUDED_KEY = "enercal_ai_excluded"
RESULT_KEY = "enercal_ai_training_result"
EXPORT_KEY = "enercal_ai_evaluation_exports"


METHOD_LABELS = {
    "enercal": "EnerCal estimate",
    "bias_correction": "Average bias correction",
    "ridge_ai": "Ridge AI adjustment",
}


def _reload_dataset():
    # Refresh approved evidence and discard results from the previous snapshot.
    st.session_state.pop(DATA_KEY, None)
    st.session_state.pop(EXCLUDED_KEY, None)
    st.session_state.pop(RESULT_KEY, None)
    st.session_state.pop(EXPORT_KEY, None)

    dataset, excluded = prepare_ai_dataset()
    st.session_state[DATA_KEY] = dataset
    st.session_state[EXCLUDED_KEY] = excluded


def _show_readiness(dataset, excluded):
    # Count distinct verified elevators, not just submitted documents.
    elevator_count = len({row["elevator_id"] for row in dataset})
    first, second, third = st.columns(3)
    first.metric("Eligible projects", len(dataset))
    second.metric("Distinct elevators", elevator_count)
    third.metric("Excluded pairs", len(excluded))

    if dataset:
        with st.expander("Review eligible training records", expanded=False):
            st.dataframe(
                [
                    {
                        "Case": row["case_ref"],
                        "Elevator ID": row["elevator_id"],
                        "EnerCal saving (%)": row["predicted_saving_percent"],
                        "Measured saving (%)": row["measured_saving_percent"],
                        "Correction (pp)": row["target_correction_pp"],
                    }
                    for row in dataset
                ],
                hide_index=True,
                use_container_width=True,
            )

    if excluded:
        with st.expander(f"Excluded measurement pairs ({len(excluded)})"):
            st.dataframe(
                excluded,
                hide_index=True,
                use_container_width=True,
            )

    if elevator_count < MIN_ELEVATORS:
        st.warning(
            f"Training is not ready: {elevator_count} of {MIN_ELEVATORS} "
            "distinct elevators have eligible verified measurements."
        )
        st.info(
            "Approve genuine metered before-and-after period totals in "
            "Measurement Pair Review. EnerCal estimates and ISO reference-cycle "
            "calculations alone are not valid measured training targets."
        )
        return False

    st.success(
        "The minimum independent-elevator count is met. "
        "Training still requires the dataset to pass all validation checks."
    )
    return True


def _show_evaluation(result):
    # Display held-out accuracy; lower MAE and RMSE indicate less error.
    st.subheader("Held-out elevator evaluation")
    st.caption(
        f"Training: {result['train_projects']} projects from "
        f"{result['train_elevators']} elevators. "
        f"Test: {result['test_projects']} projects from "
        f"{result['test_elevators']} unseen elevators."
    )

    rows = [
        {
            "Method": METHOD_LABELS[name],
            "MAE (pp)": result["metrics"][name]["mae_pp"],
            "RMSE (pp)": result["metrics"][name]["rmse_pp"],
            "Bias (pp)": result["metrics"][name]["bias_pp"],
        }
        for name in METHOD_LABELS
    ]
    st.dataframe(rows, hide_index=True, use_container_width=True)
    st.bar_chart(rows, x="Method", y="MAE (pp)")

    st.caption(
        "MAE and RMSE are in percentage points (pp). Positive bias "
        "means predicted savings exceed the measured savings. "
        "The comparison uses the same unseen elevators for all methods."
    )

    with st.expander("Individual held-out predictions", expanded=True):
        st.dataframe(
            [
                {
                    "Case": row["case_ref"],
                    "Elevator ID": row["elevator_id"],
                    "Measured (%)": row["measured_saving_percent"],
                    "EnerCal (%)": row["enercal_percent"],
                    "Bias-adjusted (%)": row["bias_correction_percent"],
                    "Ridge AI (%)": row["ridge_ai_percent"],
                }
                for row in result["test_predictions"]
            ],
            hide_index=True,
            use_container_width=True,
        )

    with st.expander("Model inputs and interpretation"):
        st.write("Features used:", ", ".join(result["feature_names"]))
        st.info(
            "This is a prototype evaluation from a single elevator-grouped "
            "train/test split. Better results on this split do not prove "
            "generalisation. The trained model exists only in this "
            "Streamlit session and is not saved or deployed."
        )


def _prepare_evaluation_exports(dataset, result):
    """Build CSVs from this session's held-out evaluation, not a new model.

    Cross-check each held-out project against the currently verified meter pair
    and the matching approved EnerCal PDF. Refuse export on any inconsistency.
    """
    verified_pairs = load_verified_savings()
    comparisons = build_holdout_comparison(dataset, result, verified_pairs)
    metrics = evaluation_metrics(result)

    # Use temporary files so no permanent project data is written to Codespaces.
    with tempfile.TemporaryDirectory(prefix="enercal_ai_evaluation_") as folder:
        output_paths = write_evaluation_files(folder, comparisons, metrics)
        return [
            {"filename": Path(filename).name, "contents": Path(filename).read_bytes()}
            for filename in output_paths
        ]


def _show_export_controls(dataset, result):
    st.subheader("Export held-out AI evaluation")
    st.caption(
        "Exports include only predictions for elevators held out of model training. "
        "Each record must match a currently verified meter pair and approved "
        "EnerCal PDF. Case and elevator identifiers are internal project data."
    )

    if st.button("Prepare held-out evaluation CSVs"):
        st.session_state.pop(EXPORT_KEY, None)
        try:
            with st.spinner("Checking held-out predictions against verified evidence..."):
                files = _prepare_evaluation_exports(dataset, result)
        except Exception as exc:
            # An export problem must not hide the existing AI evaluation.
            st.error(f"Cannot prepare AI evaluation exports: {exc}")
        else:
            st.session_state[EXPORT_KEY] = files
            st.success("Two checked CSV files are ready for download.")

    for item in st.session_state.get(EXPORT_KEY, []):
        st.download_button(
            f'Download {item["filename"]}',
            data=item["contents"],
            file_name=item["filename"],
            mime="text/csv",
            key=f'enercal_ai_export_{item["filename"]}',
        )


def main():
    st.title("EnerCal | AI Training & Evaluation")
    st.caption(
        "Compare EnerCal saving estimates with measured outcomes using "
        "independently reviewed modernisation projects."
    )

    # The role picker in this prototype is not secure authentication.
    if st.session_state.get("role") != "Admin":
        st.error("This prototype page is restricted to the Admin navigation role.")
        st.stop()

    st.info(
        "This prototype role selection is not secure authentication. "
        "Verified reviewer identity and access control are required "
        "before production use."
    )

    refresh = st.button("Reload verified dataset")
    if refresh or DATA_KEY not in st.session_state:
        try:
            _reload_dataset()
        except Exception as exc:
            st.error(f"Could not load verified AI data: {exc}")
            return

    dataset = st.session_state[DATA_KEY]
    excluded = st.session_state[EXCLUDED_KEY]

    st.subheader("Verified data readiness")
    if not _show_readiness(dataset, excluded):
        return

    st.divider()
    st.subheader("Train and compare predictions")
    test_percent = st.slider(
        "Distinct elevators reserved for testing (%)",
        min_value=20,
        max_value=40,
        value=25,
        step=5,
    )

    if st.session_state.get("enercal_ai_test_percent") != test_percent:
        st.session_state["enercal_ai_test_percent"] = test_percent
        st.session_state.pop(RESULT_KEY, None)
        st.session_state.pop(EXPORT_KEY, None)

    if st.button("Train and evaluate", type="primary"):
        st.session_state.pop(EXPORT_KEY, None)
        try:
            with st.spinner("Evaluating on unseen elevators..."):
                st.session_state[RESULT_KEY] = train_and_evaluate(
                    dataset,
                    test_size=test_percent / 100,
                    random_state=42,
                )
        except (ValueError, TypeError) as exc:
            st.session_state.pop(RESULT_KEY, None)
            st.error(f"Training could not be completed: {exc}")

    if result := st.session_state.get(RESULT_KEY):
        _show_evaluation(result)
        _show_export_controls(dataset, result)


main()
