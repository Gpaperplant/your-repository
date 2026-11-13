
import math
import re

import pymupdf


# Field, page, label, value column, occurrence, unit, data type.
FIELDS = [
    ("manufacture_year", 3, "Year of manufacture", 245, 295, 0, "year", "specification"),
    ("rated_load_kg", 3, "Rated load, [kg, persons]", 245, 295, 0, "kg", "specification"),
    ("rated_speed_m_s", 3, "Rated speed, [m/s]", 468, 545, 0, "m/s", "specification"),
    ("travel_m", 3, "Travel height, [m]", 245, 295, 0, "m", "specification"),
    ("short_cycle_distance_m", 3, "Travel height (Short cycle), [m]", 245, 295, 0, "m", "measured"),
    ("express_zone_m", 3, "The total length of the express", 468, 545, 0, "m", "specification"),
    ("balance_factor", 3, "Balance factor", 245, 295, 0, None, "specification"),
    ("empty_car_mass_kg", 3, "Mass of empty car, [kg]", 245, 295, 0, "kg", "specification"),
    ("counterweight_mass_kg", 3, "Mass of counterweight, [kg]", 468, 545, 0, "kg", "specification"),
    ("acceleration_m_s2", 3, "Average acceleration, [m/s2]", 245, 295, 0, "m/s²", "measured"),
    ("jerk_m_s3", 3, "Average jerk, [m/s3]", 468, 545, 0, "m/s³", "measured"),
    ("supply_main_specified_v", 4, "Voltage [VAC]", 225, 260, 0, "V", "specification"),
    ("supply_main_actual_v", 4, "Voltage [VAC]", 315, 360, 0, "V", "measured"),
    ("supply_ancillary_specified_v", 4, "Voltage [VAC]", 405, 455, 0, "V", "specification"),
    ("supply_ancillary_actual_v", 4, "Voltage [VAC]", 490, 550, 0, "V", "measured"),
    ("daily_trips_actual", 5, "Number of trips per day (Actual)", 303, 341, 0, "trips/day", "measured"),
    ("iso_usage_category", 5, "Usage category", 303, 341, 0, None, "reported"),
    ("operating_days", 5, "Operating days per year", 303, 341, 0, "days/year", "assumed"),
    ("door_time_s", 5, "Door operation time [s]", 520, 559, 0, "s", "measured"),
    ("standby_entry_time_s", 5, "Time to reach standby mode [s]", 520, 559, 0, "s", "measured"),
    ("standby_recovery_time_s", 5, "Time to recover from standby mode [s]", 520, 559, 0, "s", "measured"),
    ("reference_cycles_count", 5, "Number of reference cycles", 303, 341, 0, None, "measured"),
    ("short_cycles_count", 5, "Number of short cycles", 520, 559, 0, None, "measured"),
    ("reference_main_10_wh", 5, "Running energy consumption of 10 reference", 300, 342, 0, "Wh", "measured"),
    ("reference_ancillary_10_wh", 5, "Running energy consumption of 10 reference", 300, 342, 1, "Wh", "measured"),
    ("short_main_10_wh", 5, "Running energy consumption of 10 short", 515, 560, 0, "Wh", "measured"),
    ("short_ancillary_10_wh", 5, "Running energy consumption of 10 short", 515, 560, 1, "Wh", "measured"),
    ("reference_main_average_wh", 5, "Average running energy consumption of a refer", 300, 342, 0, "Wh", "measured"),
    ("reference_ancillary_average_wh", 5, "Average running energy consumption of a refer", 300, 342, 1, "Wh", "measured"),
    ("short_main_average_wh", 5, "Average running energy consumption of a", 515, 560, 1, "Wh", "measured"),
    ("short_ancillary_average_wh", 5, "Average running energy consumption of a", 515, 560, 3, "Wh", "measured"),
    ("reference_energy_wh", 5, "Total average running energy consumption of a", 300, 342, 0, "Wh", "measured"),
    ("short_energy_wh", 5, "Total average running energy consumption of a", 515, 560, 1, "Wh", "measured"),
    ("avg_running_specific_mwh_kg_m", 5, "Specific running energy for the average running cycle", 435, 465, 0, "mWh/(kg·m)", "calculated"),
    ("idle_main_energy_wh", 5, "Idle main energy [Wh]", 306, 342, 0, "Wh", "measured"),
    ("idle_ancillary_energy_wh", 5, "Idle ancillary energy [Wh]", 306, 342, 0, "Wh", "measured"),
    ("idle_power_w", 5, "Idle power [W]", 306, 342, 0, "W", "measured"),
    ("idle_energy_wh", 5, "Idle energy [Wh]", 306, 342, 0, "Wh", "measured"),
    ("idle_performance_level", 5, "Performance level for idle", 306, 342, 0, None, "reported"),
    ("standby5_main_energy_wh", 5, "5min Standby main energy [Wh]", 518, 560, 0, "Wh", "measured"),
    ("standby5_ancillary_energy_wh", 5, "5min Standby ancillary energy [Wh]", 518, 560, 0, "Wh", "measured"),
    ("standby5_power_w", 5, "5min Standby power [W]", 518, 560, 0, "W", "measured"),
    ("standby5_energy_wh", 5, "5min Standby energy [Wh]", 518, 560, 0, "Wh", "measured"),
    ("standby5_performance_level", 5, "Performance level for 5min standby", 518, 560, 0, None, "reported"),
    ("standby30_main_energy_wh", 5, "30min Standby main energy [Wh]", 518, 560, 0, "Wh", "measured"),
    ("standby30_ancillary_energy_wh", 5, "30min Standby ancillary energy [Wh]", 518, 560, 0, "Wh", "measured"),
    ("standby30_power_w", 5, "30min Standby power [W]", 518, 560, 0, "W", "measured"),
    ("standby30_energy_wh", 5, "30min Standby energy [Wh]", 518, 560, 0, "Wh", "measured"),
    ("standby30_performance_level", 5, "Performance level for 30min standby", 518, 560, 0, None, "reported"),
    ("annual_energy_kwh", 6, "Estimated annual energy consumption [kWh]", 425, 555, 0, "kWh/year", "reported"),
    ("daily_energy_wh", 6, "Estimated daily energy consumption [Wh]", 425, 555, 0, "Wh/day", "reported"),
    ("specific_reference_mwh_kg_m", 6, "Specific running energy for reference cycle [mWh/(kg·m)]", 425, 555, 0, "mWh/(kg·m)", "calculated"),
]


