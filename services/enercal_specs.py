import re
import unicodedata
from datetime import datetime

import pymupdf
from rapidfuzz import fuzz, process

# Page 8 inputs: (database key, unit, existing/proposed layout).
MAIN = {
    "Nominal load [kg]": ("nominal_load_kg", "kg", "pair"),
    "Nominal speed [m/s]": ("nominal_speed_m_s", "m/s", "pair"),
    "Travel distance [m]": ("travel_distance_m", "m", "single"),
    "Lifetime [year]": ("lifetime_years", "years", "single"),
    "Number of served floors": ("served_floors", None, "single"),
    "Starts per year": ("starts_per_year", "starts/year", "single"),
    "Operating days per year": ("operating_days_per_year", "days/year", "single"),
    "Inflation rate [%]": ("inflation_rate", "%", "single"),
    "Country": ("country", None, "single"),
    "Electricity price [BHD/kWh]": ("electricity_price", "BHD/kWh", "single"),
    "ISO25745 Usage category": ("iso_usage_category", None, "single"),
    "Gear": ("gear", None, "single"),
    "Electric motor type": ("electric_motor_type", None, "single"),
    "Drive type": ("drive_type", None, "single"),
    "Signalization type": ("signalization_type", None, "single"),
    "Machine room location": ("machine_room_location", None, "single"),
    "Controller type": ("controller_type", None, "single"),
    "Roping suspension type": ("roping_suspension_type", None, "single"),
    "Rope wrap type": ("rope_wrap_type", None, "single"),
    "Ceiling type": ("ceiling_type", None, "pair"),
    "OCL": ("ocl", None, "pair"),
    "Regenerative drive": ("regenerative_drive", None, "pair"),
    "Car Fan": ("car_fan", None, "pair"),
}

# Page 9 uses two independent side-by-side label/value tables.
EXTRA = {
    "Flexible Car": ("flexible_car", None, "single"),
    "No of Elevators in Group": ("elevators_in_group", None, "single"),
    "LIL": ("lil", None, "single"),
    "Number of COP": ("number_of_cop", None, "single"),
    "CH": ("ch", None, "single"),
    "Control system": ("control_system", None, "single"),
    "No of floors with LCI": ("floors_with_lci", None, "single"),
    "IntegrateLCI": ("integrate_lci", None, "single"),
    "ExistingOCL": ("existing_ocl_enabled", None, "single"),
    "Modern OCL": ("modern_ocl_enabled", None, "single"),
    "Modern Regenerative Drive": ("modern_regenerative_drive", None, "single"),
    "ACU F": ("acu_f", None, "single"),
    "EBD A": ("ebd_a", None, "single"),
    "Display Types": ("display_type", None, "single"),
    "ABE M": ("abe_m", None, "single"),
    "KFC": ("kfc", None, "single"),
    "Number of landings": ("number_of_landings", None, "single"),
    "No of car entrances": ("number_of_car_entrances", None, "single"),
    "FRD": ("frd", None, "single"),
    "Door Type": ("door_type", None, "single"),
    "KRM": ("krm", None, "single"),
    "Group Control": ("group_control", None, "single"),
    "Car width [mm]": ("car_width_mm", "mm", "single"),
    "Car depth": ("car_depth", "mm", "single"),
    "Quantity of identical elevators": ("p9_elevator_quantity", None, "single"),
    "Running time [h / operating day]": ("running_hours_per_day", "h/day", "single"),
}

ALIASES = {
    "Rated load [kg]": "Nominal load [kg]",
    "Rated speed [m/s]": "Nominal speed [m/s]",
    "Number of floors served": "Number of served floors",
    "Operating hours [h / operating day]": "Running time [h / operating day]",
}

METADATA = {
    "Client": ("client", None, "single"),
    "Project": ("project_name", None, "single"),
    "Site": ("site", None, "single"),
    "Platform Type": ("platform_type", None, "single"),
    "Quantity of identical elevators": ("elevator_quantity", None, "single"),
    "Group Name": ("group_name", None, "single"),
}

