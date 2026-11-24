from copy import deepcopy
from decimal import Decimal

import pytest

from services.iso_value_mapper import (
    ISO_PRE_FEATURES,
    extract_iso_values,
    iso_pre_feature_record,
    map_iso_values,
)


SOURCES = (
    ("lift_landing_floors", "common", "specification", "Lift parameters", "C15", 45),
    ("counterbalance_percent", "common", "specification", "Lift parameters", "C9", 50),
    ("acceleration_m_s2", "common", "specification", "Lift parameters", "C10", 0.83),
    ("jerk_m_s3", "common", "specification", "Lift parameters", "C11", 1.4),
    ("door_time_s", "common", "specification", "Lift parameters", "C12", 12.36),
    ("daily_trips_actual", "existing", "measured", "Data measured", "C4", 511),
    ("reference_energy_wh", "existing", "measured", "Data measured", "C10", 490),
    ("short_cycle_distance_m", "existing", "measured", "Data measured", "C11", 109.2),
    ("short_energy_wh", "existing", "measured", "Data measured", "C12", 345),
    ("idle_power_w", "existing", "measured", "Data measured", "C13", 1050),
    ("standby5_power_w", "existing", "measured", "Data measured", "C6", 920),
    ("standby30_power_w", "existing", "measured", "Data measured", "C8", 720),
    ("reference_main_energy_wh", "existing", "measured", "Data measured", "C19", 484.5),
    ("reference_ancillary_energy_wh", "existing", "measured", "Data measured", "C20", 5.5),
    ("short_main_energy_wh", "existing", "measured", "Data measured", "C21", 340.5),
    ("short_ancillary_energy_wh", "existing", "measured", "Data measured", "C22", 4.5),
    ("energy_per_m_wh", "existing", "calculated", "Calculation", "C3", 0.8155230596175479),
    ("start_stop_energy_wh", "existing", "calculated", "Calculation", "C4", 83.44488188976376),
    ("average_cycle_energy_wh", "existing", "calculated", "Calculation", "C5", 280.1957945597709),
    ("daily_running_energy_wh", "existing", "calculated", "Calculation", "C6", 99042.20848201502),
    ("daily_nonrunning_energy_wh", "existing", "calculated", "Calculation", "C10", 15842.855640117288),
    ("annual_energy_kwh", "existing", "calculated", "Classification", "C16", 41933.048404578294),
    ("efficiency_class", "existing", "calculated", "Classification", "C17", "C"),
)


@pytest.fixture
def iso_records():
    result = []
    for name, phase, kind, sheet, cell, value in SOURCES:
        field = f"before_{name}" if phase == "existing" else name
        result.append({
            "field": field,
            "value": value,
            "kind": kind,
            "sheet": sheet,
            "cell": cell,
            "unit": None,
            "label": name.replace("_", " "),
            "note": "",
            "formula": None,
        })
    result.append({
        "field": "after_annual_energy_kwh",
        "value": 22084.069938453213,
        "kind": "calculated",
        "sheet": "Classification",
        "cell": "D16",
        "formula": "=D15/1000",
        "note": "",
    })
    result.append({
        "field": "before_standby5_time_s",
        "value": 60,
        "kind": "measured",
        "sheet": "Data measured",
        "cell": "C7",
        "note": "Enter EE report, Page 5 / Synthetic Before | Before: 60 s assumed.",
    })
    return result


@pytest.fixture
def mapped(iso_records):
    return map_iso_values(iso_records)


EXPECTED_FEATURES = tuple("""
ISO_PRE_landing_floors ISO_PRE_counterbalance_pct ISO_PRE_acceleration_mps2
ISO_PRE_jerk_mps3 ISO_PRE_door_operation_time_s ISO_PRE_daily_trips
ISO_PRE_reference_cycle_wh ISO_PRE_short_cycle_distance_m
ISO_PRE_short_cycle_wh ISO_PRE_idle_power_w ISO_PRE_standby_5min_w
ISO_PRE_standby_30min_w ISO_PRE_reference_main_wh
ISO_PRE_reference_ancillary_wh ISO_PRE_short_main_wh
ISO_PRE_short_ancillary_wh ISO_PRE_running_energy_wh_per_m
ISO_PRE_start_stop_energy_wh ISO_PRE_average_cycle_wh
ISO_PRE_running_wh_day ISO_PRE_nonrunning_wh_day
ISO_PRE_annual_energy_kwh_year ISO_PRE_energy_class
""".split())


