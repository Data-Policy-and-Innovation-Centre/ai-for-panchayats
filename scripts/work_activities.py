# scripts/activities.py
# Meri_Panchayat_Activity_data_extraction
# Author: Ravishankar Singh
# Date: 07-06-2026

import os
import sys
import time
import requests
import pandas as pd

# Run from project root:  python scripts/activities.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_loader import (
    STATE_ID, STATE_NAME,
    FIN_YEARS, HIERARCHY_FIN_YEAR,
    TARGET_GP_IDS,
    PILOT_GP_LIMIT,
    OUTPUT_DIR, SAVE_EVERY_GP,
    REQUEST_DELAY, REQUEST_TIMEOUT,
    BASE_URL,
    MASTER_SECRET_KEY, ACTIVITIES_SECRET_KEY,
    build_headers, output_paths,
)

OUTPUT_FILE_CSV, OUTPUT_FILE_JSON = output_paths("activities")

MASTER_HEADERS   = build_headers(MASTER_SECRET_KEY,      lang="null-IN")
ACTIVITY_HEADERS = build_headers(ACTIVITIES_SECRET_KEY,  lang="null-IN")


# ------------------------------------------------------------------
# COMMON REQUEST FUNCTION
# ------------------------------------------------------------------
def fetch_json(url, headers):
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"HTTP {response.status_code}: {url}")
            return None
        return response.json()
    except Exception as e:
        print(f"ERROR: {e}")
        return None


# ------------------------------------------------------------------
# MASTER DATA APIS
# ------------------------------------------------------------------
def get_zps():
    url  = f"{BASE_URL}/api/prd/master/v1/getZPList/{STATE_ID}"
    data = fetch_json(url, MASTER_HEADERS)
    return data.get("response", []) if data else []

def get_blocks(zp_id):
    url  = (f"{BASE_URL}/api/prd/master/v1/getBlockPanchayatList/"
            f"{STATE_ID}/{zp_id}/P/2?fYear={HIERARCHY_FIN_YEAR}")
    data = fetch_json(url, MASTER_HEADERS)
    return data.get("response", []) if data else []

def get_gps(zp_id, bp_id):
    url  = (f"{BASE_URL}/api/prd/master/v1/getGramPanchayatList/"
            f"{STATE_ID}/{zp_id}/{bp_id}/P/3?fYear={HIERARCHY_FIN_YEAR}")
    data = fetch_json(url, MASTER_HEADERS)
    return data.get("response", []) if data else []


# ------------------------------------------------------------------
# ACTIVITY API
# ------------------------------------------------------------------
def get_activities(gp_id, fin_year):
    limit, skip, all_activities = 100, 0, []

    while True:
        url  = (f"{BASE_URL}/api/prd/gp/v1/activity/getactivitylist/"
                f"{STATE_ID}/{gp_id}/3/F/0/{fin_year}?skip={skip}&limit={limit}")
        data = fetch_json(url, ACTIVITY_HEADERS)
        if not data:
            break

        response   = data.get("response", {})
        activities = response.get("activities", [])
        if not activities:
            break

        all_activities.extend(activities)
        total_count = response.get("count", 0)
        skip += limit
        if len(all_activities) >= total_count:
            break

    return all_activities


# ------------------------------------------------------------------
# OUTPUT HELPER
# ------------------------------------------------------------------
def save_outputs(df):
    df.to_csv(OUTPUT_FILE_CSV, index=False, encoding="utf-8-sig")
    df.to_json(OUTPUT_FILE_JSON, orient="records", indent=2, force_ascii=False)


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows, processed_gp_count, stop_processing = [], 0, False

    zps = get_zps()
    print(f"Found {len(zps)} districts")

    for zp in zps:
        if stop_processing:
            break
        zp_id, zp_name = zp.get("zpId"), zp.get("name")
        print(f"\nDistrict: {zp_name}")

        for block in get_blocks(zp_id):
            if stop_processing:
                break
            bp_id, bp_name = block.get("bpId"), block.get("name")
            print(f"  Block: {bp_name}")

            for gp in get_gps(zp_id, bp_id):
                gp_id, gp_name = gp.get("gpId"), gp.get("name")
                if gp_id not in TARGET_GP_IDS:
                    continue
                if PILOT_GP_LIMIT is not None and processed_gp_count >= PILOT_GP_LIMIT:
                    stop_processing = True
                    break

                print(f"    GP {processed_gp_count + 1}: {gp_name}")

                for fin_year in FIN_YEARS:
                    print(f"      {fin_year}")
                    activities = get_activities(gp_id, fin_year)
                    print(f"        Activities found: {len(activities)}")

                    for activity in activities:
                        asset_details = activity.get("assetDetails", {})
                        assets        = asset_details.get("asts", []) or [{}]

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
                                "theme":             activity.get("theme"),
                                "focusarea":         activity.get("focusarea"),
                                "activity_type":     activity.get("activityIs"),
                                "activity_category": activity.get("type"),
                                "scheme":            activity.get("scheme"),
                                "component":         activity.get("component"),
                                "status_code":       activity.get("statusCode"),
                                "activity_status":   activity.get("activity_stts"),
                                "current_status":    activity.get("currentStatus"),
                                "is_completed":      activity.get("isCompleted"),
                                "register_on":       activity.get("registerOn"),
                                "sanction_date":     activity.get("sanctionDate"),
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
                    save_outputs(pd.DataFrame(rows))
                    print(f"Checkpoint saved after {processed_gp_count} GPs")

    df = pd.DataFrame(rows)
    save_outputs(df)
    print(f"\n{'='*60}\nProcessed {processed_gp_count} GPs | Saved {len(df)} rows\n{'='*60}")

if __name__ == "__main__":
    main()