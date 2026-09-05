"""eGramSwaraj Activity-wise Expenditure scraper.
Reads a plan.csv (one row per plan code per GP x year) and POSTs each plan to
``actexpenditurereport.do`` to pull its full activity-wise expenditure table.
"""

from __future__ import annotations

import glob
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
from bs4 import BeautifulSoup
from loguru import logger

from .config import (
    URL, UA, STATE_NAME, LVL_FLG, STATUS_LEVEL, OUT_COLS, ExpenditureConfig,
)


# --------------------------- pure helpers ---------------------------
def load_lgd_lookup(path) -> dict:
    d = json.loads(open(path, encoding="utf-8").read())
    state = str(d["state_code"])
    lut = {}
    for z in d["zillas"]:
        for b in z["blocks"]:
            for g in b["gps"]:
                lut[str(g["gp_lgd_code"])] = {
                    "statecode": state,
                    "zpNm": z["zp_name"], "plnunt": str(z["zp_lgd_code"]),
                    "bpNm": b["bp_name"], "lbCode": str(b["bp_lgd_code"]),
                    "gpNm": g["gp_name"],
                }
    return lut


def cell_text(td) -> str:
    return " ".join(td.get_text(" ", strip=True).split())


def cell_lines(td) -> list[str]:
    return [t.strip() for t in td.get_text(" ", strip=True).split() if t.strip()]


def header_plan_year(soup):
    table = soup.find("table", class_="myTable")
    if table is None or table.find("thead") is None:
        return None
    m = re.search(r"Plan Year\s*:\s*([0-9]{4}-[0-9]{4})",
                  table.find("thead").get_text(" ", strip=True))
    return m.group(1) if m else None


def parse_rows(soup, meta) -> list[dict]:
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
            "S.No.": cell_text(tds[0]), "Activity Code": cell_text(tds[1]),
            "Activity Name": cell_text(tds[2]), "Activity For": cell_text(tds[3]),
            "Focus Area": cell_text(tds[4]),
            "Approved Cost in Action Plan": cell_text(tds[5]),
            "Technical Approved Cost": cell_text(tds[6]),
            "Admin Approved Cost": cell_text(tds[7]),
            "Scheme Name": cell_text(tds[8]), "General": cell_text(tds[9]),
            "SC": cell_text(tds[10]), "ST": cell_text(tds[11]),
            "Total Expenditure": cell_text(tds[12]),
            "Voucher Date": " | ".join(cell_lines(tds[13])),
            "Voucher No": " | ".join(cell_lines(tds[14])),
            "Voucher Cost": " | ".join(cell_lines(tds[15])),
        })
        rows.append(rec)
    return rows


def build_body(row, loc) -> dict:
    return {
        "fnyear": str(row["fiscal_year"]).split("-")[0],
        "status_Level": STATUS_LEVEL,
        "statecode": loc["statecode"],
        "planYear": str(row["fiscal_year"]),
        "lbCode": loc["lbCode"],
        "planCode": str(row["plan_code"]),
        "stNm": STATE_NAME,
        "lvlFlg": LVL_FLG,
        "zpNm": loc["zpNm"],
        "gpNm": loc["gpNm"],
        "plnunt": loc["plnunt"],
        "bpNm": loc["bpNm"],
        "localBodyCode": str(row["gp_lgd_code"]),
    }


def safe_name(gp_name, yr, ptype, plan_code) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", f"{gp_name}_{yr}_{ptype}_{plan_code}")


def _headers(cookie: dict) -> dict:
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookie.items())
    return {
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,image/apng,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "max-age=0",
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded",
        "Cookie": cookie_str,
        "Origin": "https://egramswaraj.gov.in",
        "Referer": URL,
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": UA,
    }