def test_exact_23_feature_names(mapped):
    assert ISO_PRE_FEATURES == EXPECTED_FEATURES
    assert tuple(iso_pre_feature_record(mapped)) == EXPECTED_FEATURES
    assert len(ISO_PRE_FEATURES) == 23


def test_complete_49_feature_contract():
    from services.mod_value_mapper import MOD_FEATURES

    names = MOD_FEATURES + ISO_PRE_FEATURES
    assert len(names) == len(set(names)) == 49
    assert names[26] == "ISO_PRE_landing_floors"
    assert names[-1] == "ISO_PRE_energy_class"


def test_actual_workbook_before_values(mapped):
    features = iso_pre_feature_record(mapped)
    assert features["ISO_PRE_landing_floors"] == 45
    assert features["ISO_PRE_daily_trips"] == 511
    assert features["ISO_PRE_reference_cycle_wh"] == 490
    assert features["ISO_PRE_standby_5min_w"] == 920
    assert features["ISO_PRE_annual_energy_kwh_year"] == Decimal("41933.048404578294")
    assert features["ISO_PRE_energy_class"] == "C"


def test_after_phase_is_never_used_as_predictor(mapped):
    features = iso_pre_feature_record(mapped)
    assert features["ISO_PRE_annual_energy_kwh_year"] > 40000
    after = next(value for value in mapped if value["phase"] == "proposed")
    assert after["numeric_value"] < 23000
    after["numeric_value"] = Decimal("999999")
    assert iso_pre_feature_record(mapped) == features


def test_common_and_existing_phases(mapped):
    index = {(item["phase"], item["field_name"]): item for item in mapped}
    assert index[("common", "counterbalance_percent")]["excel_cell"] == "C9"
    assert index[("existing", "annual_energy_kwh")]["excel_cell"] == "C16"
    assert index[("proposed", "annual_energy_kwh")]["excel_cell"] == "D16"


def test_formula_and_cached_result_preserved(mapped):
    entry = next(x for x in mapped if x["phase"] == "proposed")
    assert entry["excel_formula"] == "=D15/1000"
    assert entry["raw_source_value"] == "=D15/1000"
    assert entry["numeric_value"] == Decimal("22084.069938453213")


def test_source_note_marks_synthetic_before(mapped):
    entry = next(x for x in mapped if x["field_name"] == "standby5_time_s")
    assert entry["value_kind"] == "synthetic_input"
    assert "Synthetic Before" in entry["source_note"]


def test_default_does_not_assert_source_verification(mapped):
    assert all(x["source_verified"] is False for x in mapped)
    assert all(x["is_corrected"] is False for x in mapped)
    assert all(x["value_kind"] != "meter_measurement" for x in mapped)


def test_before_synthetic_flag_marks_measurement_inputs(iso_records):
    mapped = map_iso_values(iso_records, synthetic_before=True)
    before = [x for x in mapped if x["phase"] == "existing"]
    assert any(x["value_kind"] == "synthetic_input" for x in before)
    with pytest.raises(ValueError, match="Synthetic ISO Before input"):
        iso_pre_feature_record(mapped)
    assert len(iso_pre_feature_record(mapped, allow_synthetic=True)) == 23


def test_missing_before_feature_rejected(mapped):
    filtered = [x for x in mapped if x["field_name"] != "daily_trips_actual"]
    with pytest.raises(ValueError, match="Missing ISO Before feature source"):
        iso_pre_feature_record(filtered)


def test_after_only_cannot_replace_before(mapped):
    filtered = [x for x in mapped if not (
        x["phase"] == "existing" and x["field_name"] == "annual_energy_kwh"
    )]
    with pytest.raises(ValueError, match="Missing ISO Before feature source"):
        iso_pre_feature_record(filtered)


def test_missing_numeric_feature_rejected(mapped):
    changed = deepcopy(mapped)
    target = next(x for x in changed if x["field_name"] == "idle_power_w")
    target["numeric_value"] = None
    with pytest.raises(ValueError, match="Missing ISO Before numeric"):
        iso_pre_feature_record(changed)


def test_missing_energy_class_rejected(mapped):
    changed = deepcopy(mapped)
    target = next(x for x in changed if x["field_name"] == "efficiency_class")
    target["text_value"] = None
    with pytest.raises(ValueError, match="Missing ISO Before energy class"):
        iso_pre_feature_record(changed)


