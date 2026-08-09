#!/usr/bin/env python3
"""
harvest_plancodes.py  (targets mode: the 20 GPs, all years incl. 2020-2021)
====================================================================
Drill State -> ZP -> Block -> GP and harvest each target GP's
planCode(s), for every year with a seed below.

Key robustness:
- Only the JSESSIONID is taken from each seed cURL; the ZP-list request
  is rebuilt canonically per year. So a seed captured at the wrong
  drill level (e.g. the 2020-2021 one) still works.
- RESUMABLE: each (year, block) is checkpointed under
  <out>/plancodes_per_block/. Re-running skips done blocks.
- MERGE-SAFE: the final plancodes.csv = existing plancodes.csv +
  new checkpoints (deduped). Years you already have are preserved even
  if some seeds' cookies have expired this run.

Output columns:
    planYear, stateName, zpName, zpCode, blockName, blockCode,
    gpName, gpCode, planType, approvalDate, planCode
====================================================================
"""

import re
import sys
import glob
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import requests
import pandas as pd
from bs4 import BeautifulSoup

# ---- output location: project config if available, else cwd ----
try:
    REPO_ROOT = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(REPO_ROOT))
    from config import directories
    OUT_DIR = directories.RAW_DATA
except Exception:  # noqa
    OUT_DIR = Path(".")

URL = "https://egramswaraj.gov.in/actexpenditurereport.do"

MODE = "targets"          # "targets" = the 20 GPs; "all" = whole state
STATECODE = 21
STATES_WITH_EXTRA_LEVEL = {13, 15, 30, 12, 31, 14, 11, 38, 34, 17}

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://egramswaraj.gov.in",
    "Referer": URL,
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/150.0.0.0 Safari/537.36"),
}

# --------------------------------------------------------------------
# One seed cURL per year. Only the JSESSIONID is used; the drill level
# of the pasted body does not matter. Refresh a year's cookie if that
# year fails (session expired).
# --------------------------------------------------------------------
SEED_CURL_BY_YEAR = {
    "2025-2026": r'''curl "https://egramswaraj.gov.in/actexpenditurereport.do" -b "JSESSIONID=A117A9193B9B8342170B819F5C00C923" --data-raw "fnyear=2025&status_Level=2&statecode=21&planYear=2025-2026&stNm=Odisha&lvlFlg=12"''',
    "2024-2025": r'''curl "https://egramswaraj.gov.in/actexpenditurereport.do" -b "JSESSIONID=A117A9193B9B8342170B819F5C00C923" --data-raw "fnyear=2024&status_Level=2&statecode=21&planYear=2024-2025&stNm=Odisha&lvlFlg=12"''',
    "2023-2024": r'''curl "https://egramswaraj.gov.in/actexpenditurereport.do" -b "JSESSIONID=A117A9193B9B8342170B819F5C00C923" --data-raw "fnyear=2023&status_Level=2&statecode=21&planYear=2023-2024&stNm=Odisha&lvlFlg=12"''',
    "2022-2023": r'''curl "https://egramswaraj.gov.in/actexpenditurereport.do" -b "JSESSIONID=A117A9193B9B8342170B819F5C00C923" --data-raw "fnyear=2022&status_Level=2&statecode=21&planYear=2022-2023&stNm=Odisha&lvlFlg=12"''',
    "2021-2022": r'''curl "https://egramswaraj.gov.in/actexpenditurereport.do" -b "JSESSIONID=A117A9193B9B8342170B819F5C00C923" --data-raw "fnyear=2021&status_Level=2&statecode=21&planYear=2021-2022&stNm=Odisha&lvlFlg=12"''',
    # 2020-2021 -- fresh cookie captured recently:
    "2020-2021": r'''curl "https://egramswaraj.gov.in/actexpenditurereport.do" -b "JSESSIONID=698EF97E80E47D21253C00B5BD6E0118" --data-raw "fnyear=2020&status_Level=4&statecode=21&planYear=2020-2021&lbCode=3823&stNm=Odisha&lvlFlg=12&zpNm=Khordha&plnunt=321&bpNm=Bhubaneswar"''',
}

