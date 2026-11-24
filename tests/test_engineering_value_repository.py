import importlib.util
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest


FILE = Path(__file__).resolve().parents[1] / "services/engineering_value_repository.py"


class FakeCursor:
    def __init__(self, revision=("pending", False), rows=(), fail_insert=False):
        self.revision = revision
        self.rows = list(rows)
        self.fail_insert = fail_insert
        self.statements = []
        self.inserted = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.revision

    def fetchall(self):
        return self.rows

    def executemany(self, sql, records):
        self.statements.append((" ".join(sql.split()), None))
        if self.fail_insert:
            raise RuntimeError("Database insertion failed")
        self.inserted = list(records)


class FakeConnection:
    def __init__(self, revision=("pending", False), rows=(), fail_insert=False):
        self.cursor_obj = FakeCursor(revision, rows, fail_insert)
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, error, *_):
        self.committed = error is None
        self.rolled_back = error is not None
        return False

    def cursor(self):
        return self.cursor_obj


@pytest.fixture
def repository(monkeypatch):
    def load(revision=("pending", False), rows=(), fail_insert=False):
        connection = FakeConnection(revision, rows, fail_insert)
        db = ModuleType("database")
        db.get_connection = lambda: connection
        monkeypatch.setitem(sys.modules, "database", db)
        spec = importlib.util.spec_from_file_location("_engineering_under_test", FILE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, connection
    return load


def mod_value(**changes):
    value = {
        "field_name": "annual_energy_kwh",
        "phase": "existing",
        "value_kind": "calculated_estimate",
        "raw_source_value": "4,213",
        "numeric_value": "4213",
        "unit": "kWh/year",
        "source_page": 4,
        "source_verified": True,
    }
    value.update(changes)
    return value


def iso_value(**changes):
    value = {
        "field_name": "annual_energy_kwh",
        "phase": "proposed",
        "value_kind": "calculated_estimate",
        "raw_source_value": "=D11*'Lift parameters'!$C$13/1000",
        "numeric_value": "22084.07",
        "unit": "kWh/year",
        "excel_sheet": "Calculation",
        "excel_cell": "D13",
        "excel_formula": "=D11*'Lift parameters'!$C$13/1000",
        "source_verified": False,
    }
    value.update(changes)
    return value


def test_save_mod_value_preserves_source(repository):
    repo, conn = repository()
    saved = repo.save_engineering_values(7, [mod_value()])
    assert saved == {"revision_id": 7, "values_saved": 1}
    record = dict(zip(repo.INSERT_COLUMNS, conn.cursor_obj.inserted[0]))
    assert record["numeric_value"] == Decimal("4213")
    assert record["raw_source_value"] == "4,213"
    assert record["source_page"] == 4
    assert record["source_verified"] is True
    assert conn.committed


def test_iso_formula_and_cell_preserved(repository):
    repo, conn = repository()
    repo.save_engineering_values(7, [iso_value(excel_cell="d13")])
    record = dict(zip(repo.INSERT_COLUMNS, conn.cursor_obj.inserted[0]))
    assert record["excel_sheet"] == "Calculation"
    assert record["excel_cell"] == "D13"
    assert record["excel_formula"].startswith("=D11*")
    assert record["source_verified"] is False


def test_exact_decimal_value(repository):
    repo, conn = repository()
    repo.save_engineering_values(7, [mod_value(numeric_value="0.123456789123456789")])
    record = dict(zip(repo.INSERT_COLUMNS, conn.cursor_obj.inserted[0]))
    assert record["numeric_value"] == Decimal("0.123456789123456789")


def test_raw_na_without_numeric_value(repository):
    repo, conn = repository()
    repo.save_engineering_values(7, [mod_value(raw_source_value="N/A", numeric_value=None)])
    record = dict(zip(repo.INSERT_COLUMNS, conn.cursor_obj.inserted[0]))
    assert record["raw_source_value"] == "N/A"
    assert record["numeric_value"] is None


def test_text_technology_value(repository):
    repo, conn = repository()
    value = mod_value(
        field_name="electric_motor_type", numeric_value=None,
        text_value="V3F PMSM", raw_source_value="V3F PMSM",
        value_kind="engineering_specification",
    )
    repo.save_engineering_values(7, [value])
    record = dict(zip(repo.INSERT_COLUMNS, conn.cursor_obj.inserted[0]))
    assert record["text_value"] == "V3F PMSM"


def test_two_phases_same_field_permitted(repository):
    repo, conn = repository()
    result = repo.save_engineering_values(
        7, [mod_value(), mod_value(phase="proposed", numeric_value=1993)],
    )
    assert result["values_saved"] == 2
    assert len(conn.cursor_obj.inserted) == 2


def test_iso_synthetic_input_keeps_source_note(repository):
    repo, conn = repository()
    value = iso_value(
        field_name="standby_power_w", phase="existing",
        value_kind="synthetic_input", numeric_value=920,
        source_note="Synthetic Before: standby time assumed",
    )
    repo.save_engineering_values(7, [value])
    record = dict(zip(repo.INSERT_COLUMNS, conn.cursor_obj.inserted[0]))
    assert record["value_kind"] == "synthetic_input"
    assert "assumed" in record["source_note"]


def test_test_measurement_has_no_required_period(repository):
    repo, conn = repository()
    value = iso_value(
        field_name="standby_power_w", value_kind="test_measurement",
        numeric_value=628.47, excel_sheet="Data measured", excel_cell="D6",
        excel_formula=None,
    )
    repo.save_engineering_values(7, [value])
    record = dict(zip(repo.INSERT_COLUMNS, conn.cursor_obj.inserted[0]))
    assert record["period_start"] is None


def test_meter_measurement_accepts_period(repository):
    repo, conn = repository()
    value = mod_value(
        field_name="meter_energy_kwh", value_kind="meter_measurement",
        period_start="2026-01-01", period_end="2026-01-31",
        source_page=None,
    )
    repo.save_engineering_values(7, [value])
    record = dict(zip(repo.INSERT_COLUMNS, conn.cursor_obj.inserted[0]))
    assert record["period_end"] == date(2026, 1, 31)


def test_corrected_value_preserves_original(repository):
    repo, conn = repository()
    value = mod_value(
        raw_source_value="6,689", numeric_value=6900,
        is_corrected=True, correction_reason="Engineer verified actual figure",
    )
    repo.save_engineering_values(7, [value])
    record = dict(zip(repo.INSERT_COLUMNS, conn.cursor_obj.inserted[0]))
    assert record["raw_source_value"] == "6,689"
    assert record["numeric_value"] == Decimal("6900")
    assert record["is_corrected"] is True


@pytest.mark.parametrize("value", [0, -1, True, "7", None])
def test_invalid_revision_id(repository, value):
    repo, conn = repository()
    with pytest.raises(ValueError, match="positive integer"):
        repo.save_engineering_values(value, [mod_value()])
    assert not conn.cursor_obj.statements


@pytest.mark.parametrize("records", [[], (), None, {"field_name": "x"}])
def test_nonlist_or_empty_values_rejected(repository, records):
    repo, conn = repository()
    with pytest.raises(ValueError, match="nonempty list"):
        repo.save_engineering_values(7, records)
    assert not conn.cursor_obj.statements


@pytest.mark.parametrize("name", ["", "Annual Energy", "5start", "a-b", " Field "])
def test_invalid_field_names(repository, name):
    repo, conn = repository()
    with pytest.raises(ValueError, match="Field name"):
        repo.save_engineering_values(7, [mod_value(field_name=name)])
    assert not conn.cursor_obj.statements


@pytest.mark.parametrize("phase", ["before", "after", "old", "", None])
def test_invalid_phase(repository, phase):
    repo, _ = repository()
    with pytest.raises(ValueError, match="phase"):
        repo.save_engineering_values(7, [mod_value(phase=phase)])


@pytest.mark.parametrize("kind", ["measured", "corrected", "", None])
def test_invalid_kind(repository, kind):
    repo, _ = repository()
    with pytest.raises(ValueError, match="value kind"):
        repo.save_engineering_values(7, [mod_value(value_kind=kind)])


@pytest.mark.parametrize("number", ["NaN", "Infinity", "not a number", True])
def test_bad_numbers(repository, number):
    repo, _ = repository()
    with pytest.raises(ValueError, match="Numeric value"):
        repo.save_engineering_values(7, [mod_value(numeric_value=number)])


def test_numeric_and_text_cannot_coexist(repository):
    repo, _ = repository()
    with pytest.raises(ValueError, match="not both"):
        repo.save_engineering_values(7, [mod_value(text_value="unknown")])


def test_missing_all_values_rejected(repository):
    repo, _ = repository()
    with pytest.raises(ValueError, match="At least one"):
        repo.save_engineering_values(7, [mod_value(raw_source_value=None, numeric_value=None)])


def test_duplicate_phase_and_field_rejected_before_write(repository):
    repo, conn = repository()
    with pytest.raises(ValueError, match="Duplicate field"):
        repo.save_engineering_values(7, [mod_value(), mod_value(numeric_value=1993)])
    assert not conn.cursor_obj.statements


@pytest.mark.parametrize("changes", [
    {"excel_sheet": "Calculation"},
    {"excel_cell": "C13"},
    {"excel_sheet": "Calculation", "excel_cell": "A0"},
    {"excel_formula": "=A1*2"},
    {"excel_sheet": "Calculation", "excel_cell": "C13", "excel_formula": "A1*2"},
    {"excel_sheet": "Calculation", "excel_cell": "C13", "source_page": 4},
])
def test_invalid_excel_source_location(repository, changes):
    repo, _ = repository()
    with pytest.raises(ValueError):
        record = (
            mod_value(**changes) if "source_page" in changes
            else mod_value(source_page=None, **changes)
        )
        repo.save_engineering_values(7, [record])


@pytest.mark.parametrize("page", [0, -2, True, "4"])
def test_invalid_source_page(repository, page):
    repo, _ = repository()
    with pytest.raises(ValueError, match="positive integer"):
        repo.save_engineering_values(7, [mod_value(source_page=page)])


@pytest.mark.parametrize("start,end", [
    ("2026-01-02", None),
    (None, "2026-01-02"),
    ("2026-01-10", "2026-01-02"),
    ("2026-13-01", "2026-01-31"),
])
def test_invalid_period(repository, start, end):
    repo, _ = repository()
    with pytest.raises(ValueError, match="period"):
        repo.save_engineering_values(7, [mod_value(period_start=start, period_end=end)])


def test_meter_measurement_requires_period(repository):
    repo, _ = repository()
    with pytest.raises(ValueError, match="Meter measurements"):
        repo.save_engineering_values(7, [mod_value(value_kind="meter_measurement")])


def test_correction_requires_reason(repository):
    repo, _ = repository()
    with pytest.raises(ValueError, match="correction reason"):
        repo.save_engineering_values(7, [mod_value(is_corrected=True)])


@pytest.mark.parametrize("field,value", [
    ("is_corrected", 1), ("source_verified", "yes"),
])
def test_invalid_boolean_flags(repository, field, value):
    repo, _ = repository()
    with pytest.raises(ValueError, match="Boolean"):
        repo.save_engineering_values(7, [mod_value(**{field: value})])


def test_missing_revision_rejected(repository):
    repo, conn = repository(revision=None)
    with pytest.raises(ValueError, match="Revision not found"):
        repo.save_engineering_values(99, [mod_value()])
    assert not conn.cursor_obj.inserted
    assert conn.rolled_back


def test_reviewed_revision_rejected(repository):
    repo, conn = repository(revision=("approved", False))
    with pytest.raises(ValueError, match="pending"):
        repo.save_engineering_values(7, [mod_value()])
    assert not conn.cursor_obj.inserted


def test_existing_snapshot_rejected(repository):
    repo, conn = repository(revision=("pending", True))
    with pytest.raises(ValueError, match="already has"):
        repo.save_engineering_values(7, [mod_value()])
    assert conn.rolled_back


def test_database_error_rolls_back(repository):
    repo, conn = repository(fail_insert=True)
    with pytest.raises(RuntimeError, match="insertion failed"):
        repo.save_engineering_values(7, [mod_value()])
    assert conn.rolled_back
    assert not conn.committed


def test_invalid_second_record_prevents_all_inserts(repository):
    repo, conn = repository()
    with pytest.raises(ValueError, match="phase"):
        repo.save_engineering_values(7, [mod_value(), mod_value(phase="after")])
    assert not conn.cursor_obj.statements


def test_cursor_api_joins_outer_transaction(repository):
    repo, conn = repository()
    result = repo.insert_engineering_values(conn.cursor(), 7, [mod_value()])
    assert result["values_saved"] == 1
    assert conn.committed is False


def test_get_revision_values_returns_named_fields(repository):
    repo, conn = repository()
    row = (5, 7, *[None] * len(repo.VALUE_COLUMNS), "created")
    conn.cursor_obj.rows = [row]
    records = repo.get_revision_values(7)
    assert records[0]["value_id"] == 5
    assert records[0]["revision_id"] == 7
    assert records[0]["created_at"] == "created"
    assert conn.cursor_obj.statements[0][1] == (7,)


def test_get_revision_values_returns_empty_list(repository):
    repo, _ = repository()
    assert repo.get_revision_values(7) == []


def test_get_revision_values_rejects_bad_id(repository):
    repo, conn = repository()
    with pytest.raises(ValueError, match="positive integer"):
        repo.get_revision_values(0)
    assert not conn.cursor_obj.statements


def test_insertion_still_uses_parameterized_sql(repository):
    repo, conn = repository()
    repo.save_engineering_values(7, [mod_value(source_label="O'Brien")])
    sql = conn.cursor_obj.statements[-1][0]
    assert "O'Brien" not in sql
    assert "%s" in sql
    assert "O'Brien" in conn.cursor_obj.inserted[0]
