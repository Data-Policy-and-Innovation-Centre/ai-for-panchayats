#!/usr/bin/env python3
"""
Scrape month-wise vouchers from eGramSwaraj for a list of GPs across several
financial years. Output: ONE JSON per GP, all years inside. No CSV / XLSX.

Output location:
  Files are written into <project>/data/raw/vouchers/ (via the project's
  `directories` config), not the current working directory.

Resume behaviour (per-year):
  * If a GP's JSON already exists, years already marked "ok" or "no_data" are
    kept as-is and NOT refetched.
  * Only missing years (e.g. a newly added 2020-2021) and years previously
    marked "fetch_failed" are fetched.
  * A failed request is NEVER saved as "0 receipts / 0 payments"; it stays
    {"status": "fetch_failed"} so real emptiness and real failure never mix.

Run:  python scripts/Accounting_20_GPs.py   (or: uv run python scripts/Accounting_20_GPs.py)
Deps: requests beautifulsoup4 lxml
"""

import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# Make the project root importable so `config` is found no matter where this
# script is launched from. This file lives at <root>/scripts/, so parent.parent
# is the project root where config.py sits.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import directories

# ============================== CONFIG ==============================
# Paste the JSESSIONID from your browser (DevTools > Application > Cookies,
# or from the -b "...JSESSIONID=XXXX" in your curl). Refresh it if you start
# getting 500s again — sessions expire after a while.
COOKIE = {
    "JSESSIONID": "907E07AF4336A77CEA5B62E6E605C2C2",
}

FIN_YEARS = ["2025-2026", "2024-2025", "2023-2024", "2022-2023", "2021-2022", "2020-2021"]  # newest first
STATE = "21"   # Odisha

REQUEST_DELAY = 1.0     # seconds between requests (be polite)
MAX_RETRIES = 3         # per request, on 500 / timeout
RETRY_BACKOFF = 4.0     # seconds, grows each retry (4, 8, 12)

# name, district, block, village(GP) — all codes validated against the LGD file
TARGETS = [
    ("Andhrua",        "321", "3823", "119598"),
    ("Barimunda",      "321", "3823", "119599"),
    ("Itipur",         "321", "3823", "119605"),
    ("Dadhapatna",     "309", "3699", "116936"),
    ("Govindapur",     "309", "3707", "117153"),
    ("Hirlipali",      "306", "3674", "116350"),
    ("Bandhpali",      "306", "3676", "116397"),
    ("Bhatigaon",      "306", "3676", "116400"),
    ("Bheden",         "306", "3678", "116438"),
    ("Sharagada",      "313", "3747", "118012"),
    ("Mendarajpur",    "313", "3745", "275075"),
    ("Chikilli",       "313", "3740", "117835"),
    ("Biswamathpur",   "313", "3745", "117951"),
    ("Laxmipur",       "322", "3838", "119862"),
    ("Boipariguda",    "322", "3830", "119717"),
    ("Dutimendi",      "318", "3790", "118939"),
    ("Kalyansinghpur", "329", "3914", "121162"),
    ("Kalimela",       "323", "3843", "119936"),
    ("Haldikudar",     "332", "3945", "121663"),
    ("Karuabahal",     "332", "3936", "121526"),
]

# All GP JSONs go into data/raw/vouchers/
OUTPUT_DIR = directories.RAW_DATA / "vouchers"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
# ===================================================================

BASE = "https://egramswaraj.gov.in"
FILEREDIRECT = f"{BASE}/FileRedirect.jsp"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MONTHS = {1:"January",2:"February",3:"March",4:"April",5:"May",6:"June",
          7:"July",8:"August",9:"September",10:"October",11:"November",12:"December"}

COLUMNS = ["receipt_date","receipt_voucher_no","receipt_type","receipt_amount",
           "payment_date","payment_voucher_no","payment_type","payment_amount","case_record",
           "contra_date","contra_voucher_no","contra_amount",
           "journal_date","journal_voucher_no","journal_amount"]


def clean(t):
    return " ".join(str(t).split()).strip()

def to_num(s):
    s = str(s).replace(",", "").strip()
    try:
        return float(s) if s else None
    except ValueError:
        return None


def get(session, url, referer=None):
    """GET with retries/backoff. Returns response text, or raises on final fail."""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, headers=headers, timeout=60)
            if r.status_code == 200:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"failed after {MAX_RETRIES} tries: {last}")


# ---- month-wise static page: which months have activity + their voucher URLs ----
def month_wise_url(state, fin_year, village):
    return f"{FILEREDIRECT}?FD=ExpFY{fin_year}/{state}&name={village}.html"

def active_months(html):
    """Return {month_int: voucher_report_url} for months that have activity."""
    soup = BeautifulSoup(html, "lxml")
    out = {}
    for a in soup.find_all("a", href=True):
        if "voucherWiseReport.do" in a["href"]:
            m = re.search(r"month=(\d+)", a["href"])
            if m:
                href = a["href"]
                url = href if href.startswith("http") else f"{BASE}/{href.lstrip('/')}"
                out[int(m.group(1))] = url
    return out


