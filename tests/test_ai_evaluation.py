"""Offline logic checks for the evaluation functions on the merged page."""

import ast
import math
from pathlib import Path

import pandas as pd

PAGE = Path(__file__).resolve().parents[1] / "enercal/enercal_ai_engineering12.py"


def load_function(name, namespace):
    tree = ast.parse(PAGE.read_text())
    func = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == name)
    exec(compile(ast.Module(body=[func], type_ignores=[]), str(PAGE), "exec"), namespace)
    return namespace[name]


def test_iso_reference_ignores_iso_before():
    def query(_sql, _params):
        return [
            {"document_type": "iso_excel", "phase": "existing", "field_name": "energy_saving_percent", "numeric_value": 99},
            {"document_type": "iso_excel", "phase": "proposed", "field_name": "energy_saving_percent", "numeric_value": 47.33},
        ]
    func = load_function("iso_reference", {"math": math, "_query": query, "CASE_SQL": "SQL"})
    assert func("CASE") == 47.33


def test_iso_reference_rejects_ambiguous_or_nonfinite():
    data = [{"document_type": "iso_excel", "phase": "proposed", "field_name": "energy_saving_percent", "numeric_value": float("nan")}]
    func = load_function("iso_reference", {"math": math, "_query": lambda *_: data, "CASE_SQL": "SQL"})
    assert func("CASE") is None
    data.append(dict(data[0], numeric_value=47.33))
    assert func("CASE") is None


def test_rejected_cases_excluded_and_errors_correct():
    def features(case):
        if case == "REJECTED":
            raise ValueError("Both revisions must be approved")
        return {"input": 1}, "test_fixture_demo"

    func = load_function("evaluate_cases", {
        "pd": pd, "list_cases": lambda: ["DEMO", "REJECTED"],
        "load_case_features": features, "iso_reference": lambda _case: 47.0,
        "predict": lambda _: {"mod_savings": 50.0, "ai_savings": 45.0},
    })
    results, skipped = func()
    assert len(results) == 1
    assert results.iloc[0]["MOD error (pp)"] == 3
    assert results.iloc[0]["AI error (pp)"] == 2
    assert bool(results.iloc[0]["AI improved"])
    assert skipped.iloc[0]["Case"] == "REJECTED"
