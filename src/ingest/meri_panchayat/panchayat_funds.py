# scripts/funds.py
# Meri_Panchayat_Funds_data_extraction
# Author: Ravishankar Singh

import os
import sys
import time
import pandas as pd

# Add the parent directory to the path so config and base_scraper can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    STATE_ID, STATE_NAME, FIN_YEARS, OUTPUT_DIR, SAVE_EVERY_GP,
    REQUEST_DELAY, BASE_URL, FUNDS_SECRET_KEY,
    build_headers, get_output_path
)
from base_scraper import get_zps, get_blocks, get_gps, fetch_json, save_outputs

OUTPUT_FILE_JSON = get_output_path(os.path.basename(__file__))
HEADERS = build_headers(FUNDS_SECRET_KEY, lang="en-IN")

def clean_numeric(val):
    """Helper to convert string numbers with commas into proper integers/floats."""
    if val is None:
        return 0
    try:
        return int(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        try:
            return float(str(val).replace(",", "").strip())
        except (ValueError, TypeError):
            return 0

def get_funds(gp_id, fin_year):
    """Fetches the fund metrics using an HTTP GET request with query parameters."""
    url = (
        f"{BASE_URL}/api/scheme/v1/getFund?"
        f"stateId={STATE_ID}&"
        f"fYear={fin_year}&"
        f"LocalBodyTypeCode=3&"
        f"localBodyCode={gp_id}"
    )
    
    data = fetch_json(url, headers=HEADERS)
    if not data:
        return []
    return data.get("response", {}).get("funds", [])

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
                    print(f"      Requesting FY: {fin_year}")
                    funds_list = get_funds(gp_id, fin_year)

                    for fund in funds_list:
                        rows.append({
                            "requested_financial_year": fin_year,
                            "state_id":                 STATE_ID,
                            "state_name":               STATE_NAME,
                            "gp_id":                    gp_id,
                            "gp_name":                  gp_name,
                            "plan_year":                fund.get("planYear"),
                            "expected_fund":            clean_numeric(fund.get("expctdFund")),
                            "previous_year_balance":    clean_numeric(fund.get("prevusYrBlnce")),
                            "actual_fund_received":     clean_numeric(fund.get("actulFundRcvd")),
                            "actual_expenditure":       clean_numeric(fund.get("actulExpndtur")),
                        })
                    
                    time.sleep(REQUEST_DELAY)

                processed_gp_count += 1
                
                # Checkpoint saving matching your framework architecture (every 100 GPs)
                if processed_gp_count % SAVE_EVERY_GP == 0 and rows:
                    save_outputs(pd.DataFrame(rows), json_path=OUTPUT_FILE_JSON)
                    print(f"Checkpoint saved after {processed_gp_count} GPs")

    # Final execution save for all gathered data
    df = pd.DataFrame(rows)
    save_outputs(df, json_path=OUTPUT_FILE_JSON)
    print(f"\n{'='*60}\nPRODUCTION RUN COMPLETE | Processed {processed_gp_count} Panchayats | Rows saved: {len(df)}\n{'='*60}")

if __name__ == "__main__":
    main()