# scripts/beneficiaries.py
# Meri_Panchayat_Beneficiary_data_extraction
# Author: Ravishankar Singh

import os
import sys
import time
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    STATE_ID, STATE_NAME, FIN_YEARS, OUTPUT_DIR, SAVE_EVERY_GP,
    REQUEST_DELAY, BASE_URL, BENEFICIARIES_SECRET_KEY,
    build_headers, get_output_path
)
from base_scraper import get_zps, get_blocks, get_gps, fetch_json_post, save_outputs

OUTPUT_FILE_JSON = get_output_path(os.path.basename(__file__))
HEADERS = build_headers(BENEFICIARIES_SECRET_KEY, lang="null-IN")

def get_beneficiaries(gp_id, fin_year):
    url = f"{BASE_URL}/api/beneficiary/v1/getSchemeWiseBeneficiariesCount"
    payload = {
        "stateId":           STATE_ID,
        "fYear":             fin_year,
        "LocalBodyTypeCode": 3,
        "localBodyCode":     gp_id,
    }
    data = fetch_json_post(url, headers=HEADERS, payload=payload)
    if not data:
        return []
    return data.get("response", {}).get("schms", [])

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows, processed_gp_count = [], 0

    zps = get_zps()
    print(f"Total Districts (ZPs) fetched: {len(zps)}")

    for zp in zps:
        zp_id, zp_name = zp.get("zpId"), zp.get("name")
        print(f"\nDistrict: {zp_name}")

        for block in get_blocks(zp_id):
            bp_id, bp_name = block.get("bpId"), block.get("name")
            print(f"  Block: {bp_name}")

            for gp in get_gps(zp_id, bp_id):
                gp_id, gp_name = gp.get("gpId"), gp.get("name")
                print(f"    GP {processed_gp_count + 1}: {gp_name}")

                for fin_year in FIN_YEARS:
                    print(f"      {fin_year}")
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

                processed_gp_count += 1
                if processed_gp_count % SAVE_EVERY_GP == 0 and rows:
                    save_outputs(pd.DataFrame(rows), json_path=OUTPUT_FILE_JSON)
                    print(f"Checkpoint saved after {processed_gp_count} GPs")

    df = pd.DataFrame(rows)
    save_outputs(df, json_path=OUTPUT_FILE_JSON)
    print(f"\n{'='*60}\nProcessed {processed_gp_count} Panchayats | Rows saved: {len(df)}\n{'='*60}")

if __name__ == "__main__":
    main()