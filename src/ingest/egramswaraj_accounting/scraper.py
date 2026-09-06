"""eGramSwaraj (Accounting) voucher scraper.

For each GP x financial year it:
  1. loads the static month-wise page (never 500s) to read the opening balance
     and which months have activity,
  2. drills into only those months' voucher reports (needs a browser session
     cookie + Referer, else the endpoint 500s),
  3. writes one JSON file per GP-year under
     ``<output_dir>/<district>/<block>/<gp>/<year>.json``.

"""

from __future__ import annotations 

import csv
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from loguru import logger

from .config import (
    BASE, FILEREDIRECT, UA, MONTHS, COLUMNS, CSV_FIELDS, ScraperConfig,
)


# --------------------------- pure parse helpers ---------------------------
def _clean(t) -> str:
    return " ".join(str(t).split()).strip()

def _to_num(s):
    s = str(s).replace(",", "").strip()
    try:
        return float(s) if s else None
    except ValueError:
        return None

def _safe(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", str(name)).strip().rstrip(".")
    return name or "_"

def month_wise_url(state: str, fin_year: str, village: str) -> str:
    return f"{FILEREDIRECT}?FD=ExpFY{fin_year}/{state}&name={village}.html"

def parse_opening_balance(html: str):
    soup = BeautifulSoup(html, "lxml")
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        for i, c in enumerate(cells):
            if "Opening Balance" in c.get_text():
                for c2 in cells[i + 1:]:
                    v = _to_num(c2.get_text())
                    if v is not None:
                        return v
    return None

def active_months(html: str) -> dict:
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

def parse_voucher_page(html: str, month: int) -> list[dict]:
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
        vals = [_clean(td.get_text()) for td in tds[:15]]
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

def split_clean(raw_rows: list[dict]) -> dict:
    receipts, payments = [], []
    for r in raw_rows:
        if r.get("receipt_voucher_no", "").strip():
            receipts.append({"month": r.get("month", ""), "date": r.get("receipt_date", ""),
                             "voucher_no": r.get("receipt_voucher_no", ""), "type": r.get("receipt_type", ""),
                             "amount": _to_num(r.get("receipt_amount", "")), "voucher_id": r.get("receipt_voucher_id", "")})
        if r.get("payment_voucher_no", "").strip():
            payments.append({"month": r.get("month", ""), "date": r.get("payment_date", ""),
                             "voucher_no": r.get("payment_voucher_no", ""), "type": r.get("payment_type", ""),
                             "amount": _to_num(r.get("payment_amount", "")), "voucher_id": r.get("payment_voucher_id", "")})

    def _key(v):
        try:
            dd, mm, yy = v["date"].split("/")
            return (int(yy), int(mm), int(dd))
        except Exception:
            return (9999, 99, 99)

    receipts.sort(key=_key)
    payments.sort(key=_key)
    return {"status": "ok", "receipt_count": len(receipts), "payment_count": len(payments),
            "total_receipts": round(sum(v["amount"] for v in receipts if v["amount"]), 2),
            "total_payments": round(sum(v["amount"] for v in payments if v["amount"]), 2),
            "receipts": receipts, "payments": payments}

def year_complete(block) -> bool:
    return isinstance(block, dict) and block.get("status") in ("ok", "no_data")

def _read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


# ------------------------------- rate limiter -------------------------------
class _RateLimiter:
    """Global rate cap: each caller gets a time slot spaced by min_interval,
    then sleeps OUTSIDE the lock so workers overlap their network waits."""
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next = 0.0
    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.min_interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


# ------------------------------- scraper -------------------------------
class Scraper:
    def __init__(self, config: ScraperConfig):
        self.cfg = config
        self._rate = _RateLimiter(config.min_interval)
        self._local = threading.local()

    # -- http --
    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.cookies.update(self.cfg.cookie)
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.cfg.workers, pool_maxsize=self.cfg.workers)
            s.mount("https://", adapter)
            s.mount("http://", adapter)
            self._local.session = s
        return s

    def get(self, url: str, referer: str | None = None) -> str:
        headers = {"User-Agent": UA,
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                   "Accept-Language": "en-US,en;q=0.9", "Connection": "keep-alive",
                   "Upgrade-Insecure-Requests": "1"}
        if referer:
            headers["Referer"] = referer
        session = self._session()
        last = None
        for attempt in range(1, self.cfg.max_retries + 1):
            self._rate.wait()
            try:
                r = session.get(url, headers=headers, timeout=60)
                if r.status_code == 200:
                    return r.text
                last = f"HTTP {r.status_code}"
            except requests.RequestException as e:
                last = str(e)
            if attempt < self.cfg.max_retries:
                time.sleep(self.cfg.retry_backoff * attempt)
        raise RuntimeError(f"failed after {self.cfg.max_retries} tries: {last}")

    # -- targets & paths --
    def load_targets(self) -> list[dict]:
        d = json.loads(Path(self.cfg.lgd_file).read_text(encoding="utf-8"))
        want = set(self.cfg.districts)
        targets = []
        for z in d["zillas"]:
            dcode = str(z["zp_lgd_code"])
            if want and dcode not in want:
                continue
            for b in z["blocks"]:
                for g in b["gps"]:
                    targets.append({
                        "name": g["gp_name"].strip(), "district": dcode,
                        "district_name": z["zp_name"].strip(),
                        "block": str(b["bp_lgd_code"]), "block_name": b["bp_name"].strip(),
                        "village": str(g["gp_lgd_code"]),
                    })
        return targets

    def _gp_dir(self, t: dict) -> Path:
        return (Path(self.cfg.output_dir) / _safe(t["district_name"])
                / _safe(t["block_name"]) / _safe(t["name"]))

    def _year_path(self, t: dict, fin_year: str) -> Path:
        return self._gp_dir(t) / f"{fin_year}.json"

    def _gp_is_complete(self, t: dict) -> bool:
        return all(self._year_path(t, fy).exists()
                   and year_complete(_read_json(self._year_path(t, fy)))
                   for fy in self.cfg.fin_years)

    # -- scraping --
    def scrape_year(self, ids: dict, fin_year: str) -> dict:
        mw_url = month_wise_url(self.cfg.state, fin_year, ids["village"])
        try:
            mw_html = self.get(mw_url)
        except Exception as e:
            return {"status": "fetch_failed", "note": f"month-wise page: {e}"}

        ob = parse_opening_balance(mw_html)
        months = active_months(mw_html)
        if not months:
            return {"status": "no_data", "opening_balance": ob, "receipt_count": 0,
                    "payment_count": 0, "total_receipts": 0, "total_payments": 0,
                    "receipts": [], "payments": []}

        raw, failed = [], []
        for m in sorted(months):
            try:
                raw += parse_voucher_page(self.get(months[m], referer=mw_url), m)
            except Exception as e:
                failed.append(f"{MONTHS[m]}: {e}")

        if failed:
            return {"status": "fetch_failed", "opening_balance": ob,
                    "note": "; ".join(failed), "active_months": sorted(months)}
        block = split_clean(raw)
        block["opening_balance"] = ob
        return block

    def scrape_gp(self, t: dict) -> tuple[dict, bool, list[str]]:
        ids = {"district": t["district"], "block": t["block"], "village": t["village"]}
        self._gp_dir(t).mkdir(parents=True, exist_ok=True)
        lines, any_fail = [], False
        for fy in self.cfg.fin_years:
            yp = self._year_path(t, fy)
            if yp.exists() and year_complete(_read_json(yp)):
                continue
            yb = self.scrape_year(ids, fy)
            record = {"gp_name": t["name"], "gp_lgd_code": t["village"], "state": self.cfg.state,
                      "district_name": t["district_name"], "district_code": t["district"],
                      "block_name": t["block_name"], "block_code": t["block"],
                      "year": fy, **yb}
            yp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
            if yb["status"] == "ok":
                ob = yb.get("opening_balance"); obs = f" OB={ob:,.2f}" if ob is not None else ""
                lines.append(f"{fy}: r={yb['receipt_count']} p={yb['payment_count']}{obs}")
            elif yb["status"] == "no_data":
                lines.append(f"{fy}: no activity")
            else:
                any_fail = True
                lines.append(f"{fy}: FETCH FAILED -> {yb.get('note', '')[:100]}")
        return t, any_fail, lines

    # -- csv --
    def build_master_csv(self) -> None:
        files = sorted(Path(self.cfg.output_dir).rglob("*.json"))
        if not files:
            logger.warning("No JSON found under {} — CSV not written", self.cfg.output_dir)
            return
        n_rows = n_files = 0
        with open(self.cfg.master_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            for path in files:
                rec = _read_json(path)
                if not rec:
                    continue
                for row in _rows_from_year_file(rec):
                    w.writerow(row); n_rows += 1
                n_files += 1
        logger.info("CSV: {} year-files -> {} rows -> {}", n_files, n_rows, self.cfg.master_csv)

    # -- orchestration --
    def run(self, progress: bool = True) -> dict:
        targets = self.load_targets()
        scope = "ALL districts" if not self.cfg.districts else f"districts {self.cfg.districts}"
        logger.info("{} GPs in scope ({}) x {} years | {} workers, ~{:.1f} req/s cap",
                    len(targets), scope, len(self.cfg.fin_years),
                    self.cfg.workers, 1 / self.cfg.min_interval)

        by_district: dict[str, list[dict]] = {}
        for t in targets:
            by_district.setdefault(t["district"], []).append(t)

        bar = None
        if progress:
            try:
                from tqdm import tqdm
                bar = tqdm(total=len(targets), unit="gp")
            except Exception:
                bar = None

        g_ok = g_failed = g_skipped = 0
        for dcode, dgps in by_district.items():
            dname = dgps[0]["district_name"]
            d_ok = d_failed = d_skipped = 0
            todo = []
            for t in dgps:
                if self._gp_is_complete(t):
                    d_skipped += 1; g_skipped += 1
                    if bar: bar.update(1)
                else:
                    todo.append(t)

            with ThreadPoolExecutor(max_workers=self.cfg.workers) as ex:
                futures = {ex.submit(self.scrape_gp, t): t for t in todo}
                for fut in as_completed(futures):
                    t, any_fail, lines = fut.result()
                    for ln in lines:
                        logger.info("{} / {} / {} | {}", t["name"], t["block_name"], dname, ln)
                    if any_fail:
                        d_failed += 1; g_failed += 1
                    else:
                        d_ok += 1; g_ok += 1
                    if bar: bar.update(1)

            logger.info("District {} ({}) done: {}/{} complete "
                        "({} fresh, {} already-done, {} need retry)",
                        dname, dcode, d_ok + d_skipped, len(dgps), d_ok, d_skipped, d_failed)

        if bar:
            bar.close()
        logger.info("SUMMARY: {}/{} GPs complete ({} fresh, {} already-done); {} need retry",
                    g_ok + g_skipped, len(targets), g_ok, g_skipped, g_failed)

        self.build_master_csv()
        return {"total": len(targets), "ok": g_ok, "skipped": g_skipped, "failed": g_failed}


def _rows_from_year_file(rec: dict):
    common = {"district_name": rec.get("district_name", ""), "district_code": rec.get("district_code", ""),
              "block_name": rec.get("block_name", ""), "block_code": rec.get("block_code", ""),
              "gp_name": rec.get("gp_name", ""), "gp_code": rec.get("gp_lgd_code", ""),
              "year": rec.get("year", ""), "year_status": rec.get("status", ""),
              "opening_balance": rec.get("opening_balance", "")}
    vouchers = (rec.get("receipts") or []) + (rec.get("payments") or [])
    if not vouchers:
        yield dict(common, kind="", month="", date="", voucher_no="",
                   voucher_type="", amount="", voucher_id="")
        return
    for kind, key in (("receipt", "receipts"), ("payment", "payments")):
        for v in rec.get(key, []) or []:
            yield dict(common, kind=kind, month=v.get("month", ""), date=v.get("date", ""),
                       voucher_no=v.get("voucher_no", ""), voucher_type=v.get("type", ""),
                       amount=v.get("amount", ""), voucher_id=v.get("voucher_id", ""))