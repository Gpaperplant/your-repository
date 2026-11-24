from copy import deepcopy
from decimal import Decimal

import pytest

from services.mod_value_mapper import (
    MOD_FEATURES,
    extract_mod_values,
    map_mod_values,
    mod_feature_record,
)


@pytest.fixture
def comparison():
    return {
        "source_page": 4,
        "currency_symbol": "$",
        "lifetime_years": 25,
        "rows": {
            "existing": {
                "elevator_technology": "Typical Traction Elevator",
                "annual_energy_consumption": Decimal("4213"),
                "annual_regenerative_energy": None,
                "annual_electricity_expenditure": 1138,
                "lifetime_electricity_expenditure": 36450,
                "operational_co2e": 40661,
                "embodied_co2e": None,
            },
            "proposed": {
                "elevator_technology": "ReGenerate 400 DX",
                "annual_energy_consumption": Decimal("1993"),
                "annual_regenerative_energy": Decimal("450"),
                "annual_electricity_expenditure": 538,
                "lifetime_electricity_expenditure": 17232,
                "operational_co2e": 19230,
                "embodied_co2e": 0,
            },
        },
        "reported_energy_saving_percent": Decimal("53"),
        "calculated_energy_saving_percent": Decimal("52.69"),
    }


@pytest.fixture
def specifications():
    rows = [
        ("existing_nominal_load_kg", "630", "kg", 8),
        ("proposed_nominal_load_kg", "630", "kg", 8),
        ("existing_nominal_speed_m_s", "1", "m/s", 8),
        ("proposed_nominal_speed_m_s", "1.6", "m/s", 8),
        ("travel_distance_m", "15", "m", 8),
        ("served_floors", "6", None, 8),
        ("starts_per_year", "273750", "starts/year", 8),
        ("operating_days_per_year", "365", "days/year", 8),
        ("iso_usage_category", "4", None, 8),
        ("existing_gear", "Geared", None, 8),
        ("proposed_gear", "Gearless", None, 8),
        ("existing_drive_type", "V3F induction", None, 8),
        ("proposed_drive_type", "V3F PMSM", None, 8),
        ("existing_roping_suspension_type", "1:1", None, 8),
        ("proposed_roping_suspension_type", "2:1", None, 8),
        ("existing_regenerative_drive", "No", None, 8),
        ("proposed_regenerative_drive", "Yes", None, 8),
        ("p5_existing_lighting_kwh", "125", "kWh/year", 5),
        ("p5_proposed_lighting_kwh", "80", "kWh/year", 5),
        ("contact_email", "private@example.com", None, 2),
    ]
    return rows


@pytest.fixture
def mapped(comparison, specifications):
    return map_mod_values(comparison, specifications)


EXPECTED_FEATURES = tuple("""
MOD_rated_load_kg MOD_speed_existing_mps MOD_speed_proposed_mps
MOD_travel_distance_m MOD_served_floors MOD_starts_per_year
MOD_operating_days_per_year MOD_usage_category MOD_gear_existing
MOD_gear_proposed MOD_drive_type_existing MOD_drive_type_proposed
MOD_motor_technology_existing MOD_motor_technology_proposed
MOD_roping_existing MOD_roping_proposed MOD_regeneration_existing
MOD_regeneration_proposed MOD_PROXY_energy_existing_kwh_year
MOD_PROXY_energy_proposed_kwh_year MOD_PROXY_savings_pct
DERIVED_speed_increase_mps DERIVED_speed_increase_pct
DERIVED_induction_to_pmsm DERIVED_geared_to_gearless
DERIVED_regeneration_added
""".split())


def test_exact_26_feature_columns(mapped):
    assert MOD_FEATURES == EXPECTED_FEATURES
    record = mod_feature_record(mapped)
    assert tuple(record) == MOD_FEATURES
    assert len(record) == 26


def test_derived_features_match_enercal(mapped):
    record = mod_feature_record(mapped)
    assert record["MOD_rated_load_kg"] == 630
    assert record["MOD_speed_existing_mps"] == 1
    assert record["MOD_speed_proposed_mps"] == Decimal("1.6")
    assert record["DERIVED_speed_increase_mps"] == Decimal("0.6")
    assert record["DERIVED_speed_increase_pct"] == Decimal("60")
    assert record["DERIVED_induction_to_pmsm"] == 1
    assert record["DERIVED_geared_to_gearless"] == 1
    assert record["DERIVED_regeneration_added"] == 1
    assert abs(record["MOD_PROXY_savings_pct"] - Decimal("52.69404225")) < Decimal("0.00000001")


