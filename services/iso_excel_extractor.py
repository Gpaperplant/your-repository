
from datetime import date, datetime
from io import BytesIO
import math
import re

from openpyxl import load_workbook


# Read ISO worksheets only, excluding unrelated examples.
FIELDS = {
    "Test parameters": {
        2: "test_date",
        3: "customer",
        4: "testing_company",
        5: "site_address",
        6: "lift_location",
        7: "instrument_model",
        9: "calibration_date",
    },
    "Lift parameters": {
        2: "lift_id",
        3: "installation_date",
        4: "lift_model",
        5: "rated_load_kg",
        6: "rated_speed_m_s",
        7: "travel_m",
        8: "stops",
        9: "counterbalance_percent",
        10: "acceleration_m_s2",
        11: "jerk_m_s3",
        12: "door_time_s",
        13: "operating_days",
    },
    "Data measured": {
        4: "daily_trips_actual",
        5: "iso_usage_category",
        6: "standby5_power_w",
        7: "standby5_time_s",
        8: "standby30_power_w",
        9: "standby30_time_s",
        10: "reference_energy_wh",
        11: "short_cycle_distance_m",
        12: "short_energy_wh",
        13: "idle_power_w",
        14: "idle_time_s",
    },
    "Data from tables": {
        2: "average_travel_ratio",
        3: "average_load_percent",
        4: "load_factor",
    },
    "Calculation": {
        2: "average_travel_m",
        3: "energy_per_m_wh",
        4: "start_stop_energy_wh",
        5: "average_cycle_energy_wh",
        6: "daily_running_energy_wh",
        7: "average_trip_time_s",
        8: "daily_running_hours",
        9: "daily_nonrunning_hours",
        10: "daily_nonrunning_energy_wh",
        11: "daily_energy_wh",
    },
    "Classification": {
        2: "avg_running_specific_mwh_kg_m",
        3: "idle_standby_description",
        4: "specific_reference_mwh_kg_m",
        7: "class_a_limit_wh",
        8: "class_b_limit_wh",
        9: "class_c_limit_wh",
        11: "classification_daily_wh",
        13: "annual_energy_wh",
    },
    "ISO 2023 OP13": {
        3: "recon_actual_trips",
        4: "daily_trips_iso_assumed",
        5: "express_zone_m",
        6: "landing_floors",
        7: "average_floor_spacing_m",
        8: "express_trip_fraction",
        9: "recon_average_travel_ratio",
        10: "recon_travel_m",
        11: "recon_average_travel_m",
        13: "annual_energy_kwh",
        14: "tuv_annual_energy_kwh_copy",
        15: "annual_difference_kwh",
        16: "annual_difference_ratio",
        17: "reconciliation_status",
    },
}


# Standard units for extracted numerical fields.
UNITS = {
    "rated_load_kg": "kg",
    "rated_speed_m_s": "m/s",
    "travel_m": "m",
    "short_cycle_distance_m": "m",
    "express_zone_m": "m",
    "average_travel_m": "m",
    "reference_energy_wh": "Wh",
    "short_energy_wh": "Wh",
    "daily_energy_wh": "Wh/day",
    "annual_energy_wh": "Wh/year",
    "annual_energy_kwh": "kWh/year",
    "tuv_annual_energy_kwh_copy": "kWh/year",
    "annual_difference_kwh": "kWh/year",
    "daily_running_energy_wh": "Wh/day",
    "daily_nonrunning_energy_wh": "Wh/day",
    "idle_power_w": "W",
    "standby5_power_w": "W",
    "standby30_power_w": "W",
    "daily_running_hours": "h/day",
    "daily_nonrunning_hours": "h/day",
    "daily_trips_actual": "trips/day",
    "recon_actual_trips": "trips/day",
    "daily_trips_iso_assumed": "trips/day",
    "average_floor_spacing_m": "m",
    "recon_travel_m": "m",
    "recon_average_travel_m": "m",
}


SPECIFICATIONS = {
    "lift_id",
    "lift_model",
    "rated_load_kg",
    "rated_speed_m_s",
    "travel_m",
    "stops",
    "counterbalance_percent",
    "acceleration_m_s2",
    "jerk_m_s3",
    "door_time_s",
    "express_zone_m",
    "landing_floors",
    "recon_travel_m",
}

ASSUMPTIONS = {
    "operating_days",
    "average_travel_ratio",
    "average_load_percent",
    "daily_trips_iso_assumed",
    "express_trip_fraction",
    "recon_average_travel_ratio",
}

REPORTED = {
    "iso_usage_category",
    "tuv_annual_energy_kwh_copy",
}


def _kind(sheet, field):
    # Classify each value by its actual origin.
    if field == "lift_id":
        return "identity"

    if field in REPORTED:
        return "reported"

    if field in ASSUMPTIONS or sheet == "Data from tables":
        return "calculated" if field == "load_factor" else "assumed"

    if field in SPECIFICATIONS:
        return "specification"

    if field == "recon_actual_trips" or sheet == "Data measured":
        return "measured"

    if sheet in ("Calculation", "Classification", "ISO 2023 OP13"):
        return "calculated"

    return "metadata"


