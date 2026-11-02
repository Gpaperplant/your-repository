import re

import pymupdf
from rapidfuzz import fuzz, process

# Map PDF labels to field keys, units and column layouts.
FIELDS = {
    "main": {
        "Nominal load [kg]": ("nominal_load_kg", "kg", "pair"),
        "Nominal speed [m/s]": ("nominal_speed_m_s", "m/s", "pair"),
        "Travel distance [m]": ("travel_distance_m", "m", "single"),
        "Lifetime [year]": ("lifetime_years", "years", "single"),
        "Number of served floors": ("served_floors", None, "single"),
        "Starts per year": ("starts_per_year", "starts/year", "single"),
        "Operating days per year": ("operating_days_per_year", "days/year", "single"),
        "ISO25745 Usage category": ("iso_usage_category", None, "single"),
        "Electricity price [BHD/kWh]": ("electricity_price", "BHD/kWh", "single"),
        "Inflation rate [%]": ("inflation_rate", "%", "single"),
        "Country": ("country", None, "single"),
        "Drive type": ("drive_type", None, "single"),
        "Controller type": ("controller_type", None, "single"),
        "Ceiling type": ("ceiling_type", None, "pair"),
        "Regenerative drive": ("regenerative_drive", None, "pair"),
    },
    "extra": {
        "Group Control": ("group_control", None, "right"),
        "Car width [mm]": ("car_width_mm", "mm", "right"),
        "Car depth": ("car_depth", None, "right"),
        "Quantity of identical elevators": ("elevator_quantity", None, "right"),
        "Running time [h / operating day]": ("running_hours_per_day", "h/day", "right"),
        "Control system": ("control_system", None, "left"),
        "Display Types": ("display_type", None, "left"),
        "Number of landings": ("number_of_landings", None, "left"),
        "No of car entrances": ("number_of_car_entrances", None, "left"),
    },
}


def _normalise(text):
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _lines(page):
    return [
        (line["bbox"][1], line["bbox"][0], "".join(span["text"] for span in line["spans"]).strip())
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
    ]


def extract_specs(pdf_bytes):
    # Match labels while using positions to keep values in their correct columns.
    results = {}
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        for page_number, page in enumerate(pdf, 1):
            text = page.get_text("text")
            if "Summary of Input Data" not in text:
                continue
            if "Starts per year" in text:
                section = "main"
            elif "Running time" in text:
                section = "extra"
            else:
                continue
            if abs(page.rect.width - 960) > 25:
                continue

            lines = _lines(page)
            fields = {_normalise(label): value for label, value in FIELDS[section].items()}
            for y, x, label in lines:
                if (section == "main" and x > 200) or (section == "extra" and 170 < x < 290):
                    continue
                if section == "extra" and x > 475:
                    continue

                match = process.extractOne(
                    _normalise(label), fields.keys(), scorer=fuzz.ratio, score_cutoff=90
                )
                if match is None:
                    continue

                name, unit, layout = fields[match[0]]
                zones = (
                    [("existing_", 300, 550), ("proposed_", 650, 850)]
                    if layout == "pair"
                    else [("", 210, 290)] if layout == "left"
                    else [("", 495, 610)] if layout == "right"
                    else [("", 300, 850)]
                )
                for prefix, left, right in zones:
                    value = " ".join(
                        word for row_y, row_x, word in lines
                        if abs(row_y - y) < 3 and left <= row_x < right
                    ).strip()
                    if value:
                        key = prefix + name
                        results[key] = (key, value, unit, page_number)
    return list(results.values())