OUT_COLS = ["planYear", "stateName", "zpName", "zpCode", "blockName",
            "blockCode", "gpName", "gpCode", "planType", "approvalDate",
            "planCode"]

TARGET_BLOCKS = [
    ("Khordha",    321, "Bhubaneswar",     3823, [("Andhrua",119598),("Barimunda",119599),("Itipur",119605)]),
    ("Cuttack",    309, "Baranga",         3699, [("Dadhapatna",116936)]),
    ("Cuttack",    309, "Tangi Choudwar",  3707, [("Govindapur",117153)]),
    ("Bargarh",    306, "Attabira",        3674, [("Hirlipali",116350)]),
    ("Bargarh",    306, "Barpali",         3676, [("Bandhpali",116397),("Bhatigaon",116400)]),
    ("Bargarh",    306, "Bheden",          3678, [("Bheden",116438)]),
    ("Ganjam",     313, "Sheragada",       3747, [("Sharagada",118012)]),
    ("Ganjam",     313, "Rangeilunda",     3745, [("Mendarajpur",275075),("Biswamathpur",117951)]),
    ("Ganjam",     313, "Khallikote",      3740, [("Chikilli",117835)]),
    ("Koraput",    322, "Laxmipur",        3838, [("Laxmipur",119862)]),
    ("Koraput",    322, "Boipariguda",     3830, [("Boipariguda",119717)]),
    ("Kandhamal",  318, "Khajuripada",     3790, [("Dutimendi",118939)]),
    ("Rayagada",   329, "Kalyansingpur",   3914, [("Kalyansinghpur",121162)]),
    ("Malkangiri", 323, "Kalimela",        3843, [("Kalimela",119936)]),
    ("Sundargarh", 332, "Lahunipara",      3945, [("Haldikudar",121663)]),
    ("Sundargarh", 332, "Balisankara",     3936, [("Karuabahal",121526)]),
]


# ============================ cURL parse ============================
def parse_curl_jsession(curl: str):
    """Extract JSESSIONID from a copied cURL (bash or Windows cmd)."""
    m = re.search(r"JSESSIONID=([A-Fa-f0-9]+)", curl)
    return m.group(1) if m else None


def canonical_seed_body(planyear: str) -> dict:
    """Canonical Odisha ZP-list request for a year (level-independent)."""
    return {
        "fnyear": planyear.split("-")[0],
        "status_Level": "2", "statecode": str(STATECODE),
        "planYear": planyear, "lbCode": "", "planCode": "",
        "stNm": "Odisha", "lvlFlg": "12", "zpNm": "", "gpNm": "",
        "plnunt": "", "bpNm": "", "localBodyCode": "",
    }


# ============================ helpers ===============================
def new_session(jsessionid):
    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.set("JSESSIONID", jsessionid)
    return s


def post(session, body):
    r = session.post(URL, data=body, timeout=60)
    r.raise_for_status()
    return r.text


def form_state(html):
    soup = BeautifulSoup(html, "lxml")
    f = soup.find("form", id="actWiseExpndId")
    if f is None:
        raise RuntimeError("form #actWiseExpndId not found (session/captcha?).")
    return {i["name"]: i.get("value", "")
            for i in f.find_all("input") if i.get("name")}


def parse_calls(html, fn):
    pat = re.compile(re.escape(fn) + r"\s*\(([^)]*)\)")
    return [[a.strip().strip("'").strip() for a in argstr.split(",")]
            for argstr in pat.findall(html)]


def apply_getasset(fs, statecode, planyear, lvl, lvlFlg, stateNm):
    fs = dict(fs)
    fs.update({"status_Level": str(lvl), "statecode": str(statecode),
               "planYear": planyear, "lvlFlg": str(lvlFlg), "stNm": stateNm})
    return fs


