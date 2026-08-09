#!/usr/bin/env python3
"""
Activity_wise_expenditure_20GPs.py
====================================================================
Pull the full "Activity wise Expenditure Report" for every GP x plan
x year into one combined dataset.

Plan list is gathered from BOTH:
    <RAW_DATA>/plancodes.csv                    (merged file)
    <RAW_DATA>/plancodes_per_block/*.csv        (harvester checkpoints)
    <SCRIPTS>/plancodes_per_block/*.csv         (in case they landed here)
...unioned and deduped, so newly harvested years (e.g. 2020-2021)
are included automatically.

- Paths from config.py (no hardcoded output path).
- build_body uses zpCode/blockCode from the CSV when present, else the
  name-based BLOCK_CODES lookup.
- Handles multiple plans per GP-year (Main + Supplementary).
- RESUMABLE: each plan is checkpointed as per_plan/<key>.csv. Re-running
  skips done plans; failures are retried next run.

Usage:
    uv run python scripts/Activity_wise_expenditure_20GPs.py
    uv run python scripts/Activity_wise_expenditure_20GPs.py /abs/plancodes.csv
====================================================================
"""

import re
import sys
import glob
import time
from pathlib import Path

import requests
import pandas as pd
from bs4 import BeautifulSoup

# --- make repo root importable so `config` resolves when run as a script ---
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from config import directories  # noqa: E402

URL = "https://egramswaraj.gov.in/actexpenditurereport.do"

# refresh when the session expires (DevTools -> Network -> Cookie)
JSESSIONID = "1DBCD6929BCE50B575BC029636CD9DCF"

DEFAULT_INPUT = directories.RAW_DATA / "plancodes.csv"
DEFAULT_OUTDIR = directories.RAW_DATA / "Activity_expenditure"

# folders of harvester checkpoints to also fold in (union with the CSV)
PER_BLOCK_DIRS = [
    directories.RAW_DATA / "plancodes_per_block",
    directories.SCRIPTS / "plancodes_per_block",
]

PLANCODES_CSV = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
OUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTDIR

STATECODE = "21"
LVL_FLG = "12"
STATUS_LEVEL = "99"

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://egramswaraj.gov.in",
    "Referer": URL,
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/150.0.0.0 Safari/537.36"),
}

# (zpName, blockName) -> (plnunt, lbCode)  -- fallback if CSV lacks codes
BLOCK_CODES = {
    ("Khordha", "Bhubaneswar"):     ("321", "3823"),
    ("Cuttack", "Baranga"):         ("309", "3699"),
    ("Cuttack", "Tangi Choudwar"):  ("309", "3707"),
    ("Bargarh", "Attabira"):        ("306", "3674"),
    ("Bargarh", "Barpali"):         ("306", "3676"),
    ("Bargarh", "Bheden"):          ("306", "3678"),
    ("Ganjam", "Sheragada"):        ("313", "3747"),
    ("Ganjam", "Rangeilunda"):      ("313", "3745"),
    ("Ganjam", "Khallikote"):       ("313", "3740"),
    ("Koraput", "Laxmipur"):        ("322", "3838"),
    ("Koraput", "Boipariguda"):     ("322", "3830"),
    ("Kandhamal", "Khajuripada"):   ("318", "3790"),
    ("Rayagada", "Kalyansingpur"):  ("329", "3914"),
    ("Malkangiri", "Kalimela"):     ("323", "3843"),
    ("Sundargarh", "Lahunipara"):   ("332", "3945"),
    ("Sundargarh", "Balisankara"):  ("332", "3936"),
}

META_COLS = ["planYear", "stateName", "zpName", "blockName",
             "gpName", "gpCode", "planType", "approvalDate", "planCode"]
DATA_COLS = [
    "S.No.", "Activity Code", "Activity Name", "Activity For", "Focus Area",
    "Approved Cost in Action Plan", "Technical Approved Cost",
    "Admin Approved Cost", "Scheme Name", "General", "SC", "ST",
    "Total Expenditure", "Voucher Date", "Voucher No", "Voucher Cost",
]
OUT_COLS = META_COLS + DATA_COLS


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.set("JSESSIONID", JSESSIONID)
    return s


