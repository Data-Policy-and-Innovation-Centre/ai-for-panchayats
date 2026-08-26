# scripts/panchayat_payment_register.py
# Meri_Panchayat_payment_register_data_extraction
# Author: Ravishankar Singh
# Date: 15-06-2026

import os
import time
import pandas as pd


from .config import (
    in_scope,
    STATE_ID, STATE_NAME,
    FIN_YEARS,
    OUTPUT_DIR, SAVE_EVERY_GP,
    REQUEST_DELAY, BASE_URL,
    build_headers, get_output_path,
)
from .base_scraper import (FetchError, IncompleteRun, fetch_json_post,
                           response_field,
                           get_blocks, get_gps, get_zps, save_outputs)

OUTPUT_FILE_JSON = get_output_path(os.path.basename(__file__))
EPO_HEADERS = build_headers("funds", lang="en-IN")
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
        # A failure part-way through raises, so partial pages are never
        # returned as if the register were complete. The caller decides
        # whether to retry or record the gap.
        data = fetch_json_post(url, EPO_HEADERS, payload)

        # Defaulting a missing `response` to {} turned a 200 error envelope
        # into epos=[] count=0, which the loop below accepts as a legitimate
        # empty register: no FetchError, so the retry and failure-manifest
        # paths are skipped, an existing register is overwritten with nothing,
        # and the stale-manifest cleanup then reports the run as clean.
        epos = response_field(data, "epos", url, f"GP {gp_id} {fin_year}")
        reported = data["response"].get("count")
        if not isinstance(reported, int) or isinstance(reported, bool):
            # Defaulting a missing count to 0 satisfies the stop condition
            # below after the very first page, silently capping the register
            # at one page however many payments exist. The count is what
            # makes pagination verifiable, so its absence is schema drift.
            raise FetchError(
                url,
                f"malformed payment envelope for GP {gp_id} {fin_year}: "
                f"`response.count` is {type(reported).__name__}, not an integer")

        if not epos:
            # An empty page before the reported total is reached is pagination
            # failing, not the register ending. Returning here would hand back
            # a short register as a complete one: no FetchError, so the retry
            # and failure-manifest paths never run and the stage exits green
            # with payments missing. Only a page that completes the count, or
            # a zero count, is a legitimate stop.
            if reported and len(all_epos) < reported:
                raise FetchError(
                    url,
                    f"pagination stopped at {len(all_epos)} of {reported} "
                    f"reported payment(s) for GP {gp_id} {fin_year}")
            break

        all_epos.extend(epos)
        skip += PAGE_LIMIT

        if len(all_epos) >= reported:
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

                if not in_scope(gp_id):
                    continue
                print(f"      GP {processed_gp_count + 1}: {gp_name}")

                for fin_year in FIN_YEARS:
                    # Retry once, then record the gap and carry on. Aborting
                    # the whole run on one transient failure would waste the
                    # GP-years already collected; the recorded gaps are what
                    # make the run's completeness auditable, and main()
                    # returns non-zero if any remain.
                    try:
                        epos = get_epayment_orders(gp_id, fin_year)
                    except FetchError as first:
                        print(f"        -> RETRYING once for Year {fin_year}: {first.reason}")
                        time.sleep(2)
                        try:
                            epos = get_epayment_orders(gp_id, fin_year)
                        except FetchError as second:
                            print(f"        [FAILED] {fin_year}: {second.reason}")
                            failed_gp_years.append({
                                "zp_id": zp_id, "zp_name": zp_name,
                                "bp_id": bp_id, "bp_name": bp_name,
                                "gp_id": gp_id, "gp_name": gp_name,
                                "financial_year": fin_year,
                                "reason": second.reason,
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
                    save_outputs(pd.DataFrame(rows), OUTPUT_FILE_JSON, checkpoint=True)
                    print(f"Checkpoint saved after {processed_gp_count} GPs")

    df = pd.DataFrame(rows)
    save_outputs(df, OUTPUT_FILE_JSON)

    print(f"\n{'='*60}\nProcessed {processed_gp_count} GPs | Saved {len(df)} rows\n{'='*60}")

    failed_path = os.path.join(
        OUTPUT_DIR, "panchayat_payment_register_FAILED_gp_years.json")

    if not failed_gp_years:
        # A clean rerun must not leave the previous run's manifest sitting
        # beside complete output: anyone auditing the latest extraction from
        # these two files would read failures that no longer apply.
        if os.path.exists(failed_path):
            os.remove(failed_path)
            print(f"Removed stale failure manifest from a previous run: {failed_path}")

    if failed_gp_years:
        # Write the manifest first: the collected rows are worth keeping, and
        # the gaps have to be inspectable. Then fail, so an incomplete
        # register is never reported as a successful extraction.
        pd.DataFrame(failed_gp_years).to_json(
            failed_path, orient="records", indent=2, force_ascii=False)
        print(f"\n*** {len(failed_gp_years)} GP-year(s) incomplete. "
              f"Manifest: {failed_path} ***")
        raise IncompleteRun("panchayat_payment_register", failed_gp_years)


if __name__ == "__main__":
    main()