from decimal import Decimal, InvalidOperation


COMPARISON_UNITS = {
    "annual_energy_consumption": "kWh/year",
    "annual_regenerative_energy": "kWh/year",
    "operational_co2e": "kgCO2e/lifetime",
    "embodied_co2e": "kgCO2e",
}
COMPARISON_FIELDS = (
    "annual_energy_consumption",
    "annual_regenerative_energy",
    "annual_electricity_expenditure",
    "lifetime_electricity_expenditure",
    "operational_co2e",
    "embodied_co2e",
)
NUMERIC_SPECS = frozenset({
    "elevator_quantity", "nominal_load_kg", "nominal_speed_m_s",
    "travel_distance_m", "lifetime_years", "served_floors",
    "starts_per_year", "operating_days_per_year", "inflation_rate",
    "electricity_price", "iso_usage_category",
})
PRIVATE_FIELDS = frozenset({
    "contact_first_name", "contact_last_name", "contact_address",
    "contact_city", "contact_country", "contact_phone", "contact_email",
})
MOD_FEATURES = (
    "MOD_rated_load_kg", "MOD_speed_existing_mps", "MOD_speed_proposed_mps",
    "MOD_travel_distance_m", "MOD_served_floors", "MOD_starts_per_year",
    "MOD_operating_days_per_year", "MOD_usage_category", "MOD_gear_existing",
    "MOD_gear_proposed", "MOD_drive_type_existing", "MOD_drive_type_proposed",
    "MOD_motor_technology_existing", "MOD_motor_technology_proposed",
    "MOD_roping_existing", "MOD_roping_proposed", "MOD_regeneration_existing",
    "MOD_regeneration_proposed", "MOD_PROXY_energy_existing_kwh_year",
    "MOD_PROXY_energy_proposed_kwh_year", "MOD_PROXY_savings_pct",
    "DERIVED_speed_increase_mps", "DERIVED_speed_increase_pct",
    "DERIVED_induction_to_pmsm", "DERIVED_geared_to_gearless",
    "DERIVED_regeneration_added",
)


def _number(value):
    if value is None or str(value).strip().upper() in {"", "N/A"}:
        return None
    try:
        number = Decimal(str(value).strip().replace(",", "").removesuffix("%"))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Invalid MOD number: {value}") from error
    if not number.is_finite():
        raise ValueError("MOD numeric values must be finite.")
    return number


def _record(name, phase, kind, value, unit, page, label, numeric=False):
    raw = "N/A" if value is None else str(value)
    number = _number(value) if numeric else None
    return {
        "field_name": name,
        "phase": phase,
        "value_kind": kind,
        "raw_source_value": raw,
        "numeric_value": number,
        "text_value": None if numeric or value is None else raw,
        "unit": unit,
        "source_page": page,
        "source_label": label,
        "source_verified": False,
        "is_corrected": False,
    }


def _spec_identity(key):
    for phase in ("existing", "proposed"):
        prefix = f"p5_{phase}_"
        if key.startswith(prefix):
            return f"p5_{key[len(prefix):]}", phase
        prefix = f"{phase}_"
        if key.startswith(prefix):
            return key[len(prefix):], phase
    return key, "common"


def map_mod_values(comparison, specifications):
    if comparison.get("source_page") != 4:
        raise ValueError("MOD energy comparison must come from page 4.")
    if not isinstance(specifications, (list, tuple)):
        raise ValueError("MOD specifications must be a list of extracted fields.")

    currency = comparison.get("currency_symbol")
    lifetime = comparison.get("lifetime_years")
    rows = comparison.get("rows", {})
    if not currency or not isinstance(lifetime, int) or lifetime < 1:
        raise ValueError("MOD currency and lifetime are required.")

    units = {
        **COMPARISON_UNITS,
        "annual_electricity_expenditure": f"{currency}/year",
        "lifetime_electricity_expenditure": f"{currency}/lifetime",
    }
    units["operational_co2e"] = f"kgCO2e/lifetime {lifetime} years"

    values = []
    for phase in ("existing", "proposed"):
        if phase not in rows:
            raise ValueError(f"Missing MOD {phase} comparison row.")
        row = rows[phase]
        if not row.get("elevator_technology"):
            raise ValueError(f"Missing MOD {phase} technology.")
        values.append(_record(
            "elevator_technology", phase, "engineering_specification",
            row["elevator_technology"], None, 4, f"{phase}_elevator_technology",
        ))
        for field in COMPARISON_FIELDS:
            if field not in row:
                raise ValueError(f"Missing MOD comparison field: {phase}_{field}")
            values.append(_record(
                field, phase, "calculated_estimate", row[field],
                units[field], 4, f"{phase}_{field}", numeric=True,
            ))

    for field, kind in (
        ("reported_energy_saving_percent", "reported_estimate"),
        ("calculated_energy_saving_percent", "calculated_estimate"),
    ):
        if field not in comparison:
            raise ValueError(f"Missing MOD saving field: {field}")
        values.append(_record(
            field, "common", kind, comparison[field], "%", 4, field,
            numeric=True,
        ))

    for item in specifications:
        if len(item) != 4:
            raise ValueError("MOD specifications must contain field, value, unit and page.")
        key, value, unit, page = item
        if key in PRIVATE_FIELDS:
            continue
        name, phase = _spec_identity(key)
        numeric = name in NUMERIC_SPECS or name.startswith("p5_")
        kind = "calculated_estimate" if name.startswith("p5_") else (
            "metadata" if page == 2 else "engineering_specification"
        )
        values.append(_record(
            name, phase, kind, value, unit, page, key, numeric=numeric,
        ))

    keys = [(value["phase"], value["field_name"]) for value in values]
    if len(set(keys)) != len(keys):
        raise ValueError("Duplicate MOD field and phase.")
    return values


