"""Extract ISO lift calculations, including before and after columns."""

from datetime import date, datetime
from io import BytesIO
import math
import re

from openpyxl import load_workbook


# Field positions in supported ISO workbook layouts.
FIELDS = {
    "Test parameters": {
        2: "test_date", 3: "customer", 4: "testing_company", 5: "site_address",
        6: "lift_location", 7: "instrument_model", 9: "calibration_date",
    },
    "Lift parameters": {
        2: "lift_id", 3: "installation_date", 4: "lift_model",
        5: "rated_load_kg", 6: "rated_speed_m_s", 7: "travel_m",
        8: "stops", 9: "counterbalance_percent", 10: "acceleration_m_s2",
        11: "jerk_m_s3", 12: "door_time_s", 13: "operating_days",
        14: "lift_express_zone_m", 15: "lift_landing_floors",
        16: "lift_average_floor_spacing_m",
    },
    "Data measured": {
        4: "daily_trips_actual", 5: "iso_usage_category",
        6: "standby5_power_w", 7: "standby5_time_s",
        8: "standby30_power_w", 9: "standby30_time_s",
        10: "reference_energy_wh", 11: "short_cycle_distance_m",
        12: "short_energy_wh", 13: "idle_power_w", 14: "idle_time_s",
        15: "reference_cycle_count", 16: "short_cycle_count",
        17: "standby_entry_time_s", 18: "standby_recovery_time_s",
        19: "reference_main_energy_wh", 20: "reference_ancillary_energy_wh",
        21: "short_main_energy_wh", 22: "short_ancillary_energy_wh",
    },
    "Data from tables": {
        2: "average_travel_ratio", 3: "average_load_percent", 4: "load_factor",
        8: "daily_trips_iso_assumed", 9: "express_zone_m", 10: "landing_floors",
        11: "express_trip_fraction", 12: "average_floor_spacing_m",
        13: "adjusted_average_travel_m", 15: "idle_time_ratio",
        16: "standby5_time_ratio", 17: "standby30_time_ratio",
        18: "time_ratio_sum_percent",
    },
    "Calculation": {
        2: "average_travel_m", 3: "energy_per_m_wh", 4: "start_stop_energy_wh",
        5: "average_cycle_energy_wh", 6: "daily_running_energy_wh",
        7: "average_trip_time_s", 8: "daily_running_hours",
        9: "daily_nonrunning_hours", 10: "daily_nonrunning_energy_wh",
        11: "daily_energy_wh",
    },
    "Classification": {
        2: "avg_running_specific_mwh_kg_m", 3: "idle_standby_description",
        4: "specific_reference_mwh_kg_m", 7: "class_a_limit_wh",
        8: "class_b_limit_wh", 9: "class_c_limit_wh",
        6: "running_energy_performance_level",
        10: "class_d_limit_wh", 11: "class_e_limit_wh",
        12: "class_f_limit_wh", 20: "idle_power_performance_level",
        21: "standby5_performance_level", 22: "standby30_performance_level",
    },
    "ISO Scenarios": {
        5: "scenario_daily_trips", 6: "scenario_usage_category",
        7: "scenario_calculation_mode", 8: "scenario_assumed_trips",
        10: "scenario_average_travel_ratio", 11: "scenario_average_load_percent",
        12: "scenario_load_factor", 13: "scenario_express_trip_fraction",
        14: "scenario_average_travel_m", 16: "scenario_idle_time_ratio",
        17: "scenario_standby5_ratio", 18: "scenario_standby30_ratio",
        19: "scenario_total_time_ratio",
    },
    "ISO 2023 OP13": {
        3: "recon_actual_trips", 4: "recon_daily_trips_iso_assumed",
        5: "recon_express_zone_m", 6: "recon_landing_floors",
        7: "recon_average_floor_spacing_m", 8: "recon_express_trip_fraction",
        9: "recon_average_travel_ratio", 10: "recon_travel_m",
        11: "recon_average_travel_m", 13: "recon_annual_energy_kwh",
        14: "tuv_annual_energy_kwh_copy", 15: "annual_difference_kwh",
        16: "annual_difference_ratio", 17: "reconciliation_status",
    },
}