def _float(index, field):
    # Convert available numerical values for validation.
    try:
        return float(index[field])
    except (KeyError, ValueError, TypeError):
        return None


def _check_calculations(records):
    # Check essential cached values and calculation consistency.
    index = {
        record["field"]: record["value"]
        for record in records
    }

    required = (
        "lift_id",
        "rated_load_kg",
        "rated_speed_m_s",
        "travel_m",
        "daily_energy_wh",
        "annual_energy_wh",
    )

    missing = [
        field for field in required
        if index.get(field) is None
    ]

    if missing:
        raise ValueError(
            "Missing Excel values: "
            + ", ".join(missing)
            + ". Recalculate and save the workbook in Excel."
        )

    checks = (
        (
            "daily_energy_wh",
            "daily_running_energy_wh",
            "daily_nonrunning_energy_wh",
            lambda a, b: a + b,
        ),
        (
            "annual_energy_wh",
            "daily_energy_wh",
            "operating_days",
            lambda a, b: a * b,
        ),
        (
            "annual_energy_kwh",
            "annual_energy_wh",
            None,
            lambda a, _: a / 1000,
        ),
        (
            "classification_daily_wh",
            "daily_energy_wh",
            None,
            lambda a, _: a,
        ),
    )

    for output, left, right, formula in checks:
        actual = _float(index, output)
        first = _float(index, left)
        second = _float(index, right) if right else None

        if actual is None or first is None:
            continue

        if right and second is None:
            continue

        expected = formula(first, second)

        if not math.isclose(
            actual,
            expected,
            abs_tol=0.1,
            rel_tol=1e-6,
        ):
            raise ValueError(
                f"Excel result {output} is inconsistent. "
                "Recalculate and save the workbook in Excel."
            )


def extract_iso_excel(xlsx_bytes):
    # Validate and open the uploaded workbook.
    if not xlsx_bytes.startswith(b"PK"):
        raise ValueError(
            "ISO calculation file must be an .xlsx workbook."
        )

    try:
        cached = load_workbook(
            BytesIO(xlsx_bytes),
            data_only=True,
            keep_links=False,
        )

        original = load_workbook(
            BytesIO(xlsx_bytes),
            data_only=False,
            keep_links=False,
        )

    except Exception as error:
        raise ValueError(
            f"Cannot read ISO Excel workbook: {error}"
        ) from error

    # Verify that the required calculation sheets exist.
    required = {
        "Lift parameters",
        "Data measured",
        "Calculation",
        "Classification",
    }

    missing = required - set(cached.sheetnames)

    if missing:
        raise ValueError(
            "Missing worksheets: " + ", ".join(sorted(missing))
        )

    records = []

    # Extract values, formulas and source-cell information.
    for sheet_name, mapping in FIELDS.items():
        if sheet_name not in cached:
            continue

        sheet = cached[sheet_name]
        formula_sheet = original[sheet_name]

        column = (
            "B"
            if sheet_name in ("Test parameters", "ISO 2023 OP13")
            else "C"
        )

        for row in range(2, min(sheet.max_row, 200) + 1):
            label = sheet[f"A{row}"].value

            if row not in mapping and (
                not label or not str(label).strip() or row < 4
            ):
                continue

            cell = f"{column}{row}"
            value = sheet[cell].value
            source = formula_sheet[cell].value

            formula = (
                source
                if isinstance(source, str) and source.startswith("=")
                else None
            )

            if value is None and formula is None:
                continue

            field = mapping.get(
                row,
                "extra."
                + re.sub(
                    r"[^a-z0-9]+",
                    "_",
                    sheet_name.lower(),
                ).strip("_")
                + f".r{row}",
            )

            if isinstance(value, (date, datetime)):
                value = value.isoformat()

            if isinstance(value, str):
                value = value.strip()

            notes = [
                str(sheet[f"{col}{row}"].value)
                for col in ("D", "E")
                if sheet[f"{col}{row}"].value is not None
            ]

            records.append({
                "field": field,
                "value": value,
                "unit": UNITS.get(field),
                "kind": _kind(sheet_name, field),
                "sheet": sheet_name,
                "cell": cell,
                "formula": formula,
                "label": str(label or field).strip(),
                "note": " | ".join(notes),
            })

    # Extract classification details stored outside column C.
    for field, cell in (
        ("efficiency_class", "E7"),
        ("idle_standby_levels", "E3"),
    ):
        value = cached["Classification"][cell].value

        if value is None:
            continue

        source = original["Classification"][cell].value

        records.append({
            "field": field,
            "value": value,
            "unit": None,
            "kind": (
                "calculated"
                if field == "efficiency_class"
                else "reported"
            ),
            "sheet": "Classification",
            "cell": cell,
            "formula": (
                source
                if isinstance(source, str) and source.startswith("=")
                else None
            ),
        })

    # Reject inconsistent results before database insertion.
    _check_calculations(records)

    return records