def apply_getassetnext(fs, zpCd, statecode, planyear, flg, stateNm, zpNm):
    fs = dict(fs)
    lvl = 4 if (int(statecode) in STATES_WITH_EXTRA_LEVEL
                or int(flg) in (10, 11)) else 3
    fs.update({"lbCode": str(zpCd), "statecode": str(statecode),
               "planYear": planyear, "lvlFlg": str(flg), "stNm": stateNm,
               "zpNm": zpNm, "status_Level": str(lvl)})
    return fs


def apply_getassetnexttobp(fs, blockCd, statecode, planyear, lvl, flg,
                           stateNm, bpNm):
    fs = dict(fs)
    fs.update({"status_Level": str(lvl), "lbCode": str(blockCd),
               "statecode": str(statecode), "planYear": planyear,
               "lvlFlg": str(flg), "stNm": stateNm, "bpNm": bpNm})
    return fs


def find_call_by_code(html, fn, code, code_index):
    for args in parse_calls(html, fn):
        if len(args) > code_index and args[code_index].strip() == str(code):
            return args
    return None


# ============================ discovery =============================
def to_zp_list(session, seed_html):
    st = find_call_by_code(seed_html, "getasset", STATECODE, 0)
    if st:
        fs = apply_getasset(form_state(seed_html), st[0], st[1], st[2],
                            st[3], st[4])
        return post(session, fs)
    return seed_html


def list_zps(zp_list_html):
    return [(a[0], a[6], a) for a in parse_calls(zp_list_html, "getassetnext")
            if len(a) >= 7]


def list_blocks(block_list_html):
    return [(a[0], a[6], a) for a in
            parse_calls(block_list_html, "getassetnexttobp") if len(a) >= 7]