CONTACT = {
    "Contact first name": ("contact_first_name", None, "single"),
    "Contact last name": ("contact_last_name", None, "single"),
    "Address": ("contact_address", None, "single"),
    "City": ("contact_city", None, "single"),
    "Country": ("contact_country", None, "single"),
    "Phone": ("contact_phone", None, "single"),
    "Email": ("contact_email", None, "single"),
}

# Values stay distinct by report page for cross-page reconciliation.
SUMMARY_TOP = ("p3_energy_saving_percent", "p3_iso_class", "p3_annual_cost_bhd")
SUMMARY_BOTTOM = ("p3_operational_co2e_kg", "p3_annual_energy_kwh", "p3_regen_saving_percent")
ISO_TOP = ("p10_usage_category", "p10_iso_class", "p10_idle_level", "p10_standby5_level", "p10_standby30_level", "p10_running_level")
ISO_BOTTOM = ("p10_operating_days", "p10_annual_energy_kwh", "p10_annual_cost_bhd", "p10_regen_energy_kwh", "p10_regen_saving_percent")


def _normalise(text):
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKC", text).casefold()).strip()


def _lines(page):
    # Keep lines and positions rather than relying on page width.
    return sorted(
        [
            (*line["bbox"], "".join(s["text"] for s in line["spans"]).strip())
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
            if "bbox" in line and line.get("spans")
        ],
        key=lambda line: (line[1], line[0]),
    )


def _put(results, key, value, unit, page_number):
    # Never save a blank or conflicting field.
    value = str(value).strip()
    if value and key not in results:
        results[key] = (key, value, unit, page_number)


def _labels(lines, definitions, width, aliases=None):
    candidates = {_normalise(label): data for label, data in definitions.items()}
    for alias, original in (aliases or {}).items():
        if original in definitions:
            candidates[_normalise(alias)] = definitions[original]

    found = []
    limit = width * (0.40 if definitions is MAIN else 0.48)
    for line in lines:
        if line[0] > limit:
            continue
        label = _normalise(line[4])
        if not label or len(label) < 2:
            continue
        match = process.extractOne(label, candidates.keys(), scorer=fuzz.ratio, score_cutoff=90)
        if match:
            found.append((line, *candidates[match[0]]))
    return found


def _rows(lines, definitions, page_number, results, width, aliases=None, metadata=False):
    labels = _labels(lines, definitions, width, aliases)
    if len(labels) < (1 if metadata else 3):
        return

    # Use headings to identify existing/proposed columns even if reversed.
    heads = sorted(
        [(line[0], "existing" if "existing technology" in _normalise(line[4]) else "proposed")
         for line in lines
         if "existing technology" in _normalise(line[4])
         or "modernization technology" in _normalise(line[4])
         or "modernisation technology" in _normalise(line[4])]
    )

    for line, key, unit, layout in labels:
        x0, y0, x1, y1, _ = line
        # Stop at another label in the same row, as on page 9.
        right = min(
            (other[0][0] for other in labels
             if other[0][0] > x0 and abs(other[0][1] - y0) < 3),
            default=float("inf"),
        )
        values = sorted(
            [(other[0], other[4]) for other in lines
             if other[0] > x1 + 2 and other[0] < right
             and abs(other[1] - y0) < 3
             and all(other is not labelled[0] for labelled in labels)],
            key=lambda item: item[0],
        )
        if layout == "pair":
            # If headings are missing, do not guess existing/proposed order.
            if len(heads) != 2 or len(values) != 2:
                continue
            for x, value in values:
                role = min(heads, key=lambda heading: abs(x - heading[0]))[1]
                _put(results, f"{role}_{key}", value, unit, page_number)
        elif len(values) == 1:
            value = values[0][1]
            if key == "door_type":
                # Include a wrapped second line, e.g. "middle duty D15".
                extra = [other[4] for other in lines
                         if y0 + 0.7 * (y1 - y0) < other[1] < y0 + 2.2 * (y1 - y0)
                         and abs(other[0] - values[0][0]) < 15]
                if len(extra) == 1:
                    value += " " + extra[0]
            _put(results, key, value, unit, page_number)


