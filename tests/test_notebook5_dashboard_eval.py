"""Notebook 5 TEST display: exact input and correct scorecard calculations."""

import ast
import math
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "enercal" / "enercal_ai_engineering12.py"


def loader():
    tree = ast.parse(PAGE.read_text())
    function = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "load_notebook_test")
    namespace = {"pd": pd, "math": math}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(PAGE), "exec"), namespace)
    return namespace["load_notebook_test"]


def test_real_notebook5_csv_metrics():
    result = loader()(ROOT / "data" / "notebook5_test_predictions.csv")
    assert len(result) == 84
    assert result["Case"].nunique() == 84
    assert result["MOD error (pp)"].mean() == pytest.approx(10.9045252682, abs=1e-8)
    assert result["AI error (pp)"].mean() == pytest.approx(9.6379681220, abs=1e-8)
    assert int(result["AI improved"].sum()) == 53


def test_rejects_non_test_rows(tmp_path):
    data = pd.read_csv(ROOT / "data" / "notebook5_test_predictions.csv")
    data.loc[0, "split"] = "TRAIN"
    csv = tmp_path / "invalid.csv"
    data.to_csv(csv, index=False)
    with pytest.raises(ValueError, match="Only TEST"):
        loader()(csv)


def test_detects_tampered_errors(tmp_path):
    data = pd.read_csv(ROOT / "data" / "notebook5_test_predictions.csv")
    data.loc[0, "engineering12_accuracy_error_pp"] = 0.0
    csv = tmp_path / "invalid.csv"
    data.to_csv(csv, index=False)
    with pytest.raises(ValueError, match="AI error"):
        loader()(csv)


def test_rejects_duplicate_case_ids(tmp_path):
    data = pd.read_csv(ROOT / "data" / "notebook5_test_predictions.csv")
    data.loc[0, "case_id"] = data.loc[1, "case_id"]
    csv = tmp_path / "invalid.csv"
    data.to_csv(csv, index=False)
    with pytest.raises(ValueError, match="distinct"):
        loader()(csv)
