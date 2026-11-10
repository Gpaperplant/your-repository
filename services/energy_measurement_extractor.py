
"""Extract engineering observations from ISO workbooks and lift test PDFs."""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from zipfile import is_zipfile

import pymupdf
from openpyxl import load_workbook


MAX_SIZE = 15 * 1024 * 1024

# Worksheet, cell, field, unit and source classification
EXCEL_FIELDS = (
    ("Lift parameters", "C5", "rated_load", "kg", "engineering_input"),
    ("Lift parameters", "C6", "rated_speed", "m/s", "engineering_input"),
    ("Lift parameters", "C7", "travel_distance", "m", "engineering_input"),
    ("Lift parameters", "C8", "served_floors", "count", "engineering_input"),
    ("Lift parameters", "C9", "counterbalance", "%", "engineering_input"),
    ("Lift parameters", "C10", "acceleration", "m/s2", "engineering_input"),
    ("Lift parameters", "C11", "jerk", "m/s3", "engineering_input"),
    ("Lift parameters", "C12", "door_operation_time", "s", "engineering_input"),
    ("Lift parameters", "C13", "operating_days_per_year", "days/year", "engineering_input"),
    ("Data measured", "C4", "daily_trips", "trips/day", "engineering_input"),
    ("Data measured", "C5", "usage_category", "category", "engineering_input"),
    ("Data measured", "C6", "standby_5min_power", "W", "test_measurement"),
    ("Data measured", "C8", "standby_30min_power", "W", "test_measurement"),
    ("Data measured", "C10", "reference_cycle_energy", "Wh", "test_measurement"),
    ("Data measured", "C11", "short_cycle_distance", "m", "engineering_input"),
    ("Data measured", "C12", "short_cycle_energy", "Wh", "test_measurement"),
    ("Data measured", "C13", "idle_power", "W", "test_measurement"),
    ("Calculation", "C6", "daily_running_energy", "Wh/day", "iso_estimate"),
    ("Calculation", "C10", "daily_nonrunning_energy", "Wh/day", "iso_estimate"),
    ("Calculation", "C11", "daily_energy", "Wh/day", "iso_estimate"),
    ("Classification", "C13", "annual_energy", "Wh/year", "iso_estimate"),
    ("Calculation", "C13", "daily_energy_comparison", "Wh/day", "unverified_entry"),
    ("Enercal", "N4", "enercal_annual_energy", "kWh/year", "enercal_estimate"),
    ("Enercal", "O4", "tuv_annual_energy_comparison", "kWh/year", "unverified_entry"),
)

# Page, label expression, field, unit and source classification
PDF_FIELDS = (
    (3, r"Rated load,\s*\[kg,\s*persons\]", "rated_load", "kg", "engineering_input"),
    (3, r"Rated speed,\s*\[m/s\]", "rated_speed", "m/s", "engineering_input"),
    (3, r"Travel height,\s*\[m\]", "travel_distance", "m", "engineering_input"),
    (3, r"Travel height \(Short cycle\),\s*\[m\]", "short_cycle_distance", "m", "engineering_input"),
    (3, r"Balance factor", "counterbalance", "ratio", "engineering_input"),
    (3, r"Average acceleration,\s*\[m/s2\]", "acceleration", "m/s2", "engineering_input"),
    (3, r"Average jerk,\s*\[m/s3\]", "jerk", "m/s3", "engineering_input"),
    (5, r"Number of trips per day \(Actual\)", "daily_trips", "trips/day", "engineering_input"),
    (5, r"Door operation time \[s\]", "door_operation_time", "s", "engineering_input"),
    (5, r"Usage category", "usage_category", "category", "engineering_input"),
    (5, r"Operating days per year", "operating_days_per_year", "days/year", "engineering_input"),
    (5, r"Time to reach standby mode \[s\]", "standby_activation_time", "s", "engineering_input"),
    (5, r"Total average running energy consumption of a\s+reference cycle \[Wh\]", "reference_cycle_energy", "Wh", "test_measurement"),
    (5, r"Total average running energy consumption of\s+a short cycle \[Wh\]", "short_cycle_energy", "Wh", "test_measurement"),
    (5, r"Idle power \[W\]", "idle_power", "W", "test_measurement"),
    (5, r"5min Standby power \[W\]", "standby_5min_power", "W", "test_measurement"),
    (5, r"30min Standby power \[W\]", "standby_30min_power", "W", "test_measurement"),
    (6, r"Estimated daily energy consumption \[Wh\]", "daily_energy", "Wh/day", "iso_estimate"),
    (6, r"Estimated annual energy consumption \[kWh\]", "annual_energy", "kWh/year", "iso_estimate"),
    (6, r"The percentage of the estimated daily energy consumption reduction of lift with AFE\s*-", "afe_estimated_reduction", "%", "iso_estimate"),
    (6, r"The forward active energy measured by 10 reference cycles of lift with AFE\s*\(main \+ ancillary\) \[Wh\]", "forward_energy_10_cycles", "Wh", "test_measurement"),
    (6, r"The reverse active energy measured by 10 reference cycles of lift with AFE\s*\(main \+ ancillary\) \[Wh\]", "reverse_energy_10_cycles", "Wh", "test_measurement"),
    (6, r"The percentage of reverse and forward active energy measured by 10 reference cycles\s+of lift with AFE \(energy regenerative percentage\)", "regenerative_energy_ratio", "%", "iso_estimate"),
)