def _numbers(lines, page, ymin=0, ymax=1, xmin=0, xmax=1, percent=False):
    # Read embedded chart labels; this does not require OCR.
    pattern = r"\d[\d,]*(?:\.\d+)?%?" if percent else r"\d[\d,]*(?:\.\d+)?"
    return [
        (line[0], line[1], line[4])
        for line in lines
        if xmin * page.rect.width <= line[0] < xmax * page.rect.width
        and ymin * page.rect.height <= line[1] < ymax * page.rect.height
        and re.fullmatch(pattern, line[4])
    ]


def _numeric_rows(lines, page):
    # Group numeric text that is horizontally aligned in a KPI panel.
    candidates = [
        (line[0], line[1], line[4])
        for line in lines
        if 0.2 * page.rect.height < line[1] < 0.83 * page.rect.height
        and re.fullmatch(r"(?:\d+(?:\.\d+)?%?|[A-G])", line[4])
    ]
    rows = []
    for item in sorted(candidates, key=lambda item: item[1]):
        if rows and abs(rows[-1][0][1] - item[1]) <= 5:
            rows[-1].append(item)
        else:
            rows.append([item])
    return [sorted(row, key=lambda item: item[0]) for row in rows]


def _split_groups(values):
    # Split two chart bars at the largest horizontal gap.
    ordered = sorted(values, key=lambda item: item[0])
    if len(ordered) < 2:
        return [], []
    index = max(range(len(ordered) - 1), key=lambda i: ordered[i + 1][0] - ordered[i][0])
    return (sorted(ordered[:index + 1], key=lambda item: item[1]),
            sorted(ordered[index + 1:], key=lambda item: item[1]))


def _page3(lines, page, results, number):
    groups = [row for row in _numeric_rows(lines, page) if len(row) == 3]
    if len(groups) != 2:
        return
    for keys, values in zip((SUMMARY_TOP, SUMMARY_BOTTOM), groups):
        for key, (_, _, value) in zip(keys, values):
            _put(results, key, value, "%" if key.endswith("percent") else None, number)


def _page5(lines, page, results, number):
    # Extract both four-part energy bars, checking their arithmetic.
    left = _numbers(lines, page, 0.26, 0.68, 0.12, 0.48)
    existing, proposed = _split_groups(left)
    keys = ("total", "lighting", "non_running", "running")
    for role, group in (("existing", existing), ("proposed", proposed)):
        if len(group) != 4:
            continue
        parsed = [float(v[2].replace(",", "")) for v in group]
        if abs(parsed[0] - sum(parsed[1:])) > 2:
            continue
        for key, (_, _, value) in zip(keys, group):
            _put(results, f"p5_{role}_{key}_kwh", value, "kWh/year", number)

    # Separate the regeneration chart from the energy breakdown chart.
    regen = _numbers(lines, page, 0.26, 0.68, 0.52, 0.80)
    with_drive, without_drive = _split_groups(regen)
    if len(with_drive) == 2 and len(without_drive) == 3:
        without, saved, with_again = [float(v[2]) for v in without_drive]
        with_value = float(with_drive[-1][2])
        if abs(without - saved - with_value) <= 2 and abs(with_again - with_value) <= 2:
            for key, value in (
                ("p5_energy_with_regen_kwh", with_drive[-1][2]),
                ("p5_energy_without_regen_kwh", without_drive[0][2]),
                ("p5_energy_saved_regen_kwh", without_drive[1][2]),
            ):
                _put(results, key, value, "kWh/year", number)

    for x, _, value in _numbers(lines, page, 0.2, 0.35, percent=True):
        if value.endswith("%"):
            key = "p5_energy_saving_percent" if x < page.rect.width / 2 else "p5_regen_saving_percent"
            _put(results, key, value, "%", number)


