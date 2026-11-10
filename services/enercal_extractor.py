
"""Extract the comparison from KONE's 12-page EnerCal MOD report."""

import re
import unicodedata
from decimal import Decimal, ROUND_HALF_UP

import pymupdf


COMPARISON_FIELDS = (
    "annual_energy_consumption",
    "annual_regenerative_energy",
    "annual_electricity_expenditure",
    "lifetime_electricity_expenditure",
    "operational_co2e",
    "embodied_co2e",
)

# Boundaries of the six numeric columns in the 792-point report.
COLUMN_BOUNDS = (153, 239, 339, 434, 531, 634, 745)


def _parse_comparison(page):
    """Read the existing/proposed rows on page 4."""
    if abs(page.rect.width - 792) > 1 or abs(page.rect.height - 632) > 1:
        raise ValueError("Unsupported MOD PDF page dimensions.")

    text = unicodedata.normalize("NFKC", page.get_text("text"))
    if "EXISTING TECHNOLOGY COMPARISON" not in text.upper():
        raise ValueError("MOD comparison heading not found on page 4.")

    words = page.get_text("words")
    headers = [
        word for word in words
        if word[4] == "Technology" and word[0] < COLUMN_BOUNDS[0]
        and 300 < word[1] < 365
    ]
    if len(headers) != 1:
        raise ValueError("Cannot locate the comparison table header.")

    header_y = headers[0][1]
    energy_words = sorted(
        (
            word for word in words
            if COLUMN_BOUNDS[0] <= (word[0] + word[2]) / 2 < COLUMN_BOUNDS[1]
            and header_y + 8 < word[1] < header_y + 95
            and re.fullmatch(r"\d[\d,]*(?:\.\d+)?", word[4])
        ),
        key=lambda word: word[1],
    )
    if len(energy_words) != 2:
        raise ValueError("Expected exactly two elevator rows on page 4.")

    rows = {}
    for role, anchor in zip(("existing", "proposed"), energy_words):
        row_words = [word for word in words if abs(word[1] - anchor[1]) <= 15]
        technology = " ".join(
            word[4] for word in sorted(
                (word for word in row_words if word[0] < COLUMN_BOUNDS[0]),
                key=lambda word: (word[1], word[0]),
            )
        )
        if not technology:
            raise ValueError(f"Missing {role} elevator technology.")

        values = []
        for left, right in zip(COLUMN_BOUNDS[:-1], COLUMN_BOUNDS[1:]):
            cell = [
                word[4] for word in row_words
                if left <= (word[0] + word[2]) / 2 < right
            ]
            if len(cell) != 1 or not re.fullmatch(r"N/A|\d[\d,]*(?:\.\d+)?", cell[0]):
                raise ValueError(f"Invalid {role} comparison cell: {cell}.")
            values.append(None if cell[0] == "N/A" else Decimal(cell[0].replace(",", "")))

        rows[role] = {
            "elevator_technology": technology,
            **dict(zip(COMPARISON_FIELDS, values)),
        }

    saving_words = [
        word[4] for word in words
        if word[0] > 500
        and energy_words[1][1] + 10 < word[1] < energy_words[1][1] + 60
        and re.fullmatch(r"\d+(?:\.\d+)?%", word[4])
    ]
    currency_options = set(re.findall(r"\[([^/\]\s]+)/(?:year|lifetime)\]", text)) - {"kWh"}
    lifetime = re.search(r"\[kgCO2e/lifetime\s*(\d+)\s*years\]", text)
    if len(saving_words) != 1 or len(currency_options) != 1 or not lifetime:
        raise ValueError("Missing savings, currency, or lifetime on page 4.")

    existing = rows["existing"]["annual_energy_consumption"]
    proposed = rows["proposed"]["annual_energy_consumption"]
    if existing is None or existing <= 0 or proposed is None or proposed < 0:
        raise ValueError("Invalid annual energy values.")

    return {
        "source_page": 4,
        "rows": rows,
        "currency_symbol": currency_options.pop(),
        "lifetime_years": int(lifetime.group(1)),
        "reported_energy_saving_percent": Decimal(saving_words[0][:-1]),
        "calculated_energy_saving_percent": (
            (existing - proposed) / existing * 100
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
    }


def _page_texts(pdf):
    return [
        {"page_number": index + 1, "extracted_text": page.get_text("text", sort=True)}
        for index, page in enumerate(pdf)
    ]


def extract_full_energy_comparison(pdf_bytes):
    """Extract the 16 comparison fields from one MOD PDF."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if len(pdf) != 12:
            raise ValueError("Expected a 12-page EnerCal MOD PDF.")
        return _parse_comparison(pdf[3])


def extract_pdf_text(pdf_bytes):
    """Return page text for database storage."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        return _page_texts(pdf)


def extract_mod_document(pdf_bytes):
    """Extract page text and comparison in a single PDF read."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if len(pdf) != 12:
            raise ValueError("Expected a 12-page EnerCal MOD PDF.")
        comparison = _parse_comparison(pdf[3])
        pages = _page_texts(pdf)
    return {"comparison": comparison, "pages": pages}
