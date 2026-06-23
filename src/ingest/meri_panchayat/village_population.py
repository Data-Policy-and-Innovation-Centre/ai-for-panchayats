# scripts/villages.py
# Meri_Panchayat_Villages_data_extraction
# Author: Ravishankar Singh
# Date: 16-06-2026

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
    GP_PROFILE_SECRET_KEY,
    build_headers, get_output_path,
)
from base_scraper import get_zps, get_blocks, get_gps, fetch_json, save_outputs

OUTPUT_FILE_JSON = get_output_path(os.path.basename(__file__))
PROFILE_HEADERS = build_headers(GP_PROFILE_SECRET_KEY, lang="null-IN")

# Villages come directly out of profile listings tracked safely across bounding timelines
PROFILE_QUERY_FIN_YEAR = FIN_YEARS[-1]


# ------------------------------------------------------------------
# POPULATION INDEX REGISTRATION LINKING
# ------------------------------------------------------------------
def get_villages(gp_id):
    url = f"{BASE_URL}/api/prd/gp/v1/profile/getGPDetailsWithYr/{STATE_ID}/{gp_id}/3?fYear={PROFILE_QUERY_FIN_YEAR}"
    data = fetch_json(url, PROFILE_HEADERS)
    return data.get("response", {}).get("villages", []) if data else []


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows, processed_gp_count = [], 0

    print("Starting Extraction for Village Demographic Summaries...")
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
                
                villages = get_villages(gp_id)
                print(f"      Villages found: {len(villages)}")

                for village in villages:
                    rows.append({
                        "state_id":     STATE_ID,
                        "state_name":   STATE_NAME,
                        "zp_id":        zp_id,
                        "zp_name":      zp_name,
                        "bp_id":        bp_id,
                        "bp_name":      bp_name,
                        "gp_id":        gp_id,
                        "gp_name":      gp_name,
                        "lgd_code":     village.get("ldgCode"),
                        "village_name": village.get("name"),
                        "population":   village.get("population"),
                    })

                processed_gp_count += 1
                if processed_gp_count % SAVE_EVERY_GP == 0 and rows:
                    save_outputs(pd.DataFrame(rows), OUTPUT_FILE_JSON)
                    print(f"Checkpoint saved after {processed_gp_count} GPs")

                time.sleep(REQUEST_DELAY)

    df = pd.DataFrame(rows)
    save_outputs(df, OUTPUT_FILE_JSON)
    print(f"\n{'='*60}\nProcessed {processed_gp_count} GPs | Saved {len(df)} rows\n{'='*60}")


if __name__ == "__main__":
    main()