def _page6(lines, page, results, number):
    # Read annual and lifetime cost chart values separately.
    numbers = sorted(_numbers(lines, page, 0.31, 0.65, 0.05, 0.84), key=lambda item: item[0])
    keys = ("p6_existing_annual_cost_bhd", "p6_proposed_annual_cost_bhd",
            "p6_existing_lifetime_cost_bhd", "p6_proposed_lifetime_cost_bhd")
    if len(numbers) == 4:
        for key, (_, _, value) in zip(keys, numbers):
            _put(results, key, value, "BHD/year" if "annual" in key else "BHD/lifetime", number)


def _page7(lines, page, results, number):
    # Verify that the proposed bar is operational plus embodied CO2e.
    numbers = _numbers(lines, page, 0.27, 0.72, 0.60, 0.85)
    existing, proposed = _split_groups(numbers)
    if len(existing) == 2 and len(proposed) == 3:
        total, embodied, operational = [float(v[2]) for v in proposed]
        if abs(total - embodied - operational) <= 2 and existing[0][2] == existing[1][2]:
            for key, value in (
                ("p7_existing_operational_co2e_kg", existing[0][2]),
                ("p7_proposed_combined_co2e_kg", proposed[0][2]),
                ("p7_proposed_embodied_co2e_kg", proposed[1][2]),
                ("p7_proposed_operational_co2e_kg", proposed[2][2]),
            ):
                _put(results, key, value, "kgCO2e/lifetime", number)
    for _, _, value in _numbers(lines, page, 0.24, 0.4, 0.85, 0.98, percent=True):
        if value.endswith("%"):
            _put(results, "p7_operational_saving_percent", value, "%", number)


def _page10(lines, page, results, number):
    groups = _numeric_rows(lines, page)
    top = next((row for row in groups if len(row) == 6), None)
    bottom = next((row for row in groups if len(row) == 5), None)
    for keys, row in ((ISO_TOP, top), (ISO_BOTTOM, bottom)):
        if row is None:
            continue
        for key, (_, _, value) in zip(keys, row):
            unit = "%" if key.endswith("percent") else "kWh/year" if "energy_kwh" in key else "BHD/year" if "cost_bhd" in key else None
            _put(results, key, value, unit, number)


def _page2(lines, page, results, number):
    # Page 2 has two distinct label/value sections.
    metadata_lines = [line for line in lines if line[1] < 0.57 * page.rect.height]
    contact_lines = [line for line in lines if 0.57 * page.rect.height <= line[1] < 0.91 * page.rect.height]
    _rows(metadata_lines, METADATA, number, results, page.rect.width, metadata=True)
    _rows(contact_lines, CONTACT, number, results, page.rect.width, metadata=True)

    date_match = re.search(r"\b\d{1,2} [A-Z]+ 20\d{2}\b", page.get_text("text"))
    if date_match:
        try:
            date = datetime.strptime(date_match.group(), "%d %B %Y").date().isoformat()
            _put(results, "report_date", date, None, number)
        except ValueError:
            pass

    # Save an empty string when a field was left blank in the source PDF.
    # This preserves the distinction between missing and a real value of zero.
    for _, (key, unit, _) in (*METADATA.items(), *CONTACT.items()):
        results.setdefault(key, (key, "", unit, number))
    results.setdefault("report_date", ("report_date", "", None, number))