def load_plan_list() -> pd.DataFrame:
    """Union plancodes.csv + every plancodes_per_block/*.csv, deduped."""
    frames = []
    if PLANCODES_CSV.exists():
        frames.append(pd.read_csv(PLANCODES_CSV, dtype=str).fillna(""))
        print(f"  + {PLANCODES_CSV} ({len(frames[-1])} rows)")
    for d in PER_BLOCK_DIRS:
        files = sorted(glob.glob(str(d / "*.csv")))
        if files:
            part = [pd.read_csv(f, dtype=str).fillna("") for f in files]
            part = pd.concat(part, ignore_index=True)
            frames.append(part)
            print(f"  + {d} ({len(files)} files, {len(part)} rows)")
    if not frames:
        sys.exit("No plan list found (plancodes.csv or plancodes_per_block/).")

    plan = pd.concat(frames, ignore_index=True)
    for c in ("planType", "approvalDate", "zpCode", "blockCode"):
        if c not in plan.columns:
            plan[c] = ""
    plan = plan.fillna("")
    # a plan is unique by year + GP + planCode
    plan = plan.drop_duplicates(
        subset=["planYear", "gpCode", "planCode"], keep="first"
    ).reset_index(drop=True)
    return plan


def build_body(row) -> dict:
    fnyear = str(row["planYear"]).split("-")[0]
    # prefer explicit codes from the CSV; fall back to name lookup
    plnunt = str(row.get("zpCode", "") or "").strip()
    lbCode = str(row.get("blockCode", "") or "").strip()
    if not (plnunt and lbCode):
        key = (str(row["zpName"]).strip(), str(row["blockName"]).strip())
        plnunt, lbCode = BLOCK_CODES.get(key, (plnunt, lbCode))
    return {
        "fnyear": fnyear,
        "status_Level": STATUS_LEVEL,
        "statecode": STATECODE,
        "planYear": str(row["planYear"]),
        "lbCode": lbCode,
        "planCode": str(row["planCode"]),
        "stNm": str(row["stateName"]),
        "lvlFlg": LVL_FLG,
        "zpNm": str(row["zpName"]),
        "gpNm": str(row["gpName"]),
        "plnunt": plnunt,
        "bpNm": str(row["blockName"]),
        "localBodyCode": str(row["gpCode"]),
    }


def plan_key(row) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_",
                  f"{row['gpName']}_{row['planYear']}_"
                  f"{row.get('planType','') or 'Plan'}_{row['planCode']}")


def cell_text(td) -> str:
    return " ".join(td.get_text(" ", strip=True).split())


def cell_lines(td) -> list:
    return [t.strip() for t in td.get_text("\n", strip=True).split() if t.strip()]


def header_plan_year(soup):
    table = soup.find("table", class_="myTable")
    if table is None or table.find("thead") is None:
        return None
    m = re.search(r"Plan Year\s*:\s*([0-9]{4}-[0-9]{4})",
                  table.find("thead").get_text(" ", strip=True))
    return m.group(1) if m else None


def parse_rows(html, meta) -> list:
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="myTable")
    if table is None or table.find("tbody") is None:
        return []
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 16:
            continue
        rec = dict(meta)
        rec.update({
            "S.No.":                        cell_text(tds[0]),
            "Activity Code":                cell_text(tds[1]),
            "Activity Name":                cell_text(tds[2]),
            "Activity For":                 cell_text(tds[3]),
            "Focus Area":                   cell_text(tds[4]),
            "Approved Cost in Action Plan": cell_text(tds[5]),
            "Technical Approved Cost":      cell_text(tds[6]),
            "Admin Approved Cost":          cell_text(tds[7]),
            "Scheme Name":                  cell_text(tds[8]),
            "General":                      cell_text(tds[9]),
            "SC":                           cell_text(tds[10]),
            "ST":                           cell_text(tds[11]),
            "Total Expenditure":            cell_text(tds[12]),
            "Voucher Date":                 " | ".join(cell_lines(tds[13])),
            "Voucher No":                   " | ".join(cell_lines(tds[14])),
            "Voucher Cost":                 " | ".join(cell_lines(tds[15])),
        })
        rows.append(rec)
    return rows


def fetch(session, body, retries=2):
    for attempt in range(retries + 1):
        try:
            r = session.post(URL, data=body, timeout=60)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            if attempt == retries:
                raise
            time.sleep(2 * (attempt + 1))
    return None


def rebuild_combined(per_plan_dir: Path, out_dir: Path):
    frames = []
    for fp in sorted(glob.glob(str(per_plan_dir / "*.csv"))):
        try:
            frames.append(pd.read_csv(fp, dtype=str).fillna(""))
        except Exception:  # noqa
            pass
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    for c in OUT_COLS:
        if c not in df.columns:
            df[c] = ""
    df = df[OUT_COLS]
    df.to_csv(out_dir / "expenditure_all.csv", index=False,
              encoding="utf-8-sig")
    df.to_excel(out_dir / "expenditure_all.xlsx", index=False)
    return df


