"""Offline checks for read-only real-evidence 26/49 AI datasets."""

import importlib.util
import math
import sys
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


class Cursor:
    def __init__(self):
        self.rows = []
        self.description = []
        self.queries = []
        self.fail = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, parameters=()):
        if self.fail:
            raise RuntimeError("Simulated database failure")
        self.queries.append((sql, parameters))

    def fetchall(self):
        return list(self.rows)


class Connection:
    def __init__(self):
        self.cur = Cursor()
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, error, *_):
        self.committed = error is None
        self.rolled_back = error is not None
        return False

    def cursor(self):
        return self.cur


@pytest.fixture
def setup(monkeypatch):
    conn = Connection()
    database = ModuleType("database")
    database.get_connection = lambda: conn
    monkeypatch.setitem(sys.modules, "database", database)
    spec = importlib.util.spec_from_file_location(
        "_ai_dataset_under_test", ROOT / "services/ai_dataset.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, conn


def sample(module, feature_count=49, training=False, case_id=1):
    item = {
        "case_id": case_id,
        "case_ref": f"MOD{case_id:04}",
        "elevator_ref": f"OP{case_id:02}",
        "mod_document_id": 100 + case_id,
        "mod_revision_id": 200 + case_id,
        "iso_document_id": 300 + case_id,
        "iso_revision_id": 400 + case_id,
        "source_classification": "real",
    }
    for feature in module.feature_names(feature_count):
        item[feature] = (
            "C" if feature == "ISO_PRE_energy_class" else "Geared"
        ) if feature in module.CATEGORICAL else Decimal("1.25")
    if training:
        item[module.TARGET] = Decimal("-4.25")
    return {
        field: item[field] for field in (
            *module.METADATA[(feature_count, training)],
            *module.feature_names(feature_count),
            *((module.TARGET,) if training else ()),
        )
    }


def fake_result(conn, row):
    conn.cur.description = [SimpleNamespace(name=name) for name in row]
    conn.cur.rows = [tuple(row.values())]


@pytest.mark.parametrize("size,expected", [(26, 26), (49, 49)])
def test_feature_contract(setup, size, expected):
    module, _ = setup
    assert len(module.feature_names(size)) == expected
    assert len(set(module.feature_names(size))) == expected
    assert module.feature_names(size) == (
        module.MOD_FEATURES if size == 26
        else module.MOD_FEATURES + module.ISO_PRE_FEATURES
    )


@pytest.mark.parametrize("size,training,view", [
    (26, False, "fyp_ai_mod_features_26"),
    (49, False, "fyp_ai_features_49"),
    (26, True, "fyp_ai_train_mod_26"),
    (49, True, "fyp_ai_train_49"),
])
def test_exact_views_and_read_only(setup, size, training, view):
    module, conn = setup
    row = sample(module, size, training)
    fake_result(conn, row)
    results = module.load_ai_dataset(size, training=training)
    assert len(results) == 1
    assert results[0]["case_id"] == 1
    assert conn.cur.queries == [
        ("SET TRANSACTION READ ONLY", ()),
        (f"SELECT * FROM {view} ORDER BY case_id", ()),
    ]
    assert conn.committed


@pytest.mark.parametrize("size,training", [
    (26, False), (49, False), (26, True), (49, True)
])
def test_sql_view_column_order_is_preserved(setup, size, training):
    module, conn = setup
    row = sample(module, size, training)
    fake_result(conn, row)
    result = module.load_ai_dataset(size, training=training)[0]
    assert tuple(result) == tuple(row)
    assert len([name for name in result if name in module.feature_names(size)]) == size


def test_parameterized_case_lookup_and_limit(setup):
    module, conn = setup
    fake_result(conn, sample(module))
    module.load_ai_dataset(49, case_ref="  O'P13 --  ", limit=15)
    sql, args = conn.cur.queries[1]
    assert "WHERE case_ref = %s" in sql
    assert "LIMIT %s" in sql
    assert args == ("O'P13 --", 15)
    assert "O'P13" not in sql


def test_empty_sql_result_returns_empty_not_synthetic_rows(setup):
    module, conn = setup
    row = sample(module)
    conn.cur.description = [SimpleNamespace(name=key) for key in row]
    assert module.load_ai_dataset() == []
    with pytest.raises(ValueError, match="No eligible"):
        module.build_training_matrix([])


def test_training_matrix_preserves_groups_target_and_features(setup):
    module, _ = setup
    rows = [sample(module, 49, True, 10), sample(module, 49, True, 11)]
    result = module.build_training_matrix(rows, 49)
    assert len(result["X"]) == 2
    assert len(result["X"][0]) == 49
    assert result["y"] == [-4.25, -4.25]
    assert result["groups"] == [10, 11]
    assert result["feature_names"] == module.feature_names(49)
    assert len(result["fingerprint"]) == 64
    assert "ISO_AFTER" not in " ".join(result["feature_names"])


def test_mod_only_training_does_not_require_iso_before(setup):
    module, _ = setup
    result = module.build_training_matrix([sample(module, 26, True)], 26)
    assert len(result["X"][0]) == 26
    assert result["groups"] == [1]


def test_empty_prediction_is_allowed(setup):
    module, _ = setup
    assert module.prepare_ai_rows([], 26) == []
    assert len(module.dataset_fingerprint([], 26)) == 64


def test_fingerprint_ignores_row_order(setup):
    module, _ = setup
    rows = [sample(module, 49, True, 2), sample(module, 49, True, 1)]
    assert module.dataset_fingerprint(rows, 49, training=True) == (
        module.dataset_fingerprint(list(reversed(rows)), 49, training=True)
    )


def test_fingerprint_changes_for_revisions_and_values(setup):
    module, _ = setup
    row = sample(module, 49, True)
    fingerprint = module.dataset_fingerprint([row], 49, training=True)
    updated = deepcopy(row)
    updated["mod_revision_id"] += 1
    assert module.dataset_fingerprint([updated], 49, training=True) != fingerprint
    updated = deepcopy(row)
    updated["MOD_rated_load_kg"] += 1
    assert module.dataset_fingerprint([updated], 49, training=True) != fingerprint
    updated = deepcopy(row)
    updated[module.TARGET] += 1
    assert module.dataset_fingerprint([updated], 49, training=True) != fingerprint


@pytest.mark.parametrize("bad", [0, 23, 50, "49", True, None])
def test_invalid_feature_count(setup, bad):
    module, conn = setup
    with pytest.raises(ValueError, match="Feature count"):
        module.load_ai_dataset(bad)
    assert not conn.cur.queries


@pytest.mark.parametrize("bad", [None, "yes", 1])
def test_invalid_training_flag(setup, bad):
    module, conn = setup
    with pytest.raises(ValueError, match="Training"):
        module.load_ai_dataset(49, training=bad)
    assert not conn.cur.queries


@pytest.mark.parametrize("bad", [0, -5, 100001, "2", 2.4, True])
def test_invalid_limit(setup, bad):
    module, conn = setup
    with pytest.raises(ValueError, match="Limit"):
        module.load_ai_dataset(limit=bad)
    assert not conn.cur.queries


@pytest.mark.parametrize("bad", ["", "  ", 100, False])
def test_invalid_case_ref(setup, bad):
    module, conn = setup
    with pytest.raises(ValueError, match="Case reference"):
        module.load_ai_dataset(case_ref=bad)
    assert not conn.cur.queries


def test_missing_columns_rejected(setup):
    module, _ = setup
    row = sample(module)
    del row["ISO_PRE_daily_trips"]
    with pytest.raises(ValueError, match="missing=.*ISO_PRE_daily_trips"):
        module.prepare_ai_rows([row])


def test_extra_and_after_columns_rejected(setup):
    module, _ = setup
    row = sample(module)
    row["ISO_AFTER_annual_energy_kwh_year"] = 5
    with pytest.raises(ValueError, match="unexpected=.*ISO_AFTER"):
        module.prepare_ai_rows([row])


def test_target_not_allowed_in_prediction(setup):
    module, _ = setup
    row = sample(module)
    row[module.TARGET] = 1.5
    with pytest.raises(ValueError, match="unexpected"):
        module.prepare_ai_rows([row])


def test_training_target_required(setup):
    module, _ = setup
    row = sample(module, training=True)
    del row[module.TARGET]
    with pytest.raises(ValueError, match="TARGET_savings_gap_pp"):
        module.prepare_ai_rows([row], training=True)


@pytest.mark.parametrize("bad", [None, float('nan'), float('inf'), "1.3", True])
def test_missing_or_invalid_numeric_rejected(setup, bad):
    module, _ = setup
    row = sample(module)
    row["MOD_rated_load_kg"] = bad
    with pytest.raises(ValueError, match="numeric|Non-finite"):
        module.prepare_ai_rows([row])


def test_very_large_decimal_rejected(setup):
    module, _ = setup
    row = sample(module)
    row["MOD_rated_load_kg"] = Decimal("1E+999")
    with pytest.raises(ValueError, match="Non-finite"):
        module.prepare_ai_rows([row])


@pytest.mark.parametrize("bad", [None, "", "  ", 100])
def test_missing_or_invalid_category_rejected(setup, bad):
    module, _ = setup
    row = sample(module)
    row["MOD_gear_existing"] = bad
    with pytest.raises(ValueError, match="categorical"):
        module.prepare_ai_rows([row])


@pytest.mark.parametrize("bad", ["H", "Class Z", "0"])
def test_invalid_efficiency_class(setup, bad):
    module, _ = setup
    row = sample(module)
    row["ISO_PRE_energy_class"] = bad
    with pytest.raises(ValueError, match="efficiency class"):
        module.prepare_ai_rows([row])


@pytest.mark.parametrize("bad", ["synthetic", "unknown", "test_fixture", "Real", None])
def test_nonreal_classification_rejected(setup, bad):
    module, _ = setup
    row = sample(module)
    row["source_classification"] = bad
    with pytest.raises(ValueError, match="real evidence"):
        module.prepare_ai_rows([row])


@pytest.mark.parametrize("field", ["case_id", "mod_document_id", "mod_revision_id", "iso_document_id", "iso_revision_id"])
def test_invalid_provenance_identifiers(setup, field):
    module, _ = setup
    row = sample(module)
    row[field] = 0
    with pytest.raises(ValueError, match="Invalid"):
        module.prepare_ai_rows([row])


def test_duplicate_case_rejected(setup):
    module, _ = setup
    row = sample(module)
    with pytest.raises(ValueError, match="Duplicate AI case"):
        module.prepare_ai_rows([row, deepcopy(row)])


def test_invalid_row_types_rejected(setup):
    module, _ = setup
    with pytest.raises(ValueError, match="list or tuple"):
        module.prepare_ai_rows({"case_id": 1})
    with pytest.raises(ValueError, match="mapping"):
        module.prepare_ai_rows([("x", "y")])


def test_empty_case_reference_in_view_rejected(setup):
    module, _ = setup
    row = sample(module)
    row["case_ref"] = ""
    with pytest.raises(ValueError, match="case_ref"):
        module.prepare_ai_rows([row])


def test_sql_failure_does_not_write_and_rolls_back(setup):
    module, conn = setup
    conn.cur.fail = True
    with pytest.raises(RuntimeError, match="Simulated"):
        module.load_ai_dataset()
    assert conn.rolled_back


def test_duplicate_cursor_columns_rejected(setup):
    module, conn = setup
    row = sample(module)
    fake_result(conn, row)
    conn.cur.description.append(SimpleNamespace(name="case_id"))
    with pytest.raises(ValueError, match="duplicate column"):
        module.load_ai_dataset()
    assert conn.rolled_back


def test_no_database_access_during_prepare(setup):
    module, conn = setup
    module.prepare_ai_rows([sample(module)])
    module.dataset_fingerprint([sample(module)])
    assert not conn.cur.queries


def test_column_order_drift_rejected_even_for_empty_view(setup):
    module, conn = setup
    row = sample(module)
    keys = list(row)
    keys[2], keys[3] = keys[3], keys[2]
    conn.cur.description = [SimpleNamespace(name=key) for key in keys]
    with pytest.raises(ValueError, match="column order"):
        module.load_ai_dataset()
    assert conn.rolled_back


def test_short_database_row_rejected(setup):
    module, conn = setup
    fake_result(conn, sample(module))
    conn.cur.rows = [conn.cur.rows[0][:-1]]
    with pytest.raises(ValueError, match="incomplete database row"):
        module.load_ai_dataset()
    assert conn.rolled_back


def test_load_training_matrix_runs_readonly_query(setup):
    module, conn = setup
    fake_result(conn, sample(module, 26, True))
    result = module.load_training_matrix(26, case_ref="MOD0001")
    assert len(result["X"][0]) == 26
    assert result["y"] == [-4.25]
    assert "fyp_ai_train_mod_26" in conn.cur.queries[1][0]
    assert conn.cur.queries[1][1] == ("MOD0001",)