def extract_mod_values(pdf_bytes):
    from services.enercal_extractor import extract_mod_document
    from services.enercal_specs import extract_specs

    document = extract_mod_document(pdf_bytes)
    return map_mod_values(document["comparison"], extract_specs(pdf_bytes))


def _motor(drive):
    label = drive.casefold()
    if "pmsm" in label or "permanent magnet" in label:
        return "PMSM"
    if "induction" in label:
        return "Induction"
    raise ValueError(f"Cannot infer motor technology from drive: {drive}")


def _yes_no(value):
    label = value.strip().casefold()
    if label in {"yes", "true", "1"}:
        return 1
    if label in {"no", "false", "0"}:
        return 0
    raise ValueError(f"Invalid MOD regenerative-drive value: {value}")


def mod_feature_record(values):
    index = {(item["phase"], item["field_name"]): item for item in values}
    if len(index) != len(values):
        raise ValueError("Duplicate MOD field and phase.")

    def get(phase, field, numeric=False):
        item = index.get((phase, field))
        if item is None:
            raise ValueError(f"Missing MOD feature source: {phase}.{field}")
        value = item["numeric_value" if numeric else "text_value"]
        if value is None:
            raise ValueError(f"Missing MOD feature value: {phase}.{field}")
        return Decimal(value) if numeric else value

    existing_speed = get("existing", "nominal_speed_m_s", True)
    proposed_speed = get("proposed", "nominal_speed_m_s", True)
    existing_kwh = get("existing", "annual_energy_consumption", True)
    proposed_kwh = get("proposed", "annual_energy_consumption", True)
    if existing_speed <= 0 or proposed_speed <= 0:
        raise ValueError("MOD speeds must be positive.")
    if existing_kwh <= 0 or proposed_kwh < 0:
        raise ValueError("MOD annual energy values are invalid.")

    gear_existing = get("existing", "gear")
    gear_proposed = get("proposed", "gear")
    drive_existing = get("existing", "drive_type")
    drive_proposed = get("proposed", "drive_type")
    motor_existing = _motor(drive_existing)
    motor_proposed = _motor(drive_proposed)
    regen_existing = _yes_no(get("existing", "regenerative_drive"))
    regen_proposed = _yes_no(get("proposed", "regenerative_drive"))

    features = (
        get("existing", "nominal_load_kg", True),
        existing_speed, proposed_speed,
        get("common", "travel_distance_m", True),
        get("common", "served_floors", True),
        get("common", "starts_per_year", True),
        get("common", "operating_days_per_year", True),
        get("common", "iso_usage_category", True),
        gear_existing, gear_proposed, drive_existing, drive_proposed,
        motor_existing, motor_proposed,
        get("existing", "roping_suspension_type"),
        get("proposed", "roping_suspension_type"),
        regen_existing, regen_proposed,
        existing_kwh, proposed_kwh,
        100 * (existing_kwh - proposed_kwh) / existing_kwh,
        proposed_speed - existing_speed,
        100 * (proposed_speed - existing_speed) / existing_speed,
        int(motor_existing == "Induction" and motor_proposed == "PMSM"),
        int(gear_existing.casefold() == "geared" and gear_proposed.casefold() == "gearless"),
        int(regen_existing == 0 and regen_proposed == 1),
    )
    return dict(zip(MOD_FEATURES, features, strict=True))