def main():
    print("Building plan list from:")
    plan = load_plan_list()

    raw_dir = OUT_DIR / "raw_html"
    per_plan_dir = OUT_DIR / "per_plan"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    per_plan_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nOutput: {OUT_DIR}")
    print(f"Total {len(plan)} plan-rows "
          f"({plan.groupby(['gpName','planYear']).ngroups} GP-years), "
          f"years: {', '.join(sorted(plan['planYear'].unique()))}\n")

    session = new_session()
    status_log = []

    def log(row, status, detail, n_act):
        status_log.append({
            "planYear": row["planYear"], "zpName": row["zpName"],
            "blockName": row["blockName"], "gpName": row["gpName"],
            "gpCode": row["gpCode"],
            "planType": row.get("planType", ""), "planCode": row["planCode"],
            "status": status, "detail": detail, "activities": n_act,
        })

    for idx, row in plan.iterrows():
        gp, yr = row["gpName"], row["planYear"]
        ptype = row.get("planType", "") or "Plan"
        tag = f"{gp} {yr} [{ptype}] pc={row['planCode']}"
        key = plan_key(row)
        checkpoint = per_plan_dir / f"{key}.csv"

        if checkpoint.exists():
            try:
                n = len(pd.read_csv(checkpoint, dtype=str))
            except Exception:  # noqa
                n = 0
            print(f"  [{idx+1}/{len(plan)}] {tag}: cached ({n}) - skip")
            log(row, "cached", "", n)
            continue

        body = build_body(row)
        if not (body["plnunt"] and body["lbCode"]):
            print(f"  [{idx+1}/{len(plan)}] {tag}: no ZP/block code, skipping.")
            log(row, "failed", "no zp/block code", 0)
            continue

        try:
            html = fetch(session, body)
        except Exception as e:  # noqa
            print(f"  [{idx+1}/{len(plan)}] {tag}: fetch error {e}")
            log(row, "failed", f"fetch error: {e}", 0)
            continue

        with open(raw_dir / f"{key}.html", "w", encoding="utf-8") as fh:
            fh.write(html)

        soup = BeautifulSoup(html, "lxml")
        got_year = header_plan_year(soup)
        if got_year is None:
            print(f"  [{idx+1}/{len(plan)}] {tag}: no table "
                  f"(session/captcha?) - skip (retry next run).")
            log(row, "failed", "no table (session/captcha?)", 0)
            continue
        if got_year != yr:
            print(f"  [{idx+1}/{len(plan)}] {tag}: returned {got_year} "
                  f"not {yr} - skip.")
            log(row, "failed", f"year mismatch: got {got_year}", 0)
            continue

        meta = {c: str(row[c]) for c in META_COLS}
        rows = parse_rows(html, meta)
        pd.DataFrame(rows, columns=OUT_COLS).to_csv(
            checkpoint, index=False, encoding="utf-8-sig")
        print(f"  [{idx+1}/{len(plan)}] {tag}: {len(rows)} activities")
        log(row, "scraped", "", len(rows))
        time.sleep(1)

    df = rebuild_combined(per_plan_dir, OUT_DIR)

    summary = pd.DataFrame(status_log, columns=[
        "planYear", "zpName", "blockName", "gpName", "gpCode",
        "planType", "planCode", "status", "detail", "activities"])
    summary.to_csv(OUT_DIR / "run_summary.csv", index=False,
                   encoding="utf-8-sig")

    n_scraped = int((summary["status"] == "scraped").sum())
    n_cached = int((summary["status"] == "cached").sum())
    n_failed = int((summary["status"] == "failed").sum())
    total_rows = 0 if df is None else len(df)

    print(f"\nDone. combined {total_rows} activity rows.")
    print(f"This run: {n_scraped} newly scraped, {n_cached} cached, "
          f"{n_failed} failed.")
    print(f"Saved to {OUT_DIR}")
    if n_failed:
        print(f"\nFailed plan-rows ({n_failed}) - re-run to retry:")
        for _, r in summary[summary["status"] == "failed"].iterrows():
            print(f"  - {r['gpName']} {r['planYear']} [{r['planType']}] "
                  f"pc={r['planCode']}: {r['detail']}")


if __name__ == "__main__":
    main()