# The two-column workbook places totals in rows 14-18, not row 13.
CLASSIFICATION_DUAL = {
    14: "classification_daily_wh", 15: "annual_energy_wh",
    16: "annual_energy_kwh", 17: "efficiency_class",
    18: "energy_saving_percent",
}
CLASSIFICATION_SINGLE = {
    11: "classification_daily_wh", 13: "annual_energy_wh",
}

UNITS = {
    "rated_load_kg": "kg", "rated_speed_m_s": "m/s", "travel_m": "m",
    "lift_express_zone_m": "m", "lift_average_floor_spacing_m": "m",
    "short_cycle_distance_m": "m", "express_zone_m": "m",
    "average_travel_m": "m", "adjusted_average_travel_m": "m",
    "reference_energy_wh": "Wh", "short_energy_wh": "Wh",
    "reference_main_energy_wh": "Wh/cycle", "reference_ancillary_energy_wh": "Wh/cycle",
    "short_main_energy_wh": "Wh/cycle", "short_ancillary_energy_wh": "Wh/cycle",
    "reference_cycle_count": "cycles", "short_cycle_count": "cycles",
    "standby_entry_time_s": "s", "standby_recovery_time_s": "s",
    "daily_energy_wh": "Wh/day", "classification_daily_wh": "Wh/day",
    "class_a_limit_wh": "Wh/day", "class_b_limit_wh": "Wh/day",
    "class_c_limit_wh": "Wh/day", "class_d_limit_wh": "Wh/day",
    "class_e_limit_wh": "Wh/day", "class_f_limit_wh": "Wh/day",
    "annual_energy_wh": "Wh/year", "annual_energy_kwh": "kWh/year",
    "daily_running_energy_wh": "Wh/day", "daily_nonrunning_energy_wh": "Wh/day",
    "tuv_annual_energy_kwh_copy": "kWh/year", "annual_difference_kwh": "kWh/year",
    "recon_annual_energy_kwh": "kWh/year",
    "idle_power_w": "W", "standby5_power_w": "W", "standby30_power_w": "W",
    "daily_running_hours": "h/day", "daily_nonrunning_hours": "h/day",
    "daily_trips_actual": "trips/day", "daily_trips_iso_assumed": "trips/day",
    "energy_saving_percent": "%", "average_floor_spacing_m": "m",
    "avg_running_specific_mwh_kg_m": "mWh/(kg·m)",
    "specific_reference_mwh_kg_m": "mWh/(kg·m)",
    "idle_time_ratio": "%", "standby5_time_ratio": "%", "standby30_time_ratio": "%",
    "time_ratio_sum_percent": "%", "scenario_total_time_ratio": "%",
}

REPORTED = {"iso_usage_category", "tuv_annual_energy_kwh_copy"}

SPECIFICATIONS = {
    "lift_id", "lift_model", "rated_load_kg", "rated_speed_m_s", "travel_m",
    "stops", "counterbalance_percent", "acceleration_m_s2", "jerk_m_s3",
    "door_time_s", "express_zone_m", "landing_floors",
    "lift_express_zone_m", "lift_landing_floors", "lift_average_floor_spacing_m",
}
ASSUMPTIONS = {
    "operating_days", "average_travel_ratio", "average_load_percent",
    "daily_trips_iso_assumed", "express_trip_fraction", "idle_time_ratio",
    "standby5_time_ratio", "standby30_time_ratio",
}


def _kind(sheet_name, field):
    if field == "lift_id":
        return "identity"
    if field in REPORTED:
        return "reported"
    if field in ASSUMPTIONS or sheet_name == "Data from tables":
        return "calculated" if field == "load_factor" else "assumed"
    if field in SPECIFICATIONS:
        return "specification"
    if field == "recon_actual_trips" or sheet_name == "Data measured":
        return "measured"
    if sheet_name in ("Calculation", "Classification", "ISO Scenarios", "ISO 2023 OP13"):
        return "calculated"
    return "metadata"


