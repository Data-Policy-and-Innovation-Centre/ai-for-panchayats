# scripts/panchayat_payment_register.py
# Meri_Panchayat_payment_register_data_extraction
# Author: Ravishankar Singh
# Date: 15-06-2026

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
    FUNDS_SECRET_KEY,
    build_headers, get_output_path,
)
from base_scraper import get_zps, get_blocks, get_gps, fetch_json_post, save_outputs

OUTPUT_FILE_JSON = get_output_path(os.path.basename(__file__))
EPO_HEADERS = build_headers(FUNDS_SECRET_KEY, lang="en-IN")
PAGE_LIMIT = 50


# ------------------------------------------------------------------
# REGISTER POST PROCESSING STRATEGY (WITH BUILT-IN PAGINATION)
# ------------------------------------------------------------------
def get_epayment_orders(gp_id, fin_year):
    url = f"{BASE_URL}/api/voucher/v1/getePaymentOrders"
    all_epos, skip = [], 0
    
    while True:
        payload = {
            "stateId": STATE_ID,
            "fYear": fin_year,
            "LocalBodyTypeCode": 3,
            "localBodyCode": gp_id,
            "skip": skip,
            "limit": PAGE_LIMIT
        }
        data = fetch_json_post(url, EPO_HEADERS, payload)
        
        # FIX: If a mid-sequence page requests fails, immediately drop execution
        # and discard any partial data fragments captured on prior pages.
        if data is None:
            return None
            
        response = data.get("response", {})
        epos = response.get("epos", [])
        if not epos:
            break
            
        all_epos.extend(epos)
        skip += PAGE_LIMIT
        
        if len(all_epos) >= response.get("count", 0):
            break
            
    return all_epos


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows, processed_gp_count = [], 0
    failed_gp_years = []

    print("Starting Extraction for E-Payment Orders Ledger...")
    zps = get_zps()
    print(f"Total Districts (ZPs) fetched: {len(zps)}")

    for zp in zps:
        zp_id, zp_name = zp.get("zpId"), zp.get("name")
        print(f"\nDistrict: {zp_name}")

        for block in get_blocks(zp_id):
            bp_id, bp_name = block.get("bpId"), block.get("name")
            print(f"    Block: {bp_name}")

            for gp in get_gps(zp_id, bp_id):
                gp_id, gp_name = gp.get("gpId"), gp.get("name")
                print(f"      GP {processed_gp_count + 1}: {gp_name}")

                for fin_year in FIN_YEARS:
                    epos = get_epayment_orders(gp_id, fin_year)
                    
                    if epos is None:
                        print(f"        -> RETRYING once for Year: {fin_year}")
                        time.sleep(2)
                        epos = get_epayment_orders(gp_id, fin_year)

                    if epos is None:
                        print(f"        [CRITICAL ERROR] Failed year {fin_year}")
                        failed_gp_years.append({
                            "zp_id": zp_id, "zp_name": zp_name,
                            "bp_id": bp_id, "bp_name": bp_name,
                            "gp_id": gp_id, "gp_name": gp_name,
                            "financial_year": fin_year
                        })
                        continue

                    print(f"        Year {fin_year}: Orders count = {len(epos)}")

                    for epo in epos:
                        rows.append({
                            "financial_year":       fin_year,
                            "state_id":             STATE_ID,
                            "state_name":           STATE_NAME,
                            "zp_id":                zp_id,
                            "zp_name":              zp_name,
                            "bp_id":                bp_id,
                            "bp_name":              bp_name,
                            "gp_id":                gp_id,
                            "gp_name":              gp_name,
                            "epo_id":               epo.get("id"),
                            "epo_no":               epo.get("epono"),
                            "epo_date":             epo.get("epodate"),
                            "advice_no":            epo.get("adviceno"),
                            "bank_act_no":          epo.get("bankActNo"),
                            "total_amount":         epo.get("totalAmount"),
                            "scheme_name":          epo.get("schemeName"),
                            "voucher_no":           epo.get("voucherno"),
                            "voucher_date":         epo.get("voucherdate"),
                            "payment_mode":         epo.get("paymentmode"),
                            "signed_by_maker":      epo.get("signedByMaker"),
                            "signed_by_checker":    epo.get("signedByChecker"),
                            "work_code":            epo.get("workCode"),
                            "utr_no":               epo.get("utrno"),
                            "status":               epo.get("status"),
                        })

                    time.sleep(REQUEST_DELAY)

                processed_gp_count += 1
                if processed_gp_count % SAVE_EVERY_GP == 0 and rows:
                    save_outputs(pd.DataFrame(rows), OUTPUT_FILE_JSON)
                    print(f"Checkpoint saved after {processed_gp_count} GPs")

    df = pd.DataFrame(rows)
    save_outputs(df, OUTPUT_FILE_JSON)

    if failed_gp_years:
        failed_path = os.path.join(OUTPUT_DIR, "panchayat_payment_register_FAILED_gp_years.json")
        pd.DataFrame(failed_gp_years).to_json(failed_path, orient="records", indent=2, force_ascii=False)
        print(f"\n*** WARNING: {len(failed_gp_years)} execution chains failed. Fail logs saved at: {failed_path} ***")

    print(f"\n{'='*60}\nProcessed {processed_gp_count} GPs | Saved {len(df)} rows\n{'='*60}")


if __name__ == "__main__":
    main()