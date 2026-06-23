# scripts/activities.py
# Meri_Panchayat_Activity_data_extraction
# Author: Ravishankar Singh
# Date: 07-06-2026

import os
import sys
import time
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    STATE_ID, STATE_NAME,
    FIN_YEARS,
    TARGET_GP_IDS,
    OUTPUT_DIR, SAVE_EVERY_GP,
    REQUEST_DELAY, BASE_URL,
    ACTIVITIES_SECRET_KEY,
    build_headers, output_paths,
)
from base_scraper import get_zps, get_blocks, get_gps, fetch_json, save_outputs

OUTPUT_FILE_CSV, OUTPUT_FILE_JSON = output_paths("activities")
ACTIVITY_HEADERS = build_headers(ACTIVITIES_SECRET_KEY, lang="null-IN")


# ------------------------------------------------------------------
# PAGINATED WORK LOGISTICS RETRIEVAL
# ------------------------------------------------------------------
def get_activities(gp_id, fin_year):
    limit, skip, all_activities = 100, 0, []
    
    while True:
        url = f"{BASE_URL}/api/prd/gp/v1/activity/getactivitylist/{STATE_ID}/{gp_id}/3/F/0/{fin_year}?skip={skip}&limit={limit}"
        data = fetch_json(url, ACTIVITY_HEADERS)
        if not data:
            break
            
        activities = data.get("response", {}).get("activities", [])
        if not activities:
            break
            
        all_activities.extend(activities)
        skip += limit
        
        if len(all_activities) >= data.get("response", {}).get("count", 0):
            break
            
    return all_activities


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows, processed_gp_count = [], 0

    print("Starting Extraction for Work Activities Registry...")
    zps = get_zps()
    print(f"Total Districts (ZPs) fetched: {len(zps)}")

    for zp in zps:
        zp_id, zp_name = zp.get("zpId"), zp.get("name")
        print(f"\nProcessing District: {zp_name}")

        for block in get_blocks(zp_id):
            bp_id, bp_name = block.get("bpId"), block.get("name")
            print(f"  -> Block: {bp_name}")

            for gp in get_gps(zp_id, bp_id):
                gp_id, gp_name = gp.get("gpId"), gp.get("name")

                if gp_id not in TARGET_GP_IDS:
                    continue

                print(f"      + GP: {gp_name} ({gp_id})")

                for fin_year in FIN_YEARS:
                    activities = get_activities(gp_id, fin_year)

                    for activity in activities:
                        asset_details = activity.get("assetDetails", {})
                        assets = asset_details.get("assets", [])
                        
                        # Guard condition handling loops gracefully on single-depth or missing items
                        if not assets:
                            assets = [{}]

                        for asset in assets:
                            rows.append({
                                "financial_year":    fin_year,
                                "state_id":          STATE_ID,
                                "state_name":        STATE_NAME,
                                "zp_id":             zp_id,
                                "zp_name":           zp_name,
                                "bp_id":             bp_id,
                                "bp_name":           bp_name,
                                "gp_id":             gp_id,
                                "gp_name":           gp_name,
                                "activity_code":     activity.get("code"),
                                "activity_name":     activity.get("name"),
                                "activity_type":     activity.get("type"),
                                "focus_area_id":     activity.get("focusAreaId"),
                                "focus_area_name":   activity.get("focusAreaNm"),
                                "expected_amount":   activity.get("expectedAmount"),
                                "paid_amount":       activity.get("paidAmount"),
                                "rating":            activity.get("rating"),
                                "review_count":      activity.get("reviewcounts"),
                                "asset_type":        asset_details.get("type"),
                                "asset_category":    asset_details.get("category"),
                                "asset_subcategory": asset_details.get("subCategory"),
                                "total_unit":        asset_details.get("totalUnit"),
                                "unit_cost":         asset_details.get("unitsCost"),
                                "asset_id":          asset.get("id"),
                                "asset_name":        asset.get("astNm"),
                                "asset_stage":       asset.get("assetStage"),
                                "unit_type":         asset.get("unitType"),
                            })

                    time.sleep(REQUEST_DELAY)

                processed_gp_count += 1
                if processed_gp_count % SAVE_EVERY_GP == 0 and rows:
                    save_outputs(pd.DataFrame(rows), OUTPUT_FILE_CSV, OUTPUT_FILE_JSON)
                    print(f"Checkpoint saved after {processed_gp_count} GPs")

    df = pd.DataFrame(rows)
    save_outputs(df, OUTPUT_FILE_CSV, OUTPUT_FILE_JSON)
    print(f"\n{'='*60}\nProcessed {processed_gp_count} GPs | Saved {len(df)} rows\n{'='*60}")


if __name__ == "__main__":
    main()