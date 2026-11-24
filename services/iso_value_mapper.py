from decimal import Decimal, InvalidOperation


ISO_PRE_FIELDS = (
    ("ISO_PRE_landing_floors", "common", "lift_landing_floors"),
    ("ISO_PRE_counterbalance_pct", "common", "counterbalance_percent"),
    ("ISO_PRE_acceleration_mps2", "common", "acceleration_m_s2"),
    ("ISO_PRE_jerk_mps3", "common", "jerk_m_s3"),
    ("ISO_PRE_door_operation_time_s", "common", "door_time_s"),
    ("ISO_PRE_daily_trips", "existing", "daily_trips_actual"),
    ("ISO_PRE_reference_cycle_wh", "existing", "reference_energy_wh"),
    ("ISO_PRE_short_cycle_distance_m", "existing", "short_cycle_distance_m"),
    ("ISO_PRE_short_cycle_wh", "existing", "short_energy_wh"),
    ("ISO_PRE_idle_power_w", "existing", "idle_power_w"),
    ("ISO_PRE_standby_5min_w", "existing", "standby5_power_w"),
    ("ISO_PRE_standby_30min_w", "existing", "standby30_power_w"),
    ("ISO_PRE_reference_main_wh", "existing", "reference_main_energy_wh"),
    ("ISO_PRE_reference_ancillary_wh", "existing", "reference_ancillary_energy_wh"),
    ("ISO_PRE_short_main_wh", "existing", "short_main_energy_wh"),
    ("ISO_PRE_short_ancillary_wh", "existing", "short_ancillary_energy_wh"),
    ("ISO_PRE_running_energy_wh_per_m", "existing", "energy_per_m_wh"),
    ("ISO_PRE_start_stop_energy_wh", "existing", "start_stop_energy_wh"),
    ("ISO_PRE_average_cycle_wh", "existing", "average_cycle_energy_wh"),
    ("ISO_PRE_running_wh_day", "existing", "daily_running_energy_wh"),
    ("ISO_PRE_nonrunning_wh_day", "existing", "daily_nonrunning_energy_wh"),
    ("ISO_PRE_annual_energy_kwh_year", "existing", "annual_energy_kwh"),
    ("ISO_PRE_energy_class", "existing", "efficiency_class"),
)
ISO_PRE_FEATURES = tuple(name for name, _, _ in ISO_PRE_FIELDS)

KINDS = {
    "identity": "identity",
    "specification": "engineering_specification",
    "measured": "test_measurement",
    "assumed": "assumption",
    "calculated": "calculated_estimate",
    "reported": "reported_estimate",
    "metadata": "metadata",
}

UNITS = {
    "counterbalance_percent": "%",
    "acceleration_m_s2": "m/s^2",
    "jerk_m_s3": "m/s^3",
    "door_time_s": "s",
    "energy_per_m_wh": "Wh/m",
    "start_stop_energy_wh": "Wh",
    "average_cycle_energy_wh": "Wh",
    "lift_landing_floors": None,
}


def _phase(field):
    if field.startswith("before_"):
        return "existing", field[7:]
    if field.startswith("after_"):
        return "proposed", field[6:]
    return "common", field


def _number(value):
    if isinstance(value, bool):
        raise ValueError("ISO numeric value cannot be Boolean.")
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        result = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"Invalid ISO numeric value: {value}") from error
    if not result.is_finite():
        raise ValueError("ISO numeric values must be finite.")
    return result


def _value(record, phase, field, synthetic_before):
    kind = KINDS.get(record.get("kind"))
    if kind is None:
        raise ValueError(f"Unsupported ISO value kind: {record.get('kind')}")

    note = str(record.get("note") or "").strip()
    if kind == "test_measurement" and phase == "existing":
        if synthetic_before or "synthetic before" in note.casefold():
            kind = "synthetic_input"

    value = record.get("value")
    if isinstance(value, bool):
        raise ValueError("ISO values cannot be Boolean.")
    if isinstance(value, (int, float, Decimal)):
        number = _number(value)
        text = None
    else:
        number = None
        text = None if value is None else str(value).strip() or None

    formula = record.get("formula")
    if formula is not None and (not isinstance(formula, str) or not formula.startswith("=")):
        raise ValueError("ISO Excel formula is invalid.")
    if number is None and text is None and formula is None:
        raise ValueError(f"ISO field {field} has no usable value.")

    unit = record.get("unit")
    if unit is None:
        unit = UNITS.get(field)

    return {
        "field_name": field,
        "phase": phase,
        "value_kind": kind,
        "raw_source_value": formula if formula is not None else (
            None if value is None else str(value)
        ),
        "numeric_value": number,
        "text_value": text,
        "unit": unit,
        "excel_sheet": record.get("sheet"),
        "excel_cell": record.get("cell"),
        "excel_formula": formula,
        "source_label": record.get("label"),
        "source_note": note or None,
        "source_verified": False,
        "is_corrected": False,
    }


def map_iso_values(records, *, synthetic_before=False):
    if not isinstance(records, (list, tuple)) or not records:
        raise ValueError("ISO extraction records must be a nonempty list.")
    if type(synthetic_before) is not bool:
        raise ValueError("synthetic_before must be Boolean.")

    mapped = []
    seen = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("field"), str):
            raise ValueError("Invalid ISO extraction record.")
        phase, field = _phase(record["field"])
        key = (phase, field)
        if key in seen:
            raise ValueError(f"Duplicate ISO field: {phase}.{field}")
        seen.add(key)
        mapped.append(_value(record, phase, field, synthetic_before))
    return mapped


def extract_iso_values(xlsx_bytes, *, synthetic_before=False):
    from services.iso_excel_extractor import extract_iso_excel

    return map_iso_values(extract_iso_excel(xlsx_bytes), synthetic_before=synthetic_before)


def iso_pre_feature_record(values, *, allow_synthetic=False):
    if type(allow_synthetic) is not bool:
        raise ValueError("allow_synthetic must be Boolean.")
    index = {}
    for record in values:
        key = (record["phase"], record["field_name"])
        if key in index:
            raise ValueError(f"Duplicate ISO field: {key[0]}.{key[1]}")
        index[key] = record

    result = {}
    for feature, phase, field in ISO_PRE_FIELDS:
        record = index.get((phase, field))
        if record is None:
            raise ValueError(f"Missing ISO Before feature source: {phase}.{field}")
        if record["value_kind"] == "synthetic_input" and not allow_synthetic:
            raise ValueError(f"Synthetic ISO Before input: {phase}.{field}")
        if feature == "ISO_PRE_energy_class":
            value = record.get("text_value")
            if not isinstance(value, str) or not value.strip():
                raise ValueError("Missing ISO Before energy class.")
            category = value.strip().upper().removeprefix("CLASS ").strip()
            if category not in "ABCDEFG" or len(category) != 1:
                raise ValueError("Invalid ISO Before energy class.")
            result[feature] = category
        else:
            value = _number(record.get("numeric_value"))
            if value is None:
                raise ValueError(f"Missing ISO Before numeric feature: {phase}.{field}")
            if value < 0 or (field in {"lift_landing_floors", "annual_energy_kwh"} and value == 0):
                raise ValueError(f"Invalid ISO Before feature value: {phase}.{field}")
            result[feature] = value
    return result
