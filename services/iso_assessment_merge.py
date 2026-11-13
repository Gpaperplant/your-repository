
"""Reconcile a TÜV SÜD EE PDF and its supporting ISO Excel for ONE lift.

Read-only: no database writes, no synthetic fills, no modernisation verification.
Install once: uv add openpyxl
"""

from datetime import date, datetime
from io import BytesIO
import re

import pymupdf
from openpyxl import load_workbook


# Field: (PDF page, PDF capture pattern, Excel sheet, cell, unit, data kind, tolerance)
FIELDS = {
    "elevator_id": (1, r"No\.\s*(OP\s*\d+\s*\([^)]*\))\s*lift", "Lift parameters", "C2", "", "identity", 0),
    "test_date": (4, r"4\.2\s*Date of test\s*(\d{4}-\d{2}-\d{2})", "Test parameters", "B2", "", "identity", 0),
    "rated_load_kg": (3, r"Rated load,\s*\[kg, persons\]\s*([\d,.]+)", "Lift parameters", "C5", "kg", "specification", 0),
    "rated_speed_m_s": (3, r"Rated speed,\s*\[m/s\]\s*([\d,.]+)", "Lift parameters", "C6", "m/s", "specification", 0),
    "travel_height_m": (3, r"Travel height,\s*\[m\]\s*([\d,.]+)", "Lift parameters", "C7", "m", "specification", 0.001),
    "number_of_stops": (3, r"Number of landing floors\s*/\s*stops\s*/\s*doors\s*\d+\s*/\s*(\d+)\s*/\s*\d+", "Lift parameters", "C8", "", "specification", 0),
    "acceleration_m_s2": (3, r"Average acceleration,\s*\[m/s2\]\s*([\d,.]+)", "Lift parameters", "C10", "m/s²", "specification", 0),
    "jerk_m_s3": (3, r"Average jerk,\s*\[m/s3\]\s*([\d,.]+)", "Lift parameters", "C11", "m/s³", "specification", 0),
    "door_operation_time_s": (5, r"Door operation time\s*\[s\]\s*([\d,.]+)", "Lift parameters", "C12", "s", "specification", 0),
    "actual_trips_day": (5, r"Number of trips per day\s*\(Actual\)\s*([\d,.]+)", "Data measured", "C4", "trips/day", "reported_operation", 0),
    "usage_category": (5, r"Usage category\s*([\d,.]+)", "Data measured", "C5", "", "assumption", 0),
    "operating_days_year": (5, r"Operating days per year\s*([\d,.]+)", "Lift parameters", "C13", "days/year", "assumption", 0),
    "reference_cycle_wh": (5, r"Total average running energy consumption of a\s*reference cycle \[Wh\]\s*([\d,.]+)", "Data measured", "C10", "Wh", "measured_test", 0.001),
    "short_cycle_wh": (5, r"Total average running energy consumption of\s*a short cycle \[Wh\]\s*([\d,.]+)", "Data measured", "C12", "Wh", "measured_test", 0.001),
    "short_cycle_distance_m": (3, r"Travel height \(Short cycle\),\s*\[m\]\s*([\d,.]+)", "Data measured", "C11", "m", "specification", 0.001),
    "idle_power_w": (5, r"Idle power \[W\]\s*([\d,.]+)", "Data measured", "C13", "W", "measured_test", 0.01),
    "standby_5min_power_w": (5, r"5min Standby power \[W\]\s*([\d,.]+)", "Data measured", "C6", "W", "measured_test", 0.01),
    "standby_30min_power_w": (5, r"30min Standby power \[W\]\s*([\d,.]+)", "Data measured", "C8", "W", "measured_test", 0.01),
    "estimated_daily_wh": (6, r"Estimated daily energy consumption \[Wh\]\s*([\d,.]+)", "Calculation", "C11", "Wh/day", "calculated_iso", 0.1),
    "estimated_annual_kwh": (6, r"Estimated annual energy consumption \[kWh\]\s*([\d,.]+)", "Classification", "C13", "kWh/year", "calculated_iso", 0.02),
    "efficiency_class": (6, r"Energy efficiency class\s*([A-G])\b", "Classification", "E7", "", "calculated_iso", 0),
    "iso_assumed_trips_day": (None, None, "Data from tables", "C8", "trips/day", "assumption", 0),
    "serial_number": (1, r"Serial Number:\s*(\d+)", None, None, "", "identity", 0),
    "ee_report_number": (1, r"NO\.\s*:\s*([\d.]+_EE)", None, None, "", "identity", 0),
}


def _normalize_lift(value):
    """Use the same lift identifier for both document types."""
    if value is None:
        return None
    match = re.search(r"OP\s*(\d+)\s*\(\s*([A-Z])\s*\)", str(value), re.I)
    return f"OP{match[1]} ({match[2].upper()})" if match else str(value).strip().upper()