def _crosscheck_results(results, comparison):
    """Store check statuses and return the values behind each check."""
    from decimal import Decimal, InvalidOperation

    rows = comparison["rows"]
    old, new = rows["existing"], rows["proposed"]
    snapshot = dict(results)
    evidence = []

    def get(key):
        return snapshot[key][1] if key in snapshot else None

    def number(value):
        if value is None:
            return None
        try:
            return Decimal(str(value).replace(",", "").replace("%", "").strip())
        except (InvalidOperation, ValueError):
            return None

    def ratio(a, b):
        a, b = number(a), number(b)
        return (a - b) / a * 100 if a is not None and b is not None and a > 0 else None

    def parts(*names):
        values = [number(get(name)) for name in names]
        return sum(values) if all(value is not None for value in values) else None

    def add(name, actual, expected, tolerance="1", source=None, rule="Compare report figures"):
        # Use the same result for database storage and reviewer evidence.
        a, b = number(actual), number(expected)
        if actual is None or expected is None:
            status = "MISSING"
        elif a is not None and b is not None:
            status = "PASS" if abs(a - b) <= Decimal(tolerance) else "REVIEW"
        else:
            status = "PASS" if _normalise(str(actual)) == _normalise(str(expected)) else "REVIEW"

        origin_page = snapshot[source][3] if source in snapshot else comparison["source_page"]
        _put(results, f"check_{name}", status, "cross-page check", origin_page)
        evidence.append({
            "key": name,
            "status": status,
            "source": actual,
            "reference": expected,
            "difference": abs(a - b) if a is not None and b is not None else None,
            "tolerance": tolerance if a is not None and b is not None else "Exact text",
            "rule": rule,
            "page": origin_page,
        })

    old_kwh = old["annual_energy_consumption"]
    new_kwh = new["annual_energy_consumption"]
    savings = ratio(old_kwh, new_kwh)

    # Confirm that the page-4 reported savings agrees with its energy values.
    add("p4_reported_energy_saving_percent",
        comparison.get("reported_energy_saving_percent"), savings, "0.6",
        rule="(existing energy - proposed energy) / existing energy * 100")

    # Page 3: summary figures.
    for name, expected, tolerance in (
        ("p3_energy_saving_percent", savings, "0.6"),
        ("p3_annual_energy_kwh", new_kwh, "1"),
        ("p3_annual_cost_bhd", new["annual_electricity_expenditure"], "1"),
        ("p3_operational_co2e_kg", new["operational_co2e"], "1"),
    ):
        add(name, get(name), expected, tolerance, name)

    # Page 5: both energy bars and the regenerative energy calculation.
    for role, total in (("existing", old_kwh), ("proposed", new_kwh)):
        key = f"p5_{role}_total_kwh"
        add(key, get(key), total, source=key)
        add(f"p5_{role}_components", parts(
            f"p5_{role}_running_kwh",
            f"p5_{role}_non_running_kwh",
            f"p5_{role}_lighting_kwh",
        ), total, source=key, rule="running + non-running + lighting energy")

    regen = new["annual_regenerative_energy"]
    add("p5_energy_with_regen_kwh", get("p5_energy_with_regen_kwh"), new_kwh,
        source="p5_energy_with_regen_kwh")
    add("p5_energy_saved_regen_kwh", get("p5_energy_saved_regen_kwh"), regen,
        source="p5_energy_saved_regen_kwh")
    add("p5_energy_without_regen_kwh", get("p5_energy_without_regen_kwh"),
        number(new_kwh) + number(regen) if number(new_kwh) is not None and number(regen) is not None else None,
        source="p5_energy_without_regen_kwh", rule="proposed energy + regenerative energy")
    add("p5_energy_saving_percent", get("p5_energy_saving_percent"), savings,
        "0.6", "p5_energy_saving_percent")
    without = get("p5_energy_without_regen_kwh")
    energy_saved = get("p5_energy_saved_regen_kwh")
    regen_pct = (number(energy_saved) / number(without) * 100
                 if number(energy_saved) is not None and number(without) is not None
                 and number(without) > 0 else None)
    add("p5_regen_saving_percent", get("p5_regen_saving_percent"), regen_pct,
        "0.6", "p5_regen_saving_percent", rule="regenerative saving / energy without regeneration * 100")

    # Page 6: costs (preserve rounded report values).
    for name, expected in (
        ("p6_existing_annual_cost_bhd", old["annual_electricity_expenditure"]),
        ("p6_proposed_annual_cost_bhd", new["annual_electricity_expenditure"]),
        ("p6_existing_lifetime_cost_bhd", old["lifetime_electricity_expenditure"]),
        ("p6_proposed_lifetime_cost_bhd", new["lifetime_electricity_expenditure"]),
    ):
        add(name, get(name), expected, source=name)

    # Page 7: keep operational and embodied emissions distinct.
    for name, expected in (
        ("p7_existing_operational_co2e_kg", old["operational_co2e"]),
        ("p7_proposed_operational_co2e_kg", new["operational_co2e"]),
        ("p7_proposed_embodied_co2e_kg", new["embodied_co2e"]),
        ("p7_proposed_combined_co2e_kg",
         number(new["operational_co2e"]) + number(new["embodied_co2e"])
         if number(new["operational_co2e"]) is not None and number(new["embodied_co2e"]) is not None else None),
        ("p7_operational_saving_percent", ratio(old["operational_co2e"], new["operational_co2e"])),
    ):
        add(name, get(name), expected,
            "0.6" if name.endswith("percent") else "1", name)

    # Page 10: report figures should agree with page 4 or the other input pages.
    for name, expected in (
        ("p10_annual_energy_kwh", new_kwh),
        ("p10_annual_cost_bhd", new["annual_electricity_expenditure"]),
        ("p10_regen_energy_kwh", regen),
        ("p10_operating_days", get("operating_days_per_year")),
        ("p10_usage_category", get("iso_usage_category")),
        ("p10_iso_class", get("p3_iso_class")),
        ("p10_regen_saving_percent", regen_pct),
    ):
        add(name, get(name), expected,
            "0.6" if name.endswith("percent") else "1", name)

    return evidence