# ------------------------------- scraper -------------------------------
class ExpenditureScraper:
    def __init__(self, config: ExpenditureConfig):
        self.cfg = config
        self._local = threading.local()
        self._lut: dict | None = None
        for d in (config.per_plan_dir, config.raw_html_dir):
            d.mkdir(parents=True, exist_ok=True)

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update(_headers(self.cfg.cookie))
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.cfg.workers, pool_maxsize=self.cfg.workers, max_retries=0)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            self._local.session = s
        return s

    def fetch(self, body: dict) -> str:
        session = self._session()
        for attempt in range(self.cfg.retries + 1):
            try:
                r = session.post(URL, data=body, timeout=60)
                r.raise_for_status()
                return r.text
            except requests.RequestException:
                if attempt == self.cfg.retries:
                    raise
                time.sleep(1.5 * (attempt + 1))
        return ""

    # returns (status, n_rows, message)
    def _worker(self, task) -> tuple[str, int, str]:
        row, loc = task
        gp_code = str(row["gp_lgd_code"]).strip()
        yr = str(row["fiscal_year"]).strip()
        ptype = str(row.get("plan_type", "")) or "Plan"
        gp_name = loc["gpNm"]
        safe = safe_name(gp_name, yr, ptype, row["plan_code"])
        out_path = self.cfg.per_plan_dir / f"{safe}.csv"
        tag = f"{gp_name} {yr} [{ptype}] pc={row['plan_code']}"

        if out_path.exists():                                  # RESUME
            return ("cached", 0, tag)

        try:
            html = self.fetch(build_body(row, loc))
        except Exception as e:  # noqa: BLE001
            return ("error", 0, f"{tag}: fetch {e}")

        soup = BeautifulSoup(html, "lxml")
        got_year = header_plan_year(soup)
        if got_year is None:
            (self.cfg.raw_html_dir / f"{safe}.html").write_text(html, encoding="utf-8")
            return ("no_table", 0, f"{tag}: no table (session/captcha?)")
        if got_year != yr:
            (self.cfg.raw_html_dir / f"{safe}.html").write_text(html, encoding="utf-8")
            return ("mismatch", 0, f"{tag}: got {got_year} not {yr}")

        meta = {
            "planYear": yr, "stateName": STATE_NAME,
            "zpName": loc["zpNm"], "blockName": loc["bpNm"], "gpName": gp_name,
            "gpCode": gp_code, "planType": ptype,
            "approvalDate": str(row.get("approval_date", "")),
            "planCode": str(row["plan_code"]),
            "planCodeStatus": str(row.get("plan_code_status", "")),
        }
        rows = parse_rows(soup, meta)
        if self.cfg.save_html:
            (self.cfg.raw_html_dir / f"{safe}.html").write_text(html, encoding="utf-8")
        pd.DataFrame(rows, columns=OUT_COLS).to_csv(out_path, index=False, encoding="utf-8-sig")
        if self.cfg.delay:
            time.sleep(self.cfg.delay)
        return ("ok", len(rows), f"{tag}: {len(rows)} activities")

    def load_tasks(self) -> list[tuple]:
        if self._lut is None:
            self._lut = load_lgd_lookup(self.cfg.lgd_file)
        logger.info("LGD lookup: {} GPs", len(self._lut))

        plan = pd.read_csv(self.cfg.plan_csv, dtype=str).fillna("")
        if self.cfg.years is not None:
            before = len(plan)
            plan = plan[plan["fiscal_year"].isin(self.cfg.years)].reset_index(drop=True)
            logger.info("Filtered to {}: {}/{} plan-rows", sorted(self.cfg.years), len(plan), before)

        tasks, missing = [], 0
        for _, row in plan.iterrows():
            loc = self._lut.get(str(row["gp_lgd_code"]).strip())
            if loc is None:
                missing += 1
                continue
            tasks.append((row, loc))
        if missing:
            logger.warning("Skipped {} rows: gp_lgd_code not in LGD file", missing)
        return tasks

    def run(self, progress: bool = True) -> dict:
        tasks = self.load_tasks()
        logger.info("Scraping {} plans with {} workers (delay {}s/req)",
                    len(tasks), self.cfg.workers, self.cfg.delay)

        bar = None
        if progress:
            try:
                from tqdm import tqdm
                bar = tqdm(total=len(tasks), unit="plan")
            except Exception:
                bar = None

        stats: dict[str, int] = {}
        problems: list[str] = []
        done = 0
        with ThreadPoolExecutor(max_workers=self.cfg.workers) as ex:
            for status, n, msg in ex.map(self._worker, tasks):
                done += 1
                stats[status] = stats.get(status, 0) + 1
                if status in ("no_table", "mismatch", "error"):
                    problems.append(msg)
                if status != "cached":
                    logger.info("[{}/{}] {}: {}", done, len(tasks), status, msg)
                if bar:
                    bar.update(1)
                if done == 20 and stats.get("no_table", 0) >= 15:
                    logger.error("Most requests return no table — JSESSIONID is probably "
                                 "expired. Stop, refresh the cookie, and re-run "
                                 "(finished plans are cached).")
        if bar:
            bar.close()

        logger.info("{} ok, {} cached, {} no-table, {} mismatch, {} error",
                    stats.get("ok", 0), stats.get("cached", 0), stats.get("no_table", 0),
                    stats.get("mismatch", 0), stats.get("error", 0))
        if problems:
            logger.warning("First problems ({} of {}):", min(len(problems), 30), len(problems))
            for p in problems[:30]:
                logger.warning("  - {}", p)

        self.combine()
        return stats

    def combine(self) -> None:
        files = sorted(glob.glob(str(self.cfg.per_plan_dir / "*.csv")))
        if not files:
            logger.warning("Nothing in {} to combine.", self.cfg.per_plan_dir)
            return
        frames = [pd.read_csv(f, dtype=str) for f in files]
        df = pd.concat(frames, ignore_index=True)[OUT_COLS]
        df.to_csv(self.cfg.master_csv, index=False, encoding="utf-8-sig")
        df.to_excel(self.cfg.master_xlsx, index=False)
        n_gpyears = df.groupby(["gpName", "planYear"]).ngroups if len(df) else 0
        logger.info("Combined {} plan files -> {} activity rows / {} GP-years -> {}",
                    len(files), len(df), n_gpyears, self.cfg.master_csv)