def parse_voucher_page(html, month):
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", id="voucherTable")
    if not table:
        return []
    body = table.find("tbody")
    if not body:
        return []
    rows = []
    for tr in body.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 15:
            continue
        vals = [clean(td.get_text()) for td in tds[:15]]
        if not any(vals):
            continue
        row = dict(zip(COLUMNS, vals))
        for idx, key in ((1, "receipt"), (5, "payment")):
            a = tds[idx].find("a")
            if a and a.get("href") and "voucherID=" in a["href"]:
                row[f"{key}_voucher_id"] = a["href"].split("voucherID=")[1].split("&")[0]
        row["month"] = MONTHS[month]
        rows.append(row)
    return rows


def split_clean(raw_rows):
    receipts, payments = [], []
    for r in raw_rows:
        if r.get("receipt_voucher_no", "").strip():
            receipts.append({"month": r.get("month",""), "date": r.get("receipt_date",""),
                             "voucher_no": r.get("receipt_voucher_no",""), "type": r.get("receipt_type",""),
                             "amount": to_num(r.get("receipt_amount","")), "voucher_id": r.get("receipt_voucher_id","")})
        if r.get("payment_voucher_no", "").strip():
            payments.append({"month": r.get("month",""), "date": r.get("payment_date",""),
                             "voucher_no": r.get("payment_voucher_no",""), "type": r.get("payment_type",""),
                             "amount": to_num(r.get("payment_amount","")), "voucher_id": r.get("payment_voucher_id","")})

    def key(v):
        try:
            dd, mm, yy = v["date"].split("/"); return (int(yy), int(mm), int(dd))
        except Exception:
            return (9999, 99, 99)
    receipts.sort(key=key); payments.sort(key=key)
    return {
        "status": "ok",
        "receipt_count": len(receipts),
        "payment_count": len(payments),
        "total_receipts": round(sum(v["amount"] for v in receipts if v["amount"]), 2),
        "total_payments": round(sum(v["amount"] for v in payments if v["amount"]), 2),
        "receipts": receipts,
        "payments": payments,
    }


def scrape_year(session, ids, fin_year):
    """Return a year block dict. status is 'ok', 'no_data', or 'fetch_failed'."""
    mw_url = month_wise_url(STATE, fin_year, ids["village"])
    try:
        mw_html = get(session, mw_url)
    except Exception as e:
        return {"status": "fetch_failed", "note": f"month-wise page: {e}"}

    months = active_months(mw_html)
    if not months:
        return {"status": "no_data", "receipt_count": 0, "payment_count": 0,
                "total_receipts": 0, "total_payments": 0, "receipts": [], "payments": []}

    raw, failed = [], []
    for m in sorted(months):
        try:
            raw += parse_voucher_page(get(session, months[m], referer=mw_url), m)
        except Exception as e:
            failed.append(f"{MONTHS[m]}: {e}")
        time.sleep(REQUEST_DELAY)

    if failed:
        return {"status": "fetch_failed", "note": "; ".join(failed),
                "active_months": sorted(months)}
    return split_clean(raw)


def year_complete(block):
    return isinstance(block, dict) and block.get("status") in ("ok", "no_data")


def load_existing(out):
    """Return the previously saved record dict, or None if absent/unreadable."""
    if out.exists():
        try:
            return json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def main():
    if COOKIE.get("JSESSIONID", "").startswith("PASTE"):
        print("!! Set COOKIE['JSESSIONID'] first (see top of file).", file=sys.stderr)
        return

    session = requests.Session()
    session.cookies.update(COOKIE)

    for name, district, block, village in TARGETS:
        out = OUTPUT_DIR / f"{name.lower()}_vouchers.json"
        ids = {"district": district, "block": block, "village": village}

        # Start from the existing file (if any) so already-done years are kept.
        prev = load_existing(out)
        prev_years = prev.get("years", {}) if isinstance(prev, dict) else {}

        # If every requested year is already complete, nothing to do.
        if prev is not None and all(year_complete(prev_years.get(fy)) for fy in FIN_YEARS):
            print(f"=== {name}: all years complete, skipping ===")
            continue

        record = {"gp_name": name, "gp_lgd_code": village, "state": STATE,
                  "district": district, "block": block, "years": dict(prev_years)}
        print(f"=== {name} (village={village}) ===")

        for fy in FIN_YEARS:
            # per-year skip: keep years already fetched successfully
            if year_complete(prev_years.get(fy)):
                blk = prev_years[fy]
                if blk["status"] == "ok":
                    print(f"  {fy}: already have it "
                          f"(receipts={blk.get('receipt_count',0)} payments={blk.get('payment_count',0)}), skipping")
                else:
                    print(f"  {fy}: already have it (no activity), skipping")
                continue

            # missing or previously failed -> fetch now
            yb = scrape_year(session, ids, fy)
            record["years"][fy] = yb
            if yb["status"] == "ok":
                print(f"  {fy}: receipts={yb['receipt_count']} payments={yb['payment_count']}")
            elif yb["status"] == "no_data":
                print(f"  {fy}: no activity")
            else:
                print(f"  {fy}: FETCH FAILED -> {yb.get('note','')[:120]}", file=sys.stderr)

        out.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        bad = [fy for fy in FIN_YEARS if record["years"].get(fy, {}).get("status") == "fetch_failed"]
        print(f"  saved -> {out}" + (f"  (retry later: {bad})" if bad else "") + "\n")

    print("Done. Re-run any time to retry only the GPs/years that failed.")


if __name__ == "__main__":
    main()