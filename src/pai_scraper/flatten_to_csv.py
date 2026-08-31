import json
import csv
from pathlib import Path

from .config import OUTPUT_DIR, THEME_COLUMNS

def flatten_json_to_csv(input_json_path: Path, output_csv_path: Path):
    if not input_json_path.exists():
        print(f"Error: {input_json_path} does not exist.")
        return

    with open(input_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    records = data.get("data", [])
    if not records:
        print("No data found to flatten.")
        return

    # Define headers
    headers = ["state", "district", "block", "gram_panchayat", "gp_code"] + THEME_COLUMNS

    with open(output_csv_path, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(records)

    print(f"Flattened {len(records)} records to {output_csv_path}")

if __name__ == "__main__":
    json_path = OUTPUT_DIR / "odisha_pai_scores.json"
    csv_path = OUTPUT_DIR / "odisha_pai_scores.csv"
    flatten_json_to_csv(json_path, csv_path)
