
"""Apply an evaluated session model to an approved EnerCal proposal."""

from math import isfinite

import streamlit as st

from database import get_connection
from services.ai_dataset import FEATURE_COLUMNS, SPEC_FIELDS
from services.ai_training import predict_saving


RESULT_KEY = "enercal_ai_training_result"


def _read_rows(query, params=()):
    # Read the current approved evidence without changing project records.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            columns = [item.name for item in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def _approved_proposals():
    # Consider only projects not yet marked completed and with two approved roles.
    return _read_rows(
        """
        SELECT
            v.document_id,
            d.filename,
            d.case_id,
            m.case_ref,
            m.elevator_id,
            m.status AS case_status,
            MAX(v.annual_energy_consumption_kwh_year)
                FILTER (WHERE v.technology_role = 'existing')
                AS estimated_before_kwh_year,
            MAX(v.annual_energy_consumption_kwh_year)
                FILTER (WHERE v.technology_role = 'proposed')
                AS estimated_after_kwh_year,
            MAX(v.calculated_energy_saving_percent)
                AS predicted_saving_percent
        FROM enercal_approved_comparison_view v
        JOIN source_documents d ON d.document_id = v.document_id
        JOIN modernisation_cases m ON m.case_id = d.case_id
        WHERE m.status <> 'completed'
        GROUP BY
            v.document_id, d.filename, d.case_id,
            m.case_ref, m.elevator_id, m.status
        HAVING COUNT(*) = 2
           AND COUNT(*) FILTER (WHERE v.technology_role = 'existing') = 1
           AND COUNT(*) FILTER (WHERE v.technology_role = 'proposed') = 1
        ORDER BY m.case_ref, v.document_id DESC
        """
    )


def _specifications(document_id):
    # Specifications do not have their own approval status in this schema.
    return _read_rows(
        """
        SELECT field_name, field_value
        FROM enercal_specifications
        WHERE document_id = %s
          AND field_name = ANY(%s)
        ORDER BY field_name
        """,
        (document_id, list(SPEC_FIELDS)),
    )


def _number(value):
    # Keep nonnumeric, missing and infinite values out of predictions.
    if value is None:
        return None

    try:
        number = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError, OverflowError):
        return None

    return number if isfinite(number) else None


def _features(report, specifications, specs_verified):
    # Use EnerCal inputs available before modernisation, never meter outcomes.
    features = {name: None for name in FEATURE_COLUMNS}

    required = (
        "predicted_saving_percent",
        "estimated_before_kwh_year",
        "estimated_after_kwh_year",
    )

    for name in required:
        features[name] = _number(report[name])

    predicted = features["predicted_saving_percent"]
    before = features["estimated_before_kwh_year"]
    after = features["estimated_after_kwh_year"]

    if predicted is None or before is None or after is None:
        raise ValueError(
            "The approved EnerCal energy estimates are incomplete."
        )

    if before <= 0 or after < 0 or predicted > 100:
        raise ValueError(
            "The approved EnerCal energy values are invalid."
        )

    if abs(predicted - (100 * (before - after) / before)) > 0.15:
        raise ValueError(
            "EnerCal saving does not match its annual energy values."
        )

    if specs_verified:
        for item in specifications:
            name = SPEC_FIELDS.get(item["field_name"])

            if name is not None:
                features[name] = _number(item["field_value"])

    return features


def _show_prediction(report, prediction, features, result):
    # These figures are model estimates, not verified measured savings.
    st.subheader("Prototype prediction")

    first, second, third = st.columns(3)

    first.metric(
        "EnerCal saving",
        f"{prediction['enercal_saving_percent']:.2f}%",
    )

    second.metric(
        "AI-adjusted saving",
        f"{prediction['ai_adjusted_saving_percent']:.2f}%",
    )

    third.metric(
        "AI correction",
        f"{prediction['correction_pp']:+.2f} pp",
    )

    before = features["estimated_before_kwh_year"]
    enercal_after = features["estimated_after_kwh_year"]

    ai_after = before * (
        1 - prediction["ai_adjusted_saving_percent"] / 100
    )

    st.dataframe(
        [
            {
                "Estimate": "Before modernisation (EnerCal)",
                "kWh/year": round(before, 2),
            },
            {
                "Estimate": "After modernisation (EnerCal)",
                "kWh/year": round(enercal_after, 2),
            },
            {
                "Estimate": "After modernisation (AI-adjusted)",
                "kWh/year": round(ai_after, 2),
            },
        ],
        hide_index=True,
        use_container_width=True,
    )

    st.caption(
        f"Document #{report['document_id']} | "
        f"Case {report['case_ref']} | "
        f"Model evaluation used {result['train_elevators']} "
        f"training elevators and {result['test_elevators']} "
        "held-out elevators."
    )

    st.warning(
        "The AI model was fitted to the training split for evaluation, "
        "not refitted or independently validated for deployment. "
        "Its adjusted annual kWh value is an estimate, "
        "not a meter reading or an approved KPI."
    )


def main():
    st.title("EnerCal | AI Prediction")

    st.caption(
        "Estimate an AI adjustment for an approved, "
        "unfinished modernisation case."
    )

    # Prototype role selection does not establish reviewer identity.
    if st.session_state.get("role") != "Admin":
        st.error(
            "This prototype page is restricted to "
            "the Admin navigation role."
        )
        st.stop()

    result = st.session_state.get(RESULT_KEY)

    if not isinstance(result, dict) or result.get("model") is None:
        st.warning(
            "No trained model is available in this session. "
            "Open AI Training & Evaluation and complete training "
            "with verified measurements first."
        )
        return

    st.info(
        "This is an exploratory prediction. Re-train after any change "
        "to verified measurements; the model is not saved between "
        "sessions. The prototype Admin role is not secure authentication."
    )

    try:
        reports = _approved_proposals()

    except Exception as exc:
        st.error(
            f"Could not load approved EnerCal proposals: {exc}"
        )
        return

    if not reports:
        st.info(
            "No unfinished cases with an approved existing-and-proposed "
            "EnerCal comparison are available. Approve a report for "
            "an unfinished case first."
        )
        return

    report = st.selectbox(
        "Approved EnerCal proposal",
        reports,
        format_func=lambda row: (
            f"{row['case_ref']} | "
            f"Document #{row['document_id']} | "
            f"{row['filename']}"
        ),
    )

    try:
        specifications = _specifications(report["document_id"])

    except Exception as exc:
        st.error(
            f"Could not load EnerCal specifications: {exc}"
        )
        return

    st.write(
        f"Case status: **{report['case_status']}**"
    )

    st.caption(
        "The three required energy values come from the approved "
        "EnerCal comparison. Optional engineering specifications are "
        "excluded unless you confirm they match the original document."
    )

    if specifications:
        with st.expander("Source engineering specifications"):
            st.dataframe(
                specifications,
                hide_index=True,
                use_container_width=True,
            )

    with st.form(f"ai_prediction_{report['document_id']}"):
        specs_verified = st.checkbox(
            "I checked the optional engineering specifications "
            "against the original EnerCal PDF. "
            "Use them as additional AI inputs.",
            disabled=not specifications,
        )

        submitted = st.form_submit_button(
            "Predict saving",
            type="primary",
        )

    if not submitted:
        return

    try:
        features = _features(
            report,
            specifications,
            specs_verified,
        )

        prediction = predict_saving(
            result,
            features,
        )

        _show_prediction(
            report,
            prediction,
            features,
            result,
        )

    except (ValueError, TypeError, KeyError) as exc:
        st.error(
            f"Prediction could not be completed: {exc}"
        )


main()