def harvest_plans(gplist_html):
    soup = BeautifulSoup(gplist_html, "lxml")
    plans = []
    for tr in soup.find_all("tr"):
        if "getassetfinal(" not in str(tr):
            continue
        calls = parse_calls(str(tr), "getassetfinal")
        if not calls or len(calls[0]) < 6:
            continue
        a = calls[0]
        cells = [" ".join(td.get_text(" ", strip=True).split())
                 for td in tr.find_all("td")]
        row_text = " ".join(cells)
        mtype = re.search(r"\b(Main|Supplementary)\b", row_text, re.I)
        mdate = re.search(r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", row_text)
        plans.append({
            "gpCode": str(a[0]), "gpName": a[5], "planCode": str(a[1]),
            "planType": mtype.group(1) if mtype else "",
            "approvalDate": mdate.group(1) if mdate else "",
        })
    return plans


# ============================ crawl ================================
def build_target_filter():
    want_zps, want_blocks, want_gps = set(), set(), {}
    for zpName, zpCode, blockName, blockCode, gps in TARGET_BLOCKS:
        want_zps.add(str(zpCode))
        want_blocks.add((str(zpCode), str(blockCode)))
        want_gps[(str(zpCode), str(blockCode))] = {str(c) for _, c in gps}
    return want_zps, want_blocks, want_gps


def crawl_year(planyear, seed_curl, per_block_dir):
    jsess = parse_curl_jsession(seed_curl)
    if not jsess:
        print(f"  ! {planyear}: no JSESSIONID in seed. Skipping.")
        return

    session = new_session(jsess)
    try:
        seed_html = post(session, urlencode(canonical_seed_body(planyear)))
        zp_list_html = to_zp_list(session, seed_html)
    except Exception as e:  # noqa
        print(f"  ! {planyear}: seed request failed: {e}")
        return

    zps = list_zps(zp_list_html)
    if not zps:
        print(f"  ! {planyear}: no ZP list returned "
              f"(session expired or year has no data). Skipping.")
        return

    if MODE == "targets":
        want_zps, want_blocks, want_gps = build_target_filter()
    else:
        want_zps = want_blocks = want_gps = None

    for zpCode, zpName, zp_args in zps:
        if want_zps is not None and zpCode not in want_zps:
            continue
        try:
            fs = apply_getassetnext(form_state(zp_list_html), zp_args[0],
                                    zp_args[1], zp_args[2], zp_args[4],
                                    zp_args[5], zp_args[6])
            block_list_html = post(session, fs)
        except Exception as e:  # noqa
            print(f"    ! {planyear} ZP {zpName}({zpCode}): {e}")
            continue

        for blockCode, blockName, bp_args in list_blocks(block_list_html):
            if want_blocks is not None and (zpCode, blockCode) not in want_blocks:
                continue
            cp = per_block_dir / f"{planyear}_{zpCode}_{blockCode}.csv"
            if cp.exists():
                print(f"    [{planyear}] {zpName} > {blockName}: cached - skip")
                continue
            try:
                fs = apply_getassetnexttobp(form_state(block_list_html),
                                            bp_args[0], bp_args[1], bp_args[2],
                                            bp_args[3], bp_args[4], bp_args[5],
                                            bp_args[6])
                gp_list_html = post(session, fs)
            except Exception as e:  # noqa
                print(f"    ! {planyear} block {blockName}({blockCode}): {e}")
                continue

            gp_filter = (None if want_gps is None
                         else want_gps.get((zpCode, blockCode), set()))
            rows = []
            for p in harvest_plans(gp_list_html):
                if gp_filter is not None and p["gpCode"] not in gp_filter:
                    continue
                rows.append({
                    "planYear": planyear, "stateName": "Odisha",
                    "zpName": zpName, "zpCode": zpCode,
                    "blockName": blockName, "blockCode": blockCode,
                    "gpName": p["gpName"], "gpCode": p["gpCode"],
                    "planType": p["planType"], "approvalDate": p["approvalDate"],
                    "planCode": p["planCode"],
                })
            pd.DataFrame(rows, columns=OUT_COLS).to_csv(
                cp, index=False, encoding="utf-8-sig")
            n_gp = len({r["gpCode"] for r in rows})
            print(f"    [{planyear}] {zpName} > {blockName}: "
                  f"{len(rows)} plan-rows / {n_gp} GPs")
            time.sleep(1)


def rebuild(per_block_dir):
    frames = []
    for fp in sorted(glob.glob(str(per_block_dir / "*.csv"))):
        try:
            frames.append(pd.read_csv(fp, dtype=str).fillna(""))
        except Exception:  # noqa
            pass
    return pd.concat(frames, ignore_index=True) if frames else None


def main():
    per_block_dir = OUT_DIR / "plancodes_per_block"
    per_block_dir.mkdir(parents=True, exist_ok=True)
    out_csv = OUT_DIR / "plancodes.csv"

    print(f"MODE={MODE}  output={OUT_DIR}\n")

    for planyear, seed in SEED_CURL_BY_YEAR.items():
        if not seed:
            print(f"[{planyear}] no seed, skipping.")
            continue
        print(f"===== {planyear} =====")
        try:
            crawl_year(planyear, seed, per_block_dir)
        except Exception as e:  # noqa
            print(f"  ! {planyear} aborted: {e} (progress saved; re-run)")

    # MERGE: existing plancodes.csv + all checkpoints, deduped
    frames = []
    if out_csv.exists():
        try:
            frames.append(pd.read_csv(out_csv, dtype=str).fillna(""))
        except Exception:  # noqa
            pass
    ck = rebuild(per_block_dir)
    if ck is not None:
        frames.append(ck)

    if not frames:
        sys.exit("\nNothing harvested and no existing plancodes.csv. "
                 "Refresh a seed cookie and re-run.")

    df = pd.concat(frames, ignore_index=True)
    for c in OUT_COLS:
        if c not in df.columns:
            df[c] = ""
    df = df[OUT_COLS]
    df = df.drop_duplicates(subset=["planYear", "gpCode", "planCode"],
                            keep="last").reset_index(drop=True)
    df.to_csv(out_csv, index=False, encoding="utf-8-sig")

    print(f"\nSaved {out_csv}: {len(df)} plan-rows, "
          f"{df.groupby(['gpName','planYear']).ngroups} GP-years, "
          f"years: {', '.join(sorted(df['planYear'].unique()))}")


if __name__ == "__main__":
    main()