def _value(page, label, x_min, x_max, occurrence=0):
    # Find a numerical value alongside its printed label.
    matches = page.search_for(label)

    if len(matches) <= occurrence:
        return None

    anchor = matches[occurrence]

    values = [
        word
        for word in page.get_text("words")
        if x_min <= word[0] < x_max
        and anchor.y0 - 6 <= word[1] <= anchor.y1 + 10
        and re.fullmatch(r"\d+(?:\.\d+)?%?", word[4])
    ]

    if not values:
        return None

    return min(
        values,
        key=lambda word: (word[1], word[0])
    )[4].rstrip("%")


def _match(text, pattern):
    # Return the first captured value from a matching expression.
    found = re.search(pattern, text, re.I | re.M)
    return found.group(1).strip() if found else None


def _validate_records(records):
    # Check required fields and reject unsupported layouts.
    values = {
        row["field"]: row["value"]
        for row in records
    }

    if len(values) != len(records):
        raise ValueError("Duplicate fields found in EE PDF.")

    required = (
        "lift_id",
        "serial_number",
        "rated_load_kg",
        "rated_speed_m_s",
        "travel_m",
        "stops",
        "daily_trips_actual",
        "reference_energy_wh",
        "short_energy_wh",
        "idle_power_w",
        "standby5_power_w",
        "standby30_power_w",
        "daily_energy_wh",
        "annual_energy_kwh",
        "operating_days",
        "efficiency_class",
    )

    missing = [
        field for field in required
        if field not in values
    ]

    if missing:
        raise ValueError(
            "Incomplete or unsupported EE PDF: "
            + ", ".join(missing)
        )

    if not re.fullmatch(r"[A-G]", values["efficiency_class"]):
        raise ValueError("Invalid energy efficiency class.")

    # Validate positive physical and energy values.
    numeric_fields = (
        "rated_load_kg",
        "rated_speed_m_s",
        "travel_m",
        "stops",
        "daily_trips_actual",
        "reference_energy_wh",
        "short_energy_wh",
        "idle_power_w",
        "standby5_power_w",
        "standby30_power_w",
        "daily_energy_wh",
        "annual_energy_kwh",
        "operating_days",
    )

    for field in numeric_fields:
        try:
            number = float(values[field])
        except (ValueError, TypeError):
            raise ValueError(
                f"Invalid numerical value: {field}"
            ) from None

        if not 0 < number < 1e10:
            raise ValueError(
                f"Invalid numerical value: {field}"
            )

    # Confirm that daily energy agrees with annual energy.
    daily = float(values["daily_energy_wh"])
    annual = float(values["annual_energy_kwh"])
    days = float(values["operating_days"])

    if not math.isclose(
        daily * days / 1000,
        annual,
        abs_tol=0.2,
    ):
        raise ValueError(
            "EE daily and annual energy values disagree."
        )

    # Confirm that main and ancillary cycle energy totals agree.
    for prefix in ("reference", "short"):
        main = values.get(f"{prefix}_main_average_wh")
        ancillary = values.get(
            f"{prefix}_ancillary_average_wh"
        )

        if main is None or ancillary is None:
            continue

        total = float(values[f"{prefix}_energy_wh"])

        if not math.isclose(
            float(main) + float(ancillary),
            total,
            abs_tol=0.02,
        ):
            raise ValueError(
                f"EE {prefix} cycle energy values disagree."
            )