def build_crosscheck_details(specifications, comparison):
    """Explain stored checks using the exact rules used at PDF upload.

    The original PDF comparison is used, not engineer-edited revisions.
    get_specifications() returns labels with spaces; restore their original keys.
    """
    fields = {}
    saved_statuses = {}
    for row in specifications:
        key = row["Field"].casefold().replace(" ", "_")
        if key.startswith("check_"):
            saved_statuses[key] = str(row["Value"])
        else:
            fields[key] = (key, row["Value"], row["Unit"], row["Source page"])

    details = _crosscheck_results(fields, comparison)
    for item in details:
        item["saved_status"] = saved_statuses.get("check_" + item["key"])
    return [item for item in details if item["saved_status"] is not None]


def extract_specs(pdf_bytes):
    # Extract additional observations before checking them against page 4.
    results = {}
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        for number, page in enumerate(pdf, 1):
            text = _normalise(page.get_text("text"))
            lines = _lines(page)
            if "general information" in text and "platform type" in text:
                _page2(lines, page, results, number)
            elif "energy performance" in text and "calculated annual" in text and "co2e emissions" in text:
                _page3(lines, page, results, number)
            elif "annual energy consumption and regenerative energy" in text:
                _page5(lines, page, results, number)
            elif "annual and lifetime expenditure for electricity" in text:
                _page6(lines, page, results, number)
            elif "operational co2e emissions" in text and "embodied co2e" in text:
                _page7(lines, page, results, number)
            elif "summary of input data" in text:
                definitions = MAIN if "starts per year" in text else EXTRA
                _rows(lines, definitions, number, results, page.rect.width, ALIASES)
            elif (
                "engineering inputs" in text
                or "rated load" in text
                or ("control system" in text and "running time" in text)
            ):
                _rows(lines, MAIN, number, results, page.rect.width, ALIASES)
                _rows(lines, EXTRA, number, results, page.rect.width)
            elif "iso 25745 2 energy performance" in text and "detailed results" in text:
                _page10(lines, page, results, number)

    # Run page-4 checks when the comparison page is present.
    # Do not silently suppress errors from a recognised page-4 table.
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        has_comparison = any(
            "existing technology comparison" in page.get_text("text").casefold()
            for page in pdf
        )

    if has_comparison:
        from services.enercal_extractor import extract_full_energy_comparison

        comparison = extract_full_energy_comparison(pdf_bytes)
        _crosscheck_results(results, comparison)

    return list(results.values())
