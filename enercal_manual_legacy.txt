
import sys
from pathlib import Path

from services.enercal_extractor import (
    extract_pdf_text,
    extract_energy_comparison
)


# Check if a PDF path was provided
if len(sys.argv) < 2:
    print("Please provide a PDF file path.")
    print("Example:")
    print(
        "uv run python test_enercal.py "
        "sample_data/enercal_sample.pdf"
    )
    sys.exit(1)


# Get the PDF path from the terminal
pdf_path = Path(sys.argv[1])


# Check whether the PDF exists
if not pdf_path.is_file():
    print(f"File not found: {pdf_path}")
    sys.exit(1)


# Read PDF contents
pdf_bytes = pdf_path.read_bytes()


# Extract text using PyMuPDF
pages = extract_pdf_text(pdf_bytes)


# Display results
print(f"Total pages: {len(pages)}")

for page in pages:
    if page["page_number"] == 4:
        print(f"\n--- Page {page['page_number']} ---")
        print(page["extracted_text"])



# Identify EnerCal annual energy values
try:
    energy_result = extract_energy_comparison(pages)

    print("\n--- Extracted Energy Results ---")

    for field, value in energy_result.items():
        print(f"{field}: {value}")

except ValueError as error:
    print(f"Extraction error: {error}")




# Test saving the EnerCal report into PostgreSQL
from services.enercal_repository import save_enercal_report

result = save_enercal_report(
    case_ref="TEST-MOD-001",
    filename=pdf_path.name,
    pdf_bytes=pdf_bytes,
)

print("\n--- PostgreSQL Save Result ---")
print("Document ID:", result["document_id"])
print("Pages saved:", result["pages_saved"])
print("Extracted energy:", result["energy"])
