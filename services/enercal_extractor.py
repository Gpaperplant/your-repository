import pymupdf
import re
from decimal import Decimal


def extract_pdf_text(pdf_bytes: bytes) -> list[dict]:
    """
    Read a PDF and return its text page by page.

    Each result contains:
    - page_number: page number starting from 1
    - extracted_text: text extracted from that page
    """

    results = []

    with pymupdf.open(
        stream=pdf_bytes,
        filetype="pdf"
    ) as document:

        for page_index in range(len(document)):

            page = document[page_index]

            text = page.get_text(
                "text",
                sort=True
            )

            results.append({
                "page_number": page_index + 1,
                "extracted_text": text
            })

    return results


def extract_energy_comparison(pages: list[dict]) -> dict:
    """
    Identify calculated annual energy consumption
    for the existing and proposed elevators.

    This function is designed for the current
    EnerCal report layout.
    """

    for page in pages:
        text = page["extracted_text"]

        # Find the relevant EnerCal comparison table
        if (
            "EXISTING TECHNOLOGY COMPARISON" not in text.upper()
            or "ANNUAL ENERGY" not in text.upper()
        ):
            continue

        # Locate the existing elevator row
        existing_match = re.search(
            r"(?im)^[ \t]*Typical[ \t]+Traction[^\n]*",
            text
        )

        # Locate the proposed modernisation row
        proposed_match = re.search(
            r"(?im)^[ \t]*KONE[ \t]+N[ \t]+MonoSpace[^\n]*",
            text
        )

        if not existing_match or not proposed_match:
            raise ValueError(
                "Could not identify both elevator rows."
            )

        # Extract the first number from each row
        existing_numbers = re.findall(
            r"\b\d+(?:,\d{3})*(?:\.\d+)?\b",
            existing_match.group()
        )

        proposed_numbers = re.findall(
            r"\b\d+(?:,\d{3})*(?:\.\d+)?\b",
            proposed_match.group()
        )

        if not existing_numbers or not proposed_numbers:
            raise ValueError(
                "Annual energy values were not found."
            )

        baseline = float(
            existing_numbers[0].replace(",", "")
        )

        proposed = float(
            proposed_numbers[0].replace(",", "")
        )

        if baseline <= 0 or proposed < 0:
            raise ValueError("Invalid energy values.")

        savings_percent = (
            (baseline - proposed) / baseline
        ) * 100

        return {
            "baseline_annual_energy_kwh": baseline,
            "proposed_annual_energy_kwh": proposed,
            "calculated_saving_percent": round(
                savings_percent, 2
            ),
            "source_page": page["page_number"],
            "review_status": "pending"
        }

    raise ValueError(
        "EnerCal energy comparison table not found."
    )





# Six columns from the EnerCal comparison table
FULL_COMPARISON_FIELDS = (
    "annual_energy_consumption",
    "annual_regenerative_energy",
    "annual_electricity_expenditure",
    "lifetime_electricity_expenditure",
    "operational_co2e",
    "embodied_co2e",
)

# Column positions for this EnerCal PDF layout
COLUMN_BOUNDS = (178, 285, 415, 540, 670, 805, 925)


def extract_full_energy_comparison(pdf_bytes):
    """Extract the complete EnerCal comparison on page 4."""

    with pymupdf.open(
        stream=pdf_bytes,
        filetype="pdf"
    ) as document:

        for page_index, page in enumerate(document):

            text = page.get_text("text").upper()

            # Locate the comparison page
            if (
                "EXISTING TECHNOLOGY COMPARISON"
                not in text
            ):
                continue

            # Reject unexpected layouts
            if abs(page.rect.width - 960) > 25:
                raise ValueError(
                    "Unexpected EnerCal PDF layout."
                )

            words = page.get_text("words")
            positions = {}
            results = {}

            # Identify the start of both elevator rows
            for role, first_word in (
                ("existing", "Typical"),
                ("proposed", "KONE")
            ):

                matches = [
                    w for w in words
                    if w[4] == first_word
                    and w[0] < 170
                    and 350 < w[1] < 435
                ]

                if len(matches) != 1:
                    raise ValueError(
                        f"Cannot identify {role} elevator."
                    )

                positions[role] = matches[0][1]

            # Read the two rows
            for role in ("existing", "proposed"):

                y = positions[role]

                row_words = [
                    w for w in words
                    if y - 1 <= w[1] <= y + 28
                ]

                # Extract the elevator technology name
                name_words = [
                    w for w in row_words
                    if w[0] < COLUMN_BOUNDS[0]
                ]

                technology = " ".join(
                    w[4] for w in sorted(
                        name_words,
                        key=lambda w: (
                            round(w[1], 1),
                            w[0]
                        )
                    )
                )

                values = []

                # Extract the six numerical columns
                for left, right in zip(
                    COLUMN_BOUNDS[:-1],
                    COLUMN_BOUNDS[1:]
                ):

                    cell_words = [
                        w for w in row_words
                        if left <= (w[0] + w[2]) / 2 < right
                    ]

                    raw = " ".join(
                        w[4] for w in sorted(
                            cell_words,
                            key=lambda w: w[0]
                        )
                    ).strip()

                    if raw.upper() == "N/A":
                        number = None

                    elif re.fullmatch(
                        r"\d[\d,]*(?:\.\d+)?",
                        raw
                    ):
                        number = Decimal(
                            raw.replace(",", "")
                        )

                    else:
                        raise ValueError(
                            f"Unexpected {role} value: {raw!r}"
                        )

                    values.append(number)

                if not technology:
                    raise ValueError(
                        f"Missing {role} elevator name."
                    )

                results[role] = {
                    "elevator_technology": technology,
                    **dict(zip(
                        FULL_COMPARISON_FIELDS,
                        values
                    ))
                }

            # Read the 55% displayed on the PDF
            percentages = [
                w[4] for w in words
                if 700 < w[0] < 870
                and positions["proposed"] + 25
                    <= w[1]
                    <= positions["proposed"] + 85
                and re.fullmatch(
                    r"\d+(?:\.\d+)?%",
                    w[4]
                )
            ]

            if len(percentages) != 1:
                raise ValueError(
                    "Reported energy saving not found."
                )

            # Recalculate savings from energy values
            before = results["existing"][
                "annual_energy_consumption"
            ]

            after = results["proposed"][
                "annual_energy_consumption"
            ]

            if (
                before is None or before <= 0
                or after is None or after < 0
            ):
                raise ValueError(
                    "Invalid energy consumption values."
                )

            calculated = (
                (before - after) / before * 100
            ).quantize(Decimal("0.01"))

            reported = Decimal(
                percentages[0].replace("%", "")
            )

            return {
                "source_page": page_index + 1,
                "reported_energy_saving_percent": reported,
                "calculated_energy_saving_percent": calculated,
                "rows": results,
            }

    raise ValueError(
        "EnerCal comparison table not found."
    )
