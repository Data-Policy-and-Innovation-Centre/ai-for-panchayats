# scripts/panchayat_payment_register.py
# Meri_Panchayat_payment_register_data_extraction
# Author: Ravishankar Singh
# Date: 15-06-2026

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
    MASTER_SECRET_KEY, FUNDS_SECRET_KEY,
    build_headers, output_paths,
)

OUTPUT_FILE_CSV, OUTPUT_FILE_JSON = output_paths("panchayat_payment_register")

MASTER_HEADERS = build_headers(MASTER_SECRET_KEY, lang="null-IN")
EPO_HEADERS    = build_headers(FUNDS_SECRET_KEY,  lang="en-IN")

PAGE_LIMIT = 50


# ------------------------------------------------------------------
# REQUEST FUNCTIONS
# ------------------------------------------------------------------
def fetch_json_get(url, headers):
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


def fetch_json_post(url, headers, payload):
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
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
    data = fetch_json_get(url, MASTER_HEADERS)
    return data.get("response", []) if data else []

def get_blocks(zp_id):
    url  = (f"{BASE_URL}/api/prd/master/v1/getBlockPanchayatList/"
            f"{STATE_ID}/{zp_id}/P/2?fYear={HIERARCHY_FIN_YEAR}")
    data = fetch_json_get(url, MASTER_HEADERS)
    return data.get("response", []) if data else []

def get_gps(zp_id, bp_id):
    url  = (f"{BASE_URL}/api/prd/master/v1/getGramPanchayatList/"
            f"{STATE_ID}/{zp_id}/{bp_id}/P/3?fYear={HIERARCHY_FIN_YEAR}")
    data = fetch_json_get(url, MASTER_HEADERS)
    return data.get("response", []) if data else []


# ------------------------------------------------------------------
# ePAYMENT ORDERS API
# ------------------------------------------------------------------
def get_epayment_orders(gp_id, fin_year):
    url      = f"{BASE_URL}/api/voucher/v1/getePaymentOrders"
    all_epos = []
    skip     = 0

    while True:
        payload = {
            "stateId":           STATE_ID,
            "fYear":             fin_year,
            "LocalBodyTypeCode": 3,
            "localBodyCode":     gp_id,
            "skip":              skip,
            "limit":             PAGE_LIMIT,
        }
        data = fetch_json_post(url, EPO_HEADERS, payload)
        if not data:
            break

        response    = data.get("response", {})
        epos        = response.get("epos", [])
        if not epos:
            break

        all_epos.extend(epos)
        total_count = response.get("count", 0)
        skip       += PAGE_LIMIT

        if len(all_epos) >= total_count:
            break

    return all_epos


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
                    epos = get_epayment_orders(gp_id, fin_year)
                    print(f"        ePayment orders found: {len(epos)}")

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
                            "payee_name":           epo.get("to"),
                            "account_no":           epo.get("accountNo"),
                            "ifsc":                 epo.get("ifsc"),
                            "voucher_id":           epo.get("voucherId"),
                            "voucher_no":           epo.get("voucherNo"),
                            "voucher_date":         epo.get("voucherDate"),
                            "epo_created_on":       epo.get("ePOCreatedOn"),
                            "maker_dsc_on":         epo.get("makerDSCOn"),
                            "checker_dsc_on":       epo.get("checkerDSCOn"),
                            "pfms_ack_received_on": epo.get("pfmsAckRxOn"),
                            "bank_ack_received_on": epo.get("BankAckRxOn"),
                            "purpose":              epo.get("purpose"),
                            "amount":               epo.get("amount"),
                            "signed_by_maker":      epo.get("signedByMaker"),
                            "signed_by_checker":    epo.get("signedByChecker"),
                            "work_code":            epo.get("workCode"),
                            "utr_no":               epo.get("utrno"),
                            "status":               epo.get("status"),
                        })

                    time.sleep(REQUEST_DELAY)

                processed_gp_count += 1
                if processed_gp_count % SAVE_EVERY_GP == 0 and rows:
                    save_outputs(pd.DataFrame(rows))
                    print(f"Checkpoint saved after {processed_gp_count} GPs")

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