def test_duplicate_field_rejected(iso_records):
    with pytest.raises(ValueError, match="Duplicate ISO field"):
        map_iso_values(iso_records + [iso_records[0]])


def test_duplicate_mapped_field_rejected(mapped):
    with pytest.raises(ValueError, match="Duplicate ISO field"):
        iso_pre_feature_record(mapped + [mapped[0]])


@pytest.mark.parametrize("invalid", ["not-a-number", "NaN", "Infinity", True])
def test_invalid_numeric_feature_rejected(mapped, invalid):
    changed = deepcopy(mapped)
    target = next(x for x in changed if x["field_name"] == "idle_power_w")
    target["numeric_value"] = invalid
    with pytest.raises(ValueError, match="ISO numeric|ISO values"):
        iso_pre_feature_record(changed)


@pytest.mark.parametrize("field", ["kind", "field"])
def test_missing_required_extractor_key_rejected(iso_records, field):
    records = deepcopy(iso_records)
    records[0].pop(field)
    with pytest.raises(ValueError, match="ISO extraction record|Unsupported ISO value kind"):
        map_iso_values(records)


def test_invalid_formula_rejected(iso_records):
    records = deepcopy(iso_records)
    records[0]["formula"] = "SUM(C2:C3)"
    with pytest.raises(ValueError, match="formula"):
        map_iso_values(records)


def test_invalid_record_rejected():
    with pytest.raises(ValueError, match="nonempty list"):
        map_iso_values([])
    with pytest.raises(ValueError, match="Invalid ISO extraction record"):
        map_iso_values([None])


@pytest.mark.parametrize("value", ["true", None, 1])
def test_synthetic_before_must_be_bool(iso_records, value):
    with pytest.raises(ValueError, match="Boolean"):
        map_iso_values(iso_records, synthetic_before=value)


def test_stored_values_compatible_with_repository(mapped, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from types import ModuleType

    database = ModuleType("database")
    database.get_connection = lambda: None
    monkeypatch.setitem(sys.modules, "database", database)
    path = Path(__file__).resolve().parents[1] / "services" / "engineering_value_repository.py"
    spec = importlib.util.spec_from_file_location("_isolated_iso_values", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for row in mapped:
        module._prepare_value(row)


def test_extraction_wrapper_calls_existing_iso_extractor(monkeypatch, iso_records):
    import sys
    from types import ModuleType

    extractor = ModuleType("services.iso_excel_extractor")
    calls = []

    def extract(data):
        calls.append(data)
        return iso_records

    extractor.extract_iso_excel = extract
    monkeypatch.setitem(sys.modules, "services.iso_excel_extractor", extractor)
    result = extract_iso_values(b"PK-test")
    assert len(result) == len(iso_records)
    assert calls == [b"PK-test"]


def test_units_are_preserved_or_supported(mapped):
    index = {(x["phase"], x["field_name"]): x for x in mapped}
    assert index[("common", "acceleration_m_s2")]["unit"] == "m/s^2"
    assert index[("common", "counterbalance_percent")]["unit"] == "%"
    assert index[("existing", "energy_per_m_wh")]["unit"] == "Wh/m"


@pytest.mark.parametrize("field", ["lift_landing_floors", "annual_energy_kwh"])
def test_required_positive_before_features(mapped, field):
    changed = deepcopy(mapped)
    entry = next(x for x in changed if x["field_name"] == field)
    entry["numeric_value"] = 0
    with pytest.raises(ValueError, match="Invalid ISO Before feature value"):
        iso_pre_feature_record(changed)


def test_negative_before_input_rejected(mapped):
    changed = deepcopy(mapped)
    entry = next(x for x in changed if x["field_name"] == "idle_power_w")
    entry["numeric_value"] = -1
    with pytest.raises(ValueError, match="Invalid ISO Before feature value"):
        iso_pre_feature_record(changed)


def test_invalid_efficiency_class_rejected(mapped):
    changed = deepcopy(mapped)
    entry = next(x for x in changed if x["field_name"] == "efficiency_class")
    entry["text_value"] = "Unknown"
    with pytest.raises(ValueError, match="Invalid ISO Before energy class"):
        iso_pre_feature_record(changed)


def test_class_prefix_is_normalised(mapped):
    changed = deepcopy(mapped)
    entry = next(x for x in changed if x["field_name"] == "efficiency_class")
    entry["text_value"] = "Class C"
    assert iso_pre_feature_record(changed)["ISO_PRE_energy_class"] == "C"
