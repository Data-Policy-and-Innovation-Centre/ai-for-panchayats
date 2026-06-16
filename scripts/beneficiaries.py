# scripts/beneficiaries.py
# Meri_Panchayat_Beneficiary_data_extraction
# Author: Ravishankar Singh

import os
import sys
import time
import requests
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_loader import (
    STATE_ID, STATE_NAME,
    FIN_YEARS,
    TARGET_GPS,
    OUTPUT_DIR,
    REQUEST_DELAY, REQUEST_TIMEOUT,
    BASE_URL,
    BENEFICIARIES_SECRET_KEY,
    build_headers, output_paths,
)

# beneficiaries only saves JSON (matches original script)
_, OUTPUT_FILE_JSON = output_paths("beneficiaries")

HEADERS = build_headers(BENEFICIARIES_SECRET_KEY, lang="null-IN")


# ------------------------------------------------------------------
# COMMON REQUEST FUNCTION  (POST — this endpoint requires POST)
# ------------------------------------------------------------------
def fetch_json(url, payload):
    try:
        response = requests.post(url, headers=HEADERS, json=payload, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"HTTP {response.status_code}")
            return None
        return response.json()
    except Exception as e:
        print(f"ERROR: {e}")
        return None


# ------------------------------------------------------------------
# BENEFICIARY API
# ------------------------------------------------------------------
def get_beneficiaries(gp_id, fin_year):
    url     = f"{BASE_URL}/api/beneficiary/v1/getSchemeWiseBeneficiariesCount"
    payload = {
        "stateId":           STATE_ID,
        "fYear":             fin_year,
        "LocalBodyTypeCode": 3,
        "localBodyCode":     gp_id,
    }
    data = fetch_json(url, payload)
    if not data:
        return []
    return data.get("response", {}).get("schms", [])


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows = []

    print(f"Processing {len(TARGET_GPS)} Panchayats")

    for gp_index, (gp_id, gp_name) in enumerate(TARGET_GPS, start=1):
        print(f"\n[{gp_index}/{len(TARGET_GPS)}] {gp_name}")

        for fin_year in FIN_YEARS:
            print(f"   {fin_year}")
            schemes = get_beneficiaries(gp_id, fin_year)

            for scheme in schemes:
                try:
                    count = int(scheme.get("bnfcriesCunt", 0))
                except (TypeError, ValueError):
                    count = 0

                rows.append({
                    "financial_year":    fin_year,
                    "state_id":          STATE_ID,
                    "state_name":        STATE_NAME,
                    "gp_id":             gp_id,
                    "gp_name":           gp_name,
                    "scheme_code":       scheme.get("cd"),
                    "scheme_name":       scheme.get("nm"),
                    "beneficiary_count": count,
                })

            time.sleep(REQUEST_DELAY)

    df = pd.DataFrame(rows)
    df.to_json(OUTPUT_FILE_JSON, orient="records", indent=2, force_ascii=False)

    print(f"\n{'='*60}\nProcessed {len(TARGET_GPS)} Panchayats | Rows saved: {len(df)}\n{'='*60}")

if __name__ == "__main__":
    main()