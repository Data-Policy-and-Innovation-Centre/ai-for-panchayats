# scripts/activity_summary.py
# Meri_Panchayat_Activity_Summary_data_extraction
# Author: Ravishankar Singh
# Date: 08-06-2026

import os
import sys
import time
import requests
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_loader import (
    STATE_ID, STATE_NAME,
    FIN_YEARS, HIERARCHY_FIN_YEAR,
    TARGET_GP_IDS,
    PILOT_GP_LIMIT,
    OUTPUT_DIR, SAVE_EVERY_GP,
    REQUEST_DELAY, REQUEST_TIMEOUT,
    BASE_URL,
    MASTER_SECRET_KEY, ACTIVITY_SUMMARY_SECRET_KEY,
    build_headers, output_paths,
)

OUTPUT_FILE_CSV, OUTPUT_FILE_JSON = output_paths("activity_summary")

MASTER_HEADERS   = build_headers(MASTER_SECRET_KEY,           lang="null-IN")
ACTIVITY_HEADERS = build_headers(ACTIVITY_SUMMARY_SECRET_KEY, lang="null-IN")


# ------------------------------------------------------------------
# COMMON REQUEST FUNCTION
# ------------------------------------------------------------------
def fetch_json(url, headers):
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            print(f"HTTP {response.status_code}: {url}")
            print(response.text[:500])
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
# ACTIVITY SUMMARY API
# ------------------------------------------------------------------
def get_activity_summary(gp_id, fin_year):
    url  = (f"{BASE_URL}/api/prd/gp/v1/activity/getactivitySummary/"
            f"{STATE_ID}/{gp_id}/3/{fin_year}")
    data = fetch_json(url, ACTIVITY_HEADERS)
    if not data:
        return []
    return data.get("response", {}).get("works", [])


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
                            "gp_name":                gp_name,
                            "asset_subcategory_id":   work.get("astSubCatId"),
                            "asset_subcategory_name": work.get("astSubCatNm"),
                            "activity_count":         work.get("count"),
                            "icon_url":               work.get("icon"),
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