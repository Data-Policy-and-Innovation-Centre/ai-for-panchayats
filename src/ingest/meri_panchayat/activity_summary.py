# scripts/activity_summary.py
# Meri_Panchayat_Activity_Summary_data_extraction
# Author: Ravishankar Singh
# Date: 08-06-2026

import os
import sys
import time
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    STATE_ID, STATE_NAME,
    FIN_YEARS,
    OUTPUT_DIR, SAVE_EVERY_GP,
    REQUEST_DELAY, BASE_URL,
    ACTIVITY_SUMMARY_SECRET_KEY,
    build_headers, get_output_path,
)
from base_scraper import get_zps, get_blocks, get_gps, fetch_json, save_outputs

OUTPUT_FILE_JSON = get_output_path(os.path.basename(__file__))
ACTIVITY_HEADERS = build_headers(ACTIVITY_SUMMARY_SECRET_KEY, lang="null-IN")


# ------------------------------------------------------------------
# COMPILATION ENDPOINT LOGIC
# ------------------------------------------------------------------
def get_activity_summary(gp_id, fin_year):
    url = f"{BASE_URL}/api/prd/gp/v1/activity/getactivitysummary/{STATE_ID}/{gp_id}/3/{fin_year}"
    data = fetch_json(url, ACTIVITY_HEADERS)
    return data.get("response", {}).get("works", []) if data else []


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows, processed_gp_count = [], 0

    print("Starting Extraction for Activity Summaries...")
    zps = get_zps()
    print(f"Total Districts (ZPs) fetched: {len(zps)}")

    for zp in zps:
        zp_id, zp_name = zp.get("zpId"), zp.get("name")
        print(f"\nProcessing District: {zp_name} ({zp_id})")

        for block in get_blocks(zp_id):
            bp_id, bp_name = block.get("bpId"), block.get("name")
            print(f"  -> Block: {bp_name} ({bp_id})")

            for gp in get_gps(zp_id, bp_id):
                gp_id, gp_name = gp.get("gpId"), gp.get("name")
                print(f"      + Gram Panchayat: {gp_name} ({gp_id})")

                for fin_year in FIN_YEARS:
                    print(f"        Financial Year: {fin_year}")
                    works = get_activity_summary(gp_id, fin_year)
                    print(f"        Activity categories found: {len(works)}")

                    for work in works:
                        rows.append({
                            "financial_year":         fin_year,
                            "state_id":               STATE_ID,
                            "state_name":             STATE_NAME,
                            "zp_id":                  zp_id,
                            "zp_name":                zp_name,
                            "bp_id":                  bp_id,
                            "bp_name":                bp_name,
                            "gp_id":                  gp_id,
                            "gp_name":              gp_name,
                            "asset_subcategory_id":   work.get("astSubCatId"),
                            "asset_subcategory_name": work.get("astSubCatNm"),
                            "activity_count":         work.get("count"),
                            "icon_url":               work.get("icon"),
                        })

                    time.sleep(REQUEST_DELAY)

                processed_gp_count += 1
                if processed_gp_count % SAVE_EVERY_GP == 0 and rows:
                    save_outputs(pd.DataFrame(rows), OUTPUT_FILE_JSON)
                    print(f"Checkpoint saved after {processed_gp_count} GPs")

    df = pd.DataFrame(rows)
    save_outputs(df, OUTPUT_FILE_JSON)
    print(f"\n{'='*60}\nProcessed {processed_gp_count} GPs | Saved {len(df)} rows\n{'='*60}")


if __name__ == "__main__":
    main()