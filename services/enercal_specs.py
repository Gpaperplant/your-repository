
"""Extract unique profile and engineering fields from 12-page EnerCal MOD PDFs."""

import re
from datetime import datetime

import pymupdf


# Source label, database key, unit and comparison type
PROFILE = {
    "Client": ("client", None),
    "Project": ("project_name", None),
    "Site": ("site", None),
    "Platform Type": ("platform_type", None),
    "Quantity of identical elevators": ("elevator_quantity", None),
    "Group Name": ("group_name", None),
}

CONTACT = {
    "Contact first name": ("contact_first_name", None),
    "Contact last name": ("contact_last_name", None),
    "Address": ("contact_address", None),
    "City": ("contact_city", None),
    "Country": ("contact_country", None),
    "Phone": ("contact_phone", None),
    "Email": ("contact_email", None),
}

ENGINEERING = {
    "Nominal load [kg]": ("nominal_load_kg", "kg", True),
    "Nominal speed [m/s]": ("nominal_speed_m_s", "m/s", True),
    "Travel distance [m]": ("travel_distance_m", "m", False),
    "Lifetime [year]": ("lifetime_years", "years", False),
    "Number of served floors": ("served_floors", None, False),
    "Starts per year": ("starts_per_year", "starts/year", False),
    "Operating days per year": ("operating_days_per_year", "days/year", False),
    "Inflation rate [%]": ("inflation_rate", "%", False),
    "Country": ("country", None, False),
    "Electricity price [$/kWh]": ("electricity_price", "$/kWh", False),
    "ISO25745 Usage category": ("iso_usage_category", None, False),
    "Gear": ("gear", None, True),
    "Electric motor type": ("electric_motor_type", None, True),
    "Drive type": ("drive_type", None, True),
    "Signalization type": ("signalization_type", None, True),
    "Machine room location": ("machine_room_location", None, True),
    "Controller type": ("controller_type", None, True),
    "Roping suspension type": ("roping_suspension_type", None, True),
    "Rope wrap type": ("rope_wrap_type", None, True),
    "Ceiling type": ("ceiling_type", None, True),
    "OCL": ("ocl", None, True),
    "Regenerative drive": ("regenerative_drive", None, True),
}

# Coordinates for the eight energy figures on page 5
ENERGY_COMPONENTS = {
    "p5_existing_lighting_kwh": (130, 190, 200, 230, "kWh/year"),
    "p5_existing_non_running_kwh": (130, 190, 230, 275, "kWh/year"),
    "p5_existing_running_kwh": (130, 190, 295, 345, "kWh/year"),
    "p5_proposed_lighting_kwh": (245, 310, 273, 297, "kWh/year"),
    "p5_proposed_non_running_kwh": (245, 310, 299, 325, "kWh/year"),
    "p5_proposed_running_kwh": (245, 310, 330, 360, "kWh/year"),
    "p5_energy_without_regen_kwh": (540, 590, 150, 190, "kWh/year"),
    "p5_regen_saving_percent": (680, 740, 160, 200, "%"),
}


def _lines(page):
    """Extract text with its original position."""
    return [
        (*line["bbox"], "".join(
            span["text"] for span in line["spans"]
        ).strip())
        for block in page.get_text("dict")["blocks"]
        for line in block.get("lines", [])
        if line.get("spans")
    ]


def _put(found, key, value, unit, page):
    """Store a nonempty field only once."""
    value = str(value).strip()

    if value and key not in found:
        found[key] = (key, value, unit, page)


def _same_row(lines, label, min_x):
    """Find values horizontally aligned with a label."""
    return sorted(
        [
            line for line in lines
            if line[0] >= min_x
            and abs(line[1] - label[1]) < 3
        ],
        key=lambda line: line[0],
    )


def _profile(page, found):
    """Extract general information and contact details from page 2."""
    lines = _lines(page)

    contact_heading = next(
        (
            line[1] for line in lines
            if "KONE Contact Information" in line[4]
        ),
        None,
    )

    if contact_heading is None:
        raise ValueError("Page 2 contact section not found.")

    for line in lines:
        label = line[4]

        fields = (
            PROFILE if line[1] < contact_heading
            else CONTACT
        )

        if label not in fields or line[0] > 180:
            continue

        values = _same_row(lines, line, 200)

        if len(values) == 1:
            key, unit = fields[label]
            _put(found, key, values[0][4], unit, 2)

    date = re.search(
        r"\b\d{1,2} [A-Z]+ 20\d{2}\b",
        page.get_text(),
    )

    if date:
        parsed = datetime.strptime(
            date.group(),
            "%d %B %Y",
        )

        _put(
            found,
            "report_date",
            parsed.date().isoformat(),
            None,
            2,
        )


def _engineering(page, found):
    """Extract shared and paired engineering values from page 8."""
    lines = _lines(page)

    for line in lines:
        label = line[4]

        if label not in ENGINEERING or line[0] > 180:
            continue

        key, unit, paired = ENGINEERING[label]
        values = _same_row(lines, line, 300)

        # Paired values belong to existing and proposed elevators
        if paired and len(values) == 2:
            if values[0][0] < 480 < values[1][0]:
                _put(
                    found,
                    f"existing_{key}",
                    values[0][4],
                    unit,
                    8,
                )

                _put(
                    found,
                    f"proposed_{key}",
                    values[1][4],
                    unit,
                    8,
                )

        # Shared values are recorded only once
        elif not paired and len(values) == 1:
            _put(
                found,
                key,
                values[0][4],
                unit,
                8,
            )


def _energy_components(page, found):
    """Read energy figures from the page 5 charts."""
    lines = _lines(page)

    for key, coordinates in ENERGY_COMPONENTS.items():
        left, right, top, bottom, unit = coordinates

        values = [
            line[4]
            for line in lines
            if left <= line[0] < right
            and top <= line[1] < bottom
            and re.fullmatch(
                r"\d+(?:\.\d+)?%?",
                line[4],
            )
        ]

        if len(values) == 1:
            _put(
                found,
                key,
                values[0],
                unit,
                5,
            )


def extract_specs(pdf_bytes):
    """Return unique field, value, unit and source page records."""
    with pymupdf.open(
        stream=pdf_bytes,
        filetype="pdf",
    ) as pdf:

        if len(pdf) != 12 or any(
            abs(pdf[i].rect.width - 792) > 1
            or abs(pdf[i].rect.height - 632) > 1
            for i in (1, 4, 7)
        ):
            raise ValueError(
                "Expected a 12-page EnerCal MOD PDF."
            )

        required_sections = {
            1: "General Information",
            4: "Energy Savings",
            7: "Summary of Input Data",
        }

        for page_index, heading in required_sections.items():
            if heading not in pdf[page_index].get_text():
                raise ValueError(
                    f"Required report section not found on page {page_index + 1}"
                )

        found = {}

        _profile(pdf[1], found)
        _energy_components(pdf[4], found)
        _engineering(pdf[7], found)

    return list(found.values())