def test_stored_rows_are_revision_repository_compatible(mapped, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    from types import ModuleType

    database = ModuleType("database")
    database.get_connection = lambda: None
    monkeypatch.setitem(sys.modules, "database", database)
    path = Path(__file__).resolve().parents[1] / "services" / "engineering_value_repository.py"
    spec = importlib.util.spec_from_file_location("_isolated_values", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for value in mapped:
        module._prepare_value(value)


def test_preserves_existing_and_proposed_phases(mapped):
    data = {(x["phase"], x["field_name"]): x for x in mapped}
    assert data[("existing", "annual_energy_consumption")]["source_page"] == 4
    assert data[("proposed", "annual_energy_consumption")]["numeric_value"] == 1993
    assert data[("existing", "p5_lighting_kwh")]["source_page"] == 5
    assert data[("proposed", "p5_lighting_kwh")]["numeric_value"] == 80
    assert data[("common", "travel_distance_m")]["source_page"] == 8
    assert len(data) == len(mapped)


def test_savings_keeps_reported_and_calculated_separate(mapped):
    data = {(x["phase"], x["field_name"]): x for x in mapped}
    assert data[("common", "reported_energy_saving_percent")]["numeric_value"] == 53
    assert data[("common", "calculated_energy_saving_percent")]["numeric_value"] == Decimal("52.69")
    assert data[("common", "reported_energy_saving_percent")]["value_kind"] == "reported_estimate"


def test_optional_na_is_not_invented(mapped):
    data = {(x["phase"], x["field_name"]): x for x in mapped}
    assert data[("existing", "annual_regenerative_energy")]["numeric_value"] is None
    assert data[("existing", "annual_regenerative_energy")]["raw_source_value"] == "N/A"


def test_no_personal_contact_data(mapped):
    assert not any("contact_" in x["field_name"] for x in mapped)
    assert not any("private@example.com" == x.get("text_value") for x in mapped)


def test_derived_fields_not_persisted(mapped):
    assert not any(x["field_name"].startswith("DERIVED_") for x in mapped)
    assert not any(x["field_name"].startswith("MOD_") for x in mapped)


def test_defaults_do_not_claim_verification(mapped):
    assert all(x["source_verified"] is False for x in mapped)
    assert all(x["is_corrected"] is False for x in mapped)


def test_missing_comparison_field_rejected(comparison, specifications):
    del comparison["rows"]["existing"]["annual_energy_consumption"]
    with pytest.raises(ValueError, match="Missing MOD comparison field"):
        map_mod_values(comparison, specifications)


def test_missing_mod_feature_rejected(mapped):
    partial = [x for x in mapped if x["field_name"] != "starts_per_year"]
    with pytest.raises(ValueError, match="Missing MOD feature source"):
        mod_feature_record(partial)


def test_duplicate_phase_field_rejected(comparison, specifications):
    specifications.append(specifications[0])
    with pytest.raises(ValueError, match="Duplicate MOD field"):
        map_mod_values(comparison, specifications)


def test_unknown_motor_does_not_get_invented(mapped):
    bad = deepcopy(mapped)
    for item in bad:
        if item["phase"] == "existing" and item["field_name"] == "drive_type":
            item["text_value"] = "Unknown VVVF"
    with pytest.raises(ValueError, match="Cannot infer motor technology"):
        mod_feature_record(bad)


def test_invalid_regeneration_rejected(mapped):
    bad = deepcopy(mapped)
    for item in bad:
        if item["phase"] == "proposed" and item["field_name"] == "regenerative_drive":
            item["text_value"] = "Maybe"
    with pytest.raises(ValueError, match="regenerative-drive"):
        mod_feature_record(bad)


@pytest.mark.parametrize("field,bad_value", [
    ("annual_energy_consumption", 0),
    ("annual_energy_consumption", -5),
])
def test_existing_annual_energy_must_be_positive(mapped, field, bad_value):
    for item in mapped:
        if item["phase"] == "existing" and item["field_name"] == field:
            item["numeric_value"] = Decimal(bad_value)
    with pytest.raises(ValueError, match="annual energy"):
        mod_feature_record(mapped)


def test_wrong_source_page_rejected(comparison, specifications):
    comparison["source_page"] = 3
    with pytest.raises(ValueError, match="page 4"):
        map_mod_values(comparison, specifications)


def test_invalid_numeric_spec_rejected(comparison, specifications):
    specifications[0] = ("existing_nominal_load_kg", "unknown", "kg", 8)
    with pytest.raises(ValueError, match="Invalid MOD number"):
        map_mod_values(comparison, specifications)


def test_pdf_integration_when_sample_available():
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "sample_data" / "EnerCal_MOD_05_11_2026.pdf"
    if not path.exists():
        pytest.skip("Sample MOD PDF not installed in this runtime.")
    mapped = extract_mod_values(path.read_bytes())
    record = mod_feature_record(mapped)
    assert len(record) == 26
    assert record["MOD_rated_load_kg"] == 630
    assert record["MOD_PROXY_energy_existing_kwh_year"] == 4213
    assert record["MOD_PROXY_energy_proposed_kwh_year"] == 1993
