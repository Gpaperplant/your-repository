from collections import Counter
from pathlib import Path
from services.enercal_specs import extract_specs

# Replace this with the actual path to your EnerCal PDF.
pdf_path = "sample_data/EnerCal_MOD_8_11_2026_test.pdf"

records = extract_specs(Path(pdf_path).read_bytes())

checks = {
    key: value
    for key, value, _, _ in records
    if key.startswith("check_")
}

print("Total records:", len(records))
print("Cross-check results:", Counter(checks.values()))

for key, status in checks.items():
    if status != "PASS":
        print(key, status)