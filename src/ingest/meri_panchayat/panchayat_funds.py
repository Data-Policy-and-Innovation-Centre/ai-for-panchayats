# scripts/funds.py
# Meri_Panchayat_Funds_data_extraction
# Author: Ravishankar Singh

import os
import time
import pandas as pd


from .config import (
    in_scope,
    STATE_ID, STATE_NAME, FIN_YEARS, OUTPUT_DIR, SAVE_EVERY_GP,
    REQUEST_DELAY, BASE_URL,
    build_headers, get_output_path
)
from .base_scraper import response_field, get_zps, get_blocks, get_gps, fetch_json, save_outputs

OUTPUT_FILE_JSON = get_output_path(os.path.basename(__file__))
HEADERS = build_headers("funds", lang="en-IN")

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
    return response_field(data, "funds", url, f"GP {gp_id} {fin_year}")

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows, processed_gp_count = [], 0
    seen_gps: set = set()

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

                if not in_scope(gp_id):
                    continue
                # A GP that moved between blocks during FIN_YEARS is
                # returned by get_gps under both its old and new block, so the
                # traversal reaches it twice. This adapter's rows carry no
                # block identifier, so those duplicates are byte-identical and
                # silently double every fund total downstream. Process each GP
                # once. Adapters that do record bp_id have distinguishable
                # duplicates and a live question about which parent is
                # authoritative -- see #42.
                if gp_id in seen_gps:
                    continue
                seen_gps.add(gp_id)

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