def _number(values, key):
    value = values.get(key)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _check_calculations(records, dual=False):
    values = {item["field"]: item["value"] for item in records}
    required = ["lift_id", "rated_load_kg", "rated_speed_m_s", "travel_m"]
    energy_fields = (
        "daily_running_energy_wh", "daily_nonrunning_energy_wh",
        "daily_energy_wh", "annual_energy_wh",
    )
    if dual:
        required.append("operating_days")
        for prefix in ("before_", "after_"):
            required.extend(prefix + name for name in energy_fields)
            required.append(prefix + "annual_energy_kwh")
    else:
        required.extend(("daily_energy_wh", "annual_energy_wh"))

    missing = [key for key in required if values.get(key) is None]
    if missing:
        raise ValueError(
            "Missing Excel values: " + ", ".join(missing)
            + ". Recalculate and save the workbook in Excel."
        )

    for key in required:
        if key == "lift_id":
            continue
        number = _number(values, key)
        if number is None or number < 0:
            raise ValueError(f"Invalid Excel numeric value: {key}.")
        if key in {"rated_load_kg", "rated_speed_m_s", "travel_m", "operating_days"} and number == 0:
            raise ValueError(f"Excel value {key} must be positive.")

    for prefix in (("before_", "after_") if dual else ("",)):
        checks = (
            ("daily_energy_wh", "daily_running_energy_wh", "daily_nonrunning_energy_wh", lambda a, b: a + b),
            ("annual_energy_wh", "daily_energy_wh", "operating_days", lambda a, b: a * b),
            ("annual_energy_kwh", "annual_energy_wh", None, lambda a, _: a / 1000),
            ("classification_daily_wh", "daily_energy_wh", None, lambda a, _: a),
        )
        for output, left, right, calculate in checks:
            field = prefix + output
            if field not in values:
                continue
            first = _number(values, prefix + left)
            right_key = right if right == "operating_days" else prefix + right if right else None
            second = _number(values, right_key) if right_key else None
            actual = _number(values, field)
            if actual is None:
                raise ValueError(f"Invalid Excel calculation inputs: {field}.")
            if first is None or (right and second is None):
                if dual:
                    raise ValueError(f"Invalid Excel calculation inputs: {field}.")
                continue
            if not math.isclose(actual, calculate(first, second), abs_tol=0.1, rel_tol=1e-6):
                raise ValueError(
                    f"Excel result {field} is inconsistent. "
                    "Recalculate and save the workbook in Excel."
                )

        # Check additional energy breakdowns when the workbook supplies them.
        for total, main, ancillary in (
            ("reference_energy_wh", "reference_main_energy_wh", "reference_ancillary_energy_wh"),
            ("short_energy_wh", "short_main_energy_wh", "short_ancillary_energy_wh"),
        ):
            keys = tuple(prefix + key for key in (total, main, ancillary))
            if not all(key in values for key in keys):
                continue
            a, b, c = (_number(values, key) for key in keys)
            if None in (a, b, c) or not math.isclose(a, b + c, abs_tol=0.1, rel_tol=1e-6):
                raise ValueError(f"Excel result {keys[0]} is inconsistent.")

    if dual:
        before = _number(values, "before_annual_energy_kwh")
        after = _number(values, "after_annual_energy_kwh")
        if before <= 0:
            raise ValueError("Before annual energy must be greater than zero.")
        expected = 100 * (before - after) / before
        for key in ("after_energy_saving_percent", "after_calculation_energy_saving_percent"):
            if key not in values:
                continue
            actual = _number(values, key)
            if actual is None or not math.isclose(actual, expected, abs_tol=0.2, rel_tol=1e-5):
                raise ValueError(f"Excel saving percentage {key} is inconsistent.")

        # Annual energy must agree across Calculation and Classification sheets.
        for prefix in ("before_", "after_"):
            key = prefix + "calculation_annual_energy_kwh"
            if key in values:
                actual = _number(values, key)
                expected = _number(values, prefix + "annual_energy_kwh")
                if actual is None or expected is None or not math.isclose(
                    actual, expected, abs_tol=0.1, rel_tol=1e-6
                ):
                    raise ValueError(f"Excel result {key} is inconsistent.")

        for prefix in ("before_", "after_"):
            fields = [prefix + key for key in (
                "scenario_idle_time_ratio", "scenario_standby5_ratio",
                "scenario_standby30_ratio", "scenario_total_time_ratio",
            )]
            if all(key in values for key in fields):
                parts = [_number(values, key) for key in fields]
                if any(part is None for part in parts) or not math.isclose(
                    sum(parts[:3]), parts[3], abs_tol=0.2
                ) or not math.isclose(parts[3], 100, abs_tol=0.2):
                    raise ValueError(f"Excel scenario time ratios are inconsistent: {prefix[:-1]}.")


