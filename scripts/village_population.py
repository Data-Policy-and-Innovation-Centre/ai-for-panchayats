# scripts/villages.py
# Meri_Panchayat_Villages_data_extraction
# Author: Ravishankar Singh
# Date: 16-06-2026

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
    MASTER_SECRET_KEY, GP_PROFILE_SECRET_KEY,
    build_headers, output_paths,
)

OUTPUT_FILE_CSV, OUTPUT_FILE_JSON = output_paths("villages")

# Villages come from the GP profile endpoint, queried once with latest year
PROFILE_QUERY_FIN_YEAR = FIN_YEARS[-1]

MASTER_HEADERS  = build_headers(MASTER_SECRET_KEY,     lang="null-IN")
PROFILE_HEADERS = build_headers(GP_PROFILE_SECRET_KEY, lang="null-IN")


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
# VILLAGES API
# ------------------------------------------------------------------
def get_villages(gp_id):
    url  = (f"{BASE_URL}/api/prd/gp/v1/profile/getGPDetailsWithYr/"
            f"{STATE_ID}/{gp_id}/3?fYear={PROFILE_QUERY_FIN_YEAR}")
    data = fetch_json(url, PROFILE_HEADERS)
    if not data:
        return []
    return data.get("response", {}).get("villages", [])


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
                    save_outputs(pd.DataFrame(rows))
                    print(f"Checkpoint saved after {processed_gp_count} GPs")

                time.sleep(REQUEST_DELAY)

    df = pd.DataFrame(rows)
    save_outputs(df)

    print()
    print("=" * 60)
    print(f"Processed {processed_gp_count} Gram Panchayats")
    print(f"Saved {len(df)} rows")
    print(f"Output CSV:  {OUTPUT_FILE_CSV}")
    print(f"Output JSON: {OUTPUT_FILE_JSON}")
    print("=" * 60)


if __name__ == "__main__":
    main()