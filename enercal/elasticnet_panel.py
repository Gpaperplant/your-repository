
"""Compare XGBoost and ElasticNet using database cases."""

import math
import pandas as pd

from services.engineering12 import (
    CASE_SQL,
    _query,
    list_cases,
    predict as predict_xgb,
    prepare_features,
)
from services.elasticnet49 import (
    predict as predict_elasticnet,
    prepare_case_features,
)


def iso_reference(rows):
    # ISO After is for evaluation only.
    values = [
        r["numeric_value"]
        for r in rows
        if r["document_type"] == "iso_excel"
        and r["phase"] == "proposed"
        and r["field_name"] == "energy_saving_percent"
        and r["numeric_value"] is not None
    ]

    if len(values) != 1:
        return None

    value = float(values[0])
    return value if math.isfinite(value) else None


def compare_case(case):
    rows = _query(CASE_SQL, (case,))

    features12, evidence = prepare_features(rows)
    features49, other_evidence = prepare_case_features(rows)

    if evidence != other_evidence:
        raise ValueError("Inconsistent document evidence")

    xgb = predict_xgb(features12)
    enet = predict_elasticnet(features49)

    mod = xgb["mod_savings"]

    if abs(mod - enet["mod_savings"]) > 1e-6:
        raise ValueError("Different MOD baselines")

    return {
        "case": case,
        "evidence": evidence,
        "mod": mod,
        "xgb": xgb,
        "elasticnet": enet,
        "iso": iso_reference(rows),
        "features49": features49,
    }


def evaluate_cases():
    results = []
    excluded = []

    for case in list_cases():
        try:
            item = compare_case(case)
            iso = item["iso"]

            if iso is None:
                raise ValueError("ISO reference missing")

        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            excluded.append({
                "Case": case,
                "Reason": str(exc),
            })
            continue

        results.append({
            "Case": case,
            "Evidence": item["evidence"],
            "MOD (%)": item["mod"],
            "XGBoost (%)": item["xgb"]["ai_savings"],
            "ElasticNet (%)": item["elasticnet"]["ai_savings"],
            "ISO (%)": iso,
            "MOD error (pp)": abs(item["mod"] - iso),
            "XGBoost error (pp)": abs(
                item["xgb"]["ai_savings"] - iso
            ),
            "ElasticNet error (pp)": abs(
                item["elasticnet"]["ai_savings"] - iso
            ),
        })

    return pd.DataFrame(results), pd.DataFrame(excluded)


def render_panel(case):
    import streamlit as st

    st.subheader("Compare XGBoost and ElasticNet")
    st.caption("Both models use PostgreSQL engineering values.")

    if st.button("Compare both models", key="compare_elasticnet"):
        try:
            st.session_state["elasticnet_comparison"] = compare_case(case)
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            st.warning(f"Comparison unavailable: {exc}")

    item = st.session_state.get("elasticnet_comparison")

    if item and item["case"] == case:
        xgb = item["xgb"]
        enet = item["elasticnet"]

        a, b, c = st.columns(3)

        a.metric("MOD savings", f"{item['mod']:.2f}%")
        b.metric("XGBoost", f"{xgb['ai_savings']:.2f}%")
        c.metric("ElasticNet", f"{enet['ai_savings']:.2f}%")

        st.write(
            f"XGBoost gap: {xgb['gap_pp']:+.2f} pp | "
            f"ElasticNet gap: {enet['gap_pp']:+.2f} pp"
        )

        difference = abs(
            xgb["ai_savings"] - enet["ai_savings"]
        )
        st.write(f"Model disagreement: {difference:.2f} pp")

        with st.expander("Show the 49 database inputs"):
            st.dataframe(
                pd.DataFrame(
                    item["features49"].items(),
                    columns=["Feature", "Value"],
                ),
                hide_index=True,
            )

        estimates = {
            "MOD": item["mod"],
            "XGBoost": xgb["ai_savings"],
            "ElasticNet": enet["ai_savings"],
        }

        if item["iso"] is not None:
            estimates["ISO reference"] = item["iso"]

        st.bar_chart(
            pd.DataFrame.from_dict(
                estimates,
                orient="index",
                columns=["Savings (%)"],
            )
        )

        if item["evidence"] == "test_fixture_demo":
            st.warning("Research demonstration only.")

    if st.button(
        "Evaluate both models on eligible cases",
        key="evaluate_elasticnet",
    ):
        st.session_state["elasticnet_evaluation"] = evaluate_cases()

    evaluation = st.session_state.get("elasticnet_evaluation")

    if not evaluation:
        return

    results, excluded = evaluation

    if not results.empty:
        group = st.selectbox(
            "Compare evidence",
            ["Test fixtures", "Verified real"],
            key="elasticnet_evidence",
        )

        key = (
            "test_fixture_demo"
            if group == "Test fixtures"
            else "real_verified"
        )

        shown = results[results["Evidence"] == key]

        if not shown.empty:
            a, b, c = st.columns(3)

            a.metric(
                "MOD MAE",
                f"{shown['MOD error (pp)'].mean():.2f} pp",
            )

            b.metric(
                "XGBoost MAE",
                f"{shown['XGBoost error (pp)'].mean():.2f} pp",
            )

            c.metric(
                "ElasticNet MAE",
                f"{shown['ElasticNet error (pp)'].mean():.2f} pp",
            )

            st.dataframe(shown.round(2), hide_index=True)

            st.download_button(
                "Download paired results",
                shown.to_csv(index=False),
                "paired_ai_results.csv",
                "text/csv",
            )

            if len(shown) < 2:
                st.caption(
                    "One case is insufficient to select a final model."
                )
        else:
            st.info("No eligible cases in this evidence group.")
    else:
        st.info("No cases ready for paired evaluation.")

    if not excluded.empty:
        with st.expander(f"Excluded cases ({len(excluded)})"):
            st.dataframe(excluded, hide_index=True)
