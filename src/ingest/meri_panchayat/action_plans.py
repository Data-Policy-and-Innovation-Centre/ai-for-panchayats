# scripts/action_plans.py
# Meri_Panchayat_Action_Plan_data_extraction
# Author: Ravishankar Singh

import os
import sys
import time
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (
    STATE_ID, STATE_NAME, FIN_YEARS,
    OUTPUT_DIR, SAVE_EVERY_GP, REQUEST_DELAY, BASE_URL,
    ACTION_PLANS_SECRET_KEY, build_headers, get_output_path
)
from base_scraper import get_zps, get_blocks, get_gps, fetch_json, save_outputs

OUTPUT_FILE_JSON = get_output_path(os.path.basename(__file__))
ACTION_PLAN_BASE_HEADERS = build_headers(ACTION_PLANS_SECRET_KEY, lang="en-IN")

def get_current_timestamp():
    """Returns timestamp in DDMMYYYYHHMMSS format (IST)."""
    return datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d%m%Y%H%M%S")

def get_action_plans(gp_id, fin_year):
    url = f"{BASE_URL}/api/plan/v1/get/actionplans"
    request_headers = ACTION_PLAN_BASE_HEADERS.copy()
    request_headers.update({
        "stateid": str(STATE_ID),
        "gpid": str(gp_id),
        "fyear": fin_year,
        "timestamp": get_current_timestamp()
    })

    data = fetch_json(url, headers=request_headers)
    if not data:
        return "0", []
    response = data.get("response", {})
    return response.get("allotmentAmount", "0"), response.get("plans", [])

def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rows, processed_gp_count = [], 0

    zps = get_zps()
    print(f"Found {len(zps)} districts")

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
                    print(f"      {fin_year}")
                    allotment_amount, plans = get_action_plans(gp_id, fin_year)
                    print(f"        Plans found: {len(plans)}")

                    for plan in plans:
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
                            "allotment_amount":     allotment_amount,
                            "plan_code":            plan.get("code"),
                            "plan_type":            plan.get("type"),
                            "plan_year":            plan.get("year"),
                            "plan_status":          plan.get("status"),
                            "registered_on":        plan.get("registeredon"),
                            "planned_outlay":       plan.get("plannedOutlay"),
                            "actual_fund_received": plan.get("actulFundRcvd"),
                            "actual_expenditure":   plan.get("actulExpndtur"),
                            "number_of_activities": plan.get("noofActivities"),
                            "actual_activities":    plan.get("actualActivities"),
                            "citizen_approval_pdf": plan.get("citizenApprovalPDF"),
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