def extract_ee_pdf(pdf_bytes):
    # Check the uploaded PDF format.
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("EE report must be a PDF.")

    records = []

    def add(field, value, page, kind="reported", unit=None):
        # Preserve each extracted field with its PDF source page.
        if value is not None and str(value).strip():
            records.append({
                "field": field,
                "value": str(value).strip(),
                "unit": unit,
                "kind": kind,
                "page": page,
            })

    with pymupdf.open(
        stream=pdf_bytes,
        filetype="pdf",
    ) as pdf:

        if len(pdf) != 6:
            raise ValueError(
                "Expected the six-page TÜV SÜD EE report layout."
            )

        p1, p3, p4, p5, p6 = [
            pdf[index].get_text("text", sort=True)
            for index in (0, 2, 3, 4, 5)
        ]

        # Confirm the supported report sections exist.
        sections = (
            (p1, "TEST REPORT"),
            (p3, "Description of test object"),
            (p4, "Power supply"),
            (p5, "Number of trips per day (Actual)"),
            (p6, "Estimated annual energy consumption"),
        )

        if any(
            label not in text
            for text, label in sections
        ):
            raise ValueError(
                "Unsupported TÜV SÜD EE report layout."
            )

        # Extract lift identification and report metadata.
        location = re.search(
            r"No\.\s*([A-Z]{1,4}\s*[-_]?\s*\d+"
            r"(?:\s*\([A-Z]\))?)\s+lift in\s+([^\n]+)",
            p1,
            re.I,
        )

        add(
            "lift_id",
            location.group(1) if location else None,
            1,
            "identity",
        )

        add(
            "building_name",
            location.group(2) if location else None,
            1,
            "metadata",
        )

        metadata = {
            "lift_model": (
                p1,
                r"Type\(s\):\s*([^\n]+)",
                1,
                "specification",
            ),
            "serial_number": (
                p1,
                r"Serial Number:\s*(\d+)",
                1,
                "identity",
            ),
            "report_number": (
                p1,
                r"NO\.:\s*([\w.]+)",
                1,
                "metadata",
            ),
            "report_date": (
                p1,
                r"^\s*Date:\s*(\d{4}-\d{2}-\d{2})",
                1,
                "metadata",
            ),
            "application_date": (
                p1,
                r"Date of Application:\s*(\d{4}-\d{2}-\d{2})",
                1,
                "metadata",
            ),
            "test_date": (
                p4,
                r"4\.2\s*Date of test\s*(\d{4}-\d{2}-\d{2})",
                4,
                "metadata",
            ),
            "calibration_date": (
                p4,
                r"Power Logger[\s\S]{0,280}?"
                r"(\d{4}-\d{2}-\d{2})",
                4,
                "metadata",
            ),
        }

        for field, (text, pattern, page, kind) in metadata.items():
            add(field, _match(text, pattern), page, kind)

        # Extract measurement instrument information.
        instrument = _value(
            pdf[3], "Power Logger", 170, 210
        )

        add(
            "instrument_model",
            f"Fluke {instrument}"
            if instrument and "Fluke" in p4
            else None,
            4,
            "metadata",
        )

        add(
            "instrument_serial",
            _match(p4, r"^\s*(\d{7,})\s*$"),
            4,
            "metadata",
        )

        # Extract specifications, measurements and energy results.
        for (
            field, page, label, x_min, x_max,
            occurrence, unit, kind
        ) in FIELDS:
            value = _value(
                pdf[page - 1],
                label,
                x_min,
                x_max,
                occurrence,
            )
            add(field, value, page, kind, unit)

        # Extract landing floors, stops and doors.
        anchors = pdf[2].search_for(
            "Number of landing floors / stops /"
        )

        for prefix, occurrence in (
            ("", 0),
            ("short_", 1),
        ):
            if len(anchors) <= occurrence:
                continue

            anchor = anchors[occurrence]

            words = [
                word[4]
                for word in pdf[2].get_text("words")
                if 468 <= word[0] <= 535
                and anchor.y0 - 3 <= word[1] <= anchor.y1 + 11
            ]

            triplet = next(
                (
                    value for value in words
                    if re.fullmatch(
                        r"\d+/\d+/\d+",
                        value,
                    )
                ),
                None,
            )

            if triplet:
                for field, value in zip(
                    ("landing_floors", "stops", "doors"),
                    triplet.split("/"),
                ):
                    add(
                        prefix + field,
                        value,
                        3,
                        "specification",
                    )

        # Extract remaining lift specifications.
        add(
            "drive_type",
            "Traction lift"
            if "Traction lift" in p3
            else None,
            3,
            "specification",
        )

        add(
            "roping",
            _match(p3, r"Roping\s+(\d+:\d+)"),
            3,
            "specification",
        )

        # Extract the energy efficiency classification.
        add(
            "efficiency_class",
            _match(
                p6,
                r"Energy efficiency class\s+([A-G])\b",
            ),
            6,
        )

        # Extract regenerative energy results.
        add(
            "afe_saving_percent",
            _match(
                p6,
                r"with AFE\s*-\s*(\d+(?:\.\d+)?)%",
            ),
            6,
            "reported",
            "%",
        )

        add(
            "regen_forward_wh",
            _value(
                pdf[5],
                "forward active energy measured by 10 reference cycles",
                475,
                515,
            ),
            6,
            "measured",
            "Wh",
        )

        add(
            "regen_reverse_wh",
            _value(
                pdf[5],
                "reverse active energy measured by 10 reference cycles",
                475,
                515,
            ),
            6,
            "measured",
            "Wh",
        )

        add(
            "regen_percent",
            _value(
                pdf[5],
                "percentage of reverse and forward active energy measured by 10 reference cycles",
                475,
                515,
            ),
            6,
            "reported",
            "%",
        )

        # Extract additional building and technology details.
        add(
            "building_type",
            "commercial office"
            if "commercial office" in p6
            else None,
            6,
            "metadata",
        )

        add(
            "regenerative_drive_type",
            "AFE"
            if "Active Front End (AFE)" in p6
            else None,
            6,
            "specification",
        )

    # Validate the extracted dataset before returning it.
    _validate_records(records)

    return records
