"""
Merge and flatten all scraped eGramSwaraj panchayat JSON files into a single CSV master file.

Each JSON file has nested sections (Basic Info, Demographic Details, Health, etc.).
This script flattens every section into columns prefixed by section name, then combines
all panchayats into one master CSV.

Usage:
    python flatten_to_csv.py
    python flatten_to_csv.py --input-dir /path/to/jsons --output /path/to/master.csv
"""

import os
import sys
import json
import csv
import re
import argparse
from pathlib import Path

# Adjust Python path for the project structure
# flatten_to_csv.py is at <project_root>/src/eGramSwaraj_panchayat_profile/flatten_to_csv.py
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.append(str(project_root / "src"))

try:
    from eGramSwaraj_panchayat_profile.config import OUTPUT_DIR
except ModuleNotFoundError:
    # Fallback: resolve OUTPUT_DIR dynamically
    OUTPUT_DIR = project_root / "data" / "raw" / "eGramSwaraj_panchayat_profile"


def clean_key(section: str, key: str) -> str:
    """
    Creates a clean, CSV-friendly column name from a section name and key.
    E.g. ("Demographic Details (As on Date)", "Male Population :") -> "demographic_details__male_population"
    """
    # Remove trailing colons and parenthetical notes
    cleaned = f"{section}__{key}"
    cleaned = cleaned.replace(" :", "").replace(":", "")
    cleaned = re.sub(r'\(.*?\)', '', cleaned)        # remove parenthetical text
    cleaned = cleaned.strip().lower()
    cleaned = re.sub(r'[^a-z0-9]+', '_', cleaned)    # replace non-alphanumeric with _
    cleaned = re.sub(r'_+', '_', cleaned)             # collapse multiple underscores
    cleaned = cleaned.strip('_')
    return cleaned


def clean_value(value: str) -> str:
    """Clean whitespace/tab artifacts from scraped values."""
    if not isinstance(value, str):
        return value
    # Replace tabs and excessive newlines with a single space
    cleaned = re.sub(r'[\t\n\r]+', ' ', value)
    cleaned = re.sub(r'\s+', ' ', cleaned)
    return cleaned.strip()


def flatten_json(filepath: Path) -> dict:
    """
    Reads a single panchayat JSON file and returns a flat dictionary
    with cleaned column names and values.
    """
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    flat = {}

    # Sections to skip (request_params is metadata, not panchayat data;
    # "Panchayat At a Glance" duplicates "Basic Info")
    skip_sections = {"request_params", "Panchayat At a Glance"}

    for section, content in data.items():
        if section in skip_sections:
            continue

        if isinstance(content, dict):
            for key, value in content.items():
                col_name = clean_key(section, key)
                flat[col_name] = clean_value(value)
        elif isinstance(content, list):
            # Some sections may have a list of table dicts
            for i, table in enumerate(content):
                if isinstance(table, dict):
                    for key, value in table.items():
                        col_name = clean_key(section, key)
                        if i > 0:
                            col_name = f"{col_name}_{i+1}"
                        flat[col_name] = clean_value(value)

    # Also extract key identifiers from request_params
    req = data.get("request_params", {})
    for key in ["stateId", "localBodyTypeCode", "label1", "label11", "label111",
                "gp_name", "bp_name", "zp_name"]:
        if key in req:
            flat[f"param__{key.lower()}"] = req[key]

    return flat


def merge_and_flatten(input_dir: Path, output_file: Path):
    """
    Reads all panchayat_*.json files from input_dir,
    flattens each one, and writes them all into a single CSV.
    """
    json_files = sorted(input_dir.glob("panchayat_*.json"))

    if not json_files:
        print(f"No panchayat JSON files found in {input_dir}")
        return

    print(f"Found {len(json_files)} JSON files to flatten.")

    # First pass: collect all unique column names across all files
    all_rows = []
    all_columns = set()

    for i, jf in enumerate(json_files):
        try:
            flat = flatten_json(jf)
            all_rows.append(flat)
            all_columns.update(flat.keys())
        except Exception as e:
            print(f"Error processing {jf.name}: {e}")

        if (i + 1) % 500 == 0:
            print(f"  Processed {i + 1}/{len(json_files)} files...")

    # Sort columns for consistent ordering
    # Put identifiers first, then alphabetical
    id_cols = [c for c in sorted(all_columns) if c.startswith("param__")]
    basic_cols = [c for c in sorted(all_columns) if c.startswith("basic_info__")]
    other_cols = sorted(all_columns - set(id_cols) - set(basic_cols))
    ordered_columns = id_cols + basic_cols + other_cols

    # Write CSV
    os.makedirs(output_file.parent, exist_ok=True)

    with open(output_file, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=ordered_columns, extrasaction='ignore')
        writer.writeheader()
        for row in all_rows:
            writer.writerow(row)

    print(f"\nMaster CSV saved to: {output_file}")
    print(f"  Total rows: {len(all_rows)}")
    print(f"  Total columns: {len(ordered_columns)}")


def main():
    parser = argparse.ArgumentParser(description="Flatten scraped panchayat JSONs into a master CSV")
    parser.add_argument("--input-dir", type=Path, default=OUTPUT_DIR,
                        help=f"Directory containing panchayat_*.json files (default: {OUTPUT_DIR})")
    parser.add_argument("--output", type=Path, default=None,
                        help="Output CSV file path (default: <project_root>/data/processed/eGramSwaraj_panchayat_master.csv)")
    args = parser.parse_args()

    if args.output is None:
        args.output = project_root / "data" / "processed" / "eGramSwaraj_panchayat_master.csv"

    merge_and_flatten(args.input_dir, args.output)


if __name__ == "__main__":
    main()