def _two_columns(sheet_name, sheet):
    header_row = {"Data measured": 3, "ISO Scenarios": 4}.get(sheet_name, 1)
    header = str(sheet[f"D{header_row}"].value or "").strip().lower()
    return header in ("input after", "after")


def extract_iso_excel(xlsx_bytes):
    """Read original cell values and formulas; keep before/after roles separate."""
    if not xlsx_bytes.startswith(b"PK"):
        raise ValueError("ISO calculation file must be an .xlsx workbook.")
    try:
        cached = load_workbook(BytesIO(xlsx_bytes), data_only=True, keep_links=False)
        original = load_workbook(BytesIO(xlsx_bytes), data_only=False, keep_links=False)
    except Exception as error:
        raise ValueError(f"Cannot read ISO Excel workbook: {error}") from error

    try:
        required = {"Lift parameters", "Data measured", "Calculation", "Classification"}
        missing = sorted(required - set(cached.sheetnames))
        if missing:
            raise ValueError("Missing worksheets: " + ", ".join(missing))

        dual = _two_columns("Calculation", cached["Calculation"])
        results = []
        for sheet_name, base_mapping in FIELDS.items():
            if sheet_name not in cached:
                continue
            sheet, source = cached[sheet_name], original[sheet_name]
            both = dual and sheet_name in {"Data measured", "Calculation", "Classification", "ISO Scenarios"}
            if both:
                columns = (("C", "before_"), ("D", "after_"))
            else:
                column = "B" if sheet_name in ("Test parameters", "ISO 2023 OP13") else "C"
                columns = ((column, ""),)
            mapping = dict(base_mapping)
            if sheet_name == "Classification":
                mapping.update(CLASSIFICATION_DUAL if dual else CLASSIFICATION_SINGLE)
            if sheet_name == "Calculation" and dual:
                mapping.update({13: "calculation_annual_energy_kwh", 14: "calculation_energy_saving_percent"})
            for row in range(2, min(sheet.max_row, 200) + 1):
                if sheet_name == "ISO Scenarios" and row <= 4:
                    continue
                label = sheet[f"A{row}"].value
                if row not in mapping and (not label or not str(label).strip() or row < 4):
                    continue
                field = mapping.get(row) or (
                    "extra." + re.sub(r"[^a-z0-9]+", "_", sheet_name.lower()).strip("_") + f".r{row}"
                )
                for col, prefix in columns:
                    cell = f"{col}{row}"
                    value = sheet[cell].value
                    raw = source[cell].value
                    formula = raw if isinstance(raw, str) and raw.startswith("=") else None
                    if value is None and formula is None:
                        continue
                    if isinstance(value, (date, datetime)):
                        value = value.isoformat()
                    if isinstance(value, str):
                        value = value.strip()
                    notes = [
                        str(sheet[f"{c}{row}"].value)
                        for c in (("E", "F") if both else ("D", "E"))
                        if sheet[f"{c}{row}"].value is not None
                    ]
                    results.append({
                        "field": prefix + field, "value": value,
                        "unit": UNITS.get(field), "kind": _kind(sheet_name, field),
                        "sheet": sheet_name, "cell": cell, "formula": formula,
                        "label": str(label or field).strip(), "note": " | ".join(notes),
                    })
        if not dual:
            # Older workbooks store the class in a separate cell.
            for field, cell in (("efficiency_class", "E7"), ("idle_standby_levels", "E3")):
                value = cached["Classification"][cell].value
                if value is not None:
                    results.append({
                        "field": field, "value": value, "unit": None,
                        "kind": "calculated", "sheet": "Classification", "cell": cell,
                        "formula": None,
                    })
        _check_calculations(results, dual=dual)
        return results
    finally:
        cached.close()
        original.close()