def _number(value):
    if isinstance(value, bool):
        raise ValueError("Boolean values are not valid energy measurements")
    try:
        number = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError(f"Invalid measurement value {value}") from None
    if not number.is_finite() or number < 0:
        raise ValueError("Measurement values must be finite and nonnegative")
    return number


def _observation(metric, value, unit, kind, location, measured_on=None):
    return {
        "metric_name": metric,
        "raw_value": str(value),
        "numeric_value": _number(value),
        "unit": unit,
        "value_kind": kind,
        "source_location": location,
        "observed_on": measured_on,
        "period_start": None,
        "period_end": None,
    }


def extract_iso_excel(file_bytes):
    if not is_zipfile(BytesIO(file_bytes)):
        raise ValueError("Invalid Excel workbook")

    book = load_workbook(
        BytesIO(file_bytes),
        read_only=True,
        data_only=True,
        keep_links=False,
    )
    try:
        required = {sheet for sheet, *_ in EXCEL_FIELDS}
        if not required.issubset(book.sheetnames):
            raise ValueError("Unsupported ISO calculation workbook")

        elevator_ref = str(
            book["Lift parameters"]["C2"].value or ""
        ).strip().upper()
        if not re.fullmatch(r"OP\d+", elevator_ref):
            raise ValueError("Elevator reference not found in workbook")

        observations = []
        for sheet, cell, metric, unit, kind in EXCEL_FIELDS:
            value = book[sheet][cell].value
            if value is None or str(value).strip() == "":
                raise ValueError(f"Missing value in {sheet} {cell}")
            observations.append(
                _observation(metric, value, unit, kind, f"{sheet}!{cell}")
            )
    finally:
        book.close()

    return {
        "source_type": "iso_excel",
        "reported_elevator_ref": elevator_ref,
        "observations": observations,
    }


def extract_iso_pdf(file_bytes):
    if not file_bytes.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF file")

    with pymupdf.open(stream=file_bytes, filetype="pdf") as pdf:
        if len(pdf) != 6:
            raise ValueError("Expected a 6 page ISO test report")
        pages = {
            number: pdf[number - 1].get_text("text")
            for number in (1, 3, 4, 5, 6)
        }

    if "TEST REPORT" not in pages[1] or "ISO 25745" not in pages[1]:
        raise ValueError("Unsupported ISO test report")

    identity = re.search(r"\bOP\d+\b", pages[1], re.IGNORECASE)
    test_date = re.search(
        r"4\.2\s+Date of test\s+(\d{4}-\d{2}-\d{2})",
        pages[4],
    )
    if not identity or not test_date:
        raise ValueError("Missing elevator reference or test date")

    measured_on = date.fromisoformat(test_date.group(1))
    observations = []
    for page, label, metric, unit, kind in PDF_FIELDS:
        match = re.search(
            rf"{label}\s*([\d,]+(?:\.\d+)?)",
            pages[page],
            re.IGNORECASE,
        )
        if not match:
            raise ValueError(f"Missing {metric} on PDF page {page}")
        observations.append(
            _observation(
                metric,
                match.group(1),
                unit,
                kind,
                f"page {page}",
                measured_on if kind == "test_measurement" else None,
            )
        )

    return {
        "source_type": "iso_test_pdf",
        "reported_elevator_ref": identity.group().upper(),
        "observations": observations,
    }


def extract_measurements(file_bytes, filename):
    if not file_bytes or len(file_bytes) > MAX_SIZE:
        raise ValueError("Measurement file is empty or exceeds 15 MB")

    extension = (
        filename.rsplit(".", 1)[-1].lower()
        if "." in filename
        else ""
    )
    if extension == "xlsx":
        return extract_iso_excel(file_bytes)
    if extension == "pdf":
        return extract_iso_pdf(file_bytes)
    raise ValueError("Choose an ISO Excel workbook or test PDF")