def _clean(name, value, *, excel=False):
    """Normalize identifiers, dates, numeric units and efficiency classes."""
    if value is None:
        return None
    if name == "elevator_id":
        return _normalize_lift(value)
    if name == "test_date":
        return value.date().isoformat() if isinstance(value, datetime) else (
            value.isoformat() if isinstance(value, date) else str(value).strip()
        )
    if name == "efficiency_class":
        match = re.search(r"\b([A-G])\s*$", str(value), re.I)
        return match[1].upper() if match else str(value).strip().upper()
    if name in ("serial_number", "ee_report_number"):
        return str(value).strip()
    value = float(value.replace(",", "")) if isinstance(value, str) else value
    return value / 1000 if name == "estimated_annual_kwh" and excel else value


def read_ee_pdf(pdf_bytes, *, filename="EE_test_report.pdf"):
    if not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("EE file is not a PDF")
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        if len(doc) < 6:
            raise ValueError("Expected a six-page TÜV SÜD EE report")
        pages = [page.get_text("text") for page in doc]

    fields = {}
    for name, (page, pattern, _, _, unit, kind, _) in FIELDS.items():
        match = re.search(pattern, pages[page - 1], re.I) if page and pattern else None
        fields[name] = {"value": _clean(name, match[1]) if match else None,
                        "page": page, "unit": unit, "kind": kind}
    return {"filename": filename, "document_role": "primary_ee_test_report", "fields": fields}


def read_iso_excel(excel_bytes, *, filename="ISO_calculation.xlsx"):
    workbook = load_workbook(BytesIO(excel_bytes), data_only=True, read_only=True)
    try:
        fields = {}
        for name, (_, _, sheet, cell, unit, kind, _) in FIELDS.items():
            if not sheet:
                continue
            if sheet not in workbook:
                raise ValueError(f"ISO Excel is missing sheet: {sheet}")
            value = workbook[sheet][cell].value
            fields[name] = {"value": _clean(name, value, excel=True),
                            "sheet": sheet, "cell": cell, "unit": unit, "kind": kind}
    finally:
        workbook.close()
    return {"filename": filename, "document_role": "supporting_iso_reconstruction", "fields": fields}


def combine_iso_assessment(ee_pdf, iso_excel):
    pdf_fields, excel_fields = ee_pdf["fields"], iso_excel["fields"]
    pdf_id = pdf_fields["elevator_id"]["value"]
    excel_id = excel_fields["elevator_id"]["value"]
    if not pdf_id or not excel_id or pdf_id != excel_id:
        raise ValueError(f"Different elevators or missing IDs: EE PDF={pdf_id}; ISO Excel={excel_id}")

    rows = []
    for name, (page, _, sheet, cell, unit, kind, tolerance) in FIELDS.items():
        p = pdf_fields.get(name, {}).get("value")
        x = excel_fields.get(name, {}).get("value")
        if p is None and x is None:
            status = "MISSING"
        elif p is None:
            status = "EXCEL_ONLY"
        elif x is None:
            status = "PDF_ONLY"
        elif isinstance(p, (int, float)) and isinstance(x, (int, float)):
            delta = abs(p - x)
            status = "MATCH" if delta == 0 else "ROUNDING" if delta <= tolerance else "CONFLICT"
        else:
            status = "MATCH" if p == x else "CONFLICT"
        rows.append({
            "name": name, "kind": kind, "unit": unit,
            "pdf_value": p, "excel_value": x,
            "value": None if status in ("MISSING", "CONFLICT") else p if p is not None else x,
            "status": status, "ee_pdf_page": page,
            "iso_excel_sheet": sheet, "iso_excel_cell": cell,
            "preferred_source": "ee_pdf" if p is not None and status != "CONFLICT" else
                                "iso_excel_reconstruction" if x is not None and status != "CONFLICT" else None,
            "synthetic_value_created": False,
            "input_synthetic_status": excel_fields.get(name, {}).get("is_synthetic"),
            "verified_modernisation_target_eligible": False,
        })

    conflicts = [r["name"] for r in rows if r["status"] == "CONFLICT"]
    missing = [r["name"] for r in rows if r["status"] == "MISSING"]
    return {
        "elevator_id": pdf_id,
        "assessment_type": "ISO_25745_energy_assessment",
        "documents": [
            {"filename": ee_pdf["filename"], "role": "primary_ee_test_report"},
            {"filename": iso_excel["filename"], "role": "supporting_reconstructed_iso_calculation"},
        ],
        "fields": rows,
        "conflicting_fields": conflicts, "missing_fields": missing,
        "review_status": "REVIEW_REQUIRED" if conflicts or missing else "RECONCILED",
        "independent_measurement_sample_count": 1,
        "verified_before_after_modernisation": False,
        "eligible_for_verified_savings_ai": False,
        "notes": [
            "Two documents support one lift assessment, not two independent samples.",
            "Excel annual energy is converted from Wh/year to kWh/year.",
            "Actual trips/day and ISO assumed trips/day are distinct.",
            "No missing values are fabricated; reconstructed Excel values are not independently verified.",
        ],
    }


def read_and_merge_iso_assessment(ee_pdf_bytes, iso_xlsx_bytes, *,
                                  ee_filename="EE_test_report.pdf",
                                  excel_filename="ISO_calculation.xlsx"):
    return combine_iso_assessment(
        read_ee_pdf(ee_pdf_bytes, filename=ee_filename),
        read_iso_excel(iso_xlsx_bytes, filename=excel_filename),
    )
