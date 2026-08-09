#!/usr/bin/env python3
"""Flatten every GP voucher JSON in data/raw/vouchers/ into CSV.

Paths resolve relative to the repo root (parent of this script's dir),
so it runs from anywhere.

Outputs:
    data/processed/all_vouchers_flat.csv         (combined, all GPs)
    data/processed/per_gp/<gp>_vouchers_flat.csv (one per input file)

Each input file is validated against its own metadata (receipt_count,
payment_count, total_receipts, total_payments); mismatches are warned,
not fatal.

Usage:
    python scripts/Json_conversion.py                 # default folders
    python scripts/Json_conversion.py <in_dir> <out_dir>
"""
import json
import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

IN_DIR = REPO_ROOT / "data" / "raw" / "vouchers"
OUT_DIR = REPO_ROOT / "data" / "processed"

if len(sys.argv) >= 2:
    IN_DIR = Path(sys.argv[1]).resolve()
if len(sys.argv) >= 3:
    OUT_DIR = Path(sys.argv[2]).resolve()

COMBINED_CSV = OUT_DIR / "all_vouchers_flat.csv"
PER_GP_DIR = OUT_DIR / "per_gp"

COLUMNS = [
    "gp_name", "gp_lgd_code", "state", "district", "block",
    "fiscal_year", "year_status",
    "direction",            # "receipt" or "payment"
    "month", "date", "voucher_no", "type", "amount", "voucher_id",
    "year_receipt_count", "year_payment_count",
    "year_total_receipts", "year_total_payments",
]


def flatten(data):
    rows = []
    meta = {
        "gp_name": data.get("gp_name"),
        "gp_lgd_code": data.get("gp_lgd_code"),
        "state": data.get("state"),
        "district": data.get("district"),
        "block": data.get("block"),
    }
    for fy, ydata in data.get("years", {}).items():
        year_fields = {
            "fiscal_year": fy,
            "year_status": ydata.get("status"),
            "year_receipt_count": ydata.get("receipt_count"),
            "year_payment_count": ydata.get("payment_count"),
            "year_total_receipts": ydata.get("total_receipts"),
            "year_total_payments": ydata.get("total_payments"),
        }
        for direction, key in (("receipt", "receipts"), ("payment", "payments")):
            for v in ydata.get(key, []):
                row = {**meta, **year_fields, "direction": direction}
                row.update({
                    "month": v.get("month"),
                    "date": v.get("date"),
                    "voucher_no": v.get("voucher_no"),
                    "type": v.get("type"),
                    "amount": v.get("amount"),
                    "voucher_id": v.get("voucher_id"),
                })
                rows.append(row)
    return rows


def validate(data, source_file):
    warns = []
    for fy, y in data.get("years", {}).items():
        recs, pays = y.get("receipts", []), y.get("payments", [])
        checks = [
            ("receipt_count", y.get("receipt_count"), len(recs)),
            ("payment_count", y.get("payment_count"), len(pays)),
            ("total_receipts", y.get("total_receipts"),
             round(sum(r.get("amount", 0) for r in recs), 2)),
            ("total_payments", y.get("total_payments"),
             round(sum(p.get("amount", 0) for p in pays), 2)),
        ]
        for label, expected, actual in checks:
            if expected is not None and round(float(expected), 2) != round(float(actual), 2):
                warns.append(
                    f"  [{source_file} {fy}] {label}: metadata={expected} computed={actual}")
    return warns


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def main():
    if not IN_DIR.is_dir():
        sys.exit(f"Input directory not found: {IN_DIR}")

    files = sorted(IN_DIR.glob("*.json"))
    if not files:
        sys.exit(f"No .json files found in {IN_DIR}")

    all_rows = []
    all_warns = []
    print(f"Found {len(files)} file(s) in {IN_DIR}\n")

    for fp in files:
        try:
            data = json.load(open(fp, encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  SKIP {fp.name}: {e}")
            all_warns.append(f"  [{fp.name}] could not be read: {e}")
            continue

        rows = flatten(data)
        all_rows.extend(rows)

        write_csv(PER_GP_DIR / f"{fp.stem}_flat.csv", rows)

        warns = validate(data, fp.name)
        all_warns.extend(warns)
        flag = "OK " if not warns else "WARN"
        gp = data.get("gp_name", "?")
        print(f"  {flag} {fp.name:40} gp={gp:12} rows={len(rows)}")

    write_csv(COMBINED_CSV, all_rows)

    print(f"\nCombined: {len(all_rows)} rows -> {COMBINED_CSV}")
    print(f"Per-GP CSVs -> {PER_GP_DIR}/")

    if all_warns:
        print(f"\n{len(all_warns)} validation warning(s):")
        print("\n".join(all_warns))

    return len(all_rows)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)