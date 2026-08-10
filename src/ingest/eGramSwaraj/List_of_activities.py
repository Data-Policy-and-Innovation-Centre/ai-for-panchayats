"""
eGramSwaraj -- "List of Activities" (GP action plan) report -> JSON

Parses the HTML returned by actionPlanReportForGPFinal.do into a single
JSON object: report-level metadata (plan year, state, ZP/BP/GP) + one
record per activity row.

Usage
-----
    # parse a saved page
    python egs_action_plan.py report.html -o out.json

    # parse many saved pages into one JSON array
    python egs_action_plan.py saved/*.html -o all.json

    # from Python
    from egs_action_plan import parse_report
    data = parse_report(open("report.html", encoding="utf-8").read())

Requires: beautifulsoup4  (pip install beautifulsoup4)
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from typing import Any

from bs4 import BeautifulSoup

# ---------------------------------------------------------------- constants

# Column order of the activity table (#myTable), mapped to JSON keys.
ACTIVITY_FIELDS = [
    ("S.No.", "sl_no"),
    ("Activity Code", "activity_code"),
    ("Activity Name", "activity_name"),
    ("Activity Cost", "activity_cost"),
    ("Focus Area", "focus_area"),
    ("Activity Type", "activity_type"),
    ("Activity Status", "activity_status"),
    ("Activity Output Type", "activity_output_type"),
    ("Plan Type", "plan_type"),
]

# Header-table labels -> JSON keys. The report only renders the tier
# columns that apply to the selected state, so this is matched by label,
# not by position.
HEADER_LABEL_MAP = {
    "plan year": "plan_year",
    "state name": "state",
    "district panchayat & equivalent": "district_panchayat",
    "block panchayat and equivalent": "block_panchayat",
    "gram panchayat & equivalent": "gram_panchayat",
    # tolerate nomenclature variants used by some states
    "zilla panchayat & equivalent": "district_panchayat",
    "intermediate panchayat & equivalent": "block_panchayat",
    "village panchayat & equivalent": "gram_panchayat",
}

# Hidden form inputs carry the same names; used as a fallback.
HIDDEN_INPUT_MAP = {
    "state_nam": "state",
    "zpname": "district_panchayat",
    "bpname": "block_panchayat",
    "gpname": "gram_panchayat",
}

_WS = re.compile(r"\s+")


# ------------------------------------------------------------------ helpers

def _clean(node) -> str:
    """Collapsed, stripped text of a tag (handles &amp;, &nbsp;, tabs)."""
    if node is None:
        return ""
    text = node.get_text(" ", strip=True).replace("\xa0", " ")
    return _WS.sub(" ", text).strip()


def _to_int(text: str):
    """'201672' -> 201672 ; '1,50,000' -> 150000 ; '' -> None."""
    digits = re.sub(r"[^\d-]", "", text or "")
    if digits in ("", "-"):
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _soup(html: str) -> BeautifulSoup:
    # The page nests multiple <html>/<body> tags. html.parser keeps all of
    # it; lxml/html5lib may discard the second document. Do not change this.
    return BeautifulSoup(html, "html.parser")


# ------------------------------------------------------------------ parsing

def _parse_header(soup: BeautifulSoup) -> dict[str, Any]:
    """Plan year / state / ZP / BP / GP from the summary table + hidden inputs."""
    meta: dict[str, Any] = {
        "plan_year": None,
        "state": None,
        "district_panchayat": None,
        "block_panchayat": None,
        "gram_panchayat": None,
    }

    # Summary table = the thead with two <tr>, the first containing "Plan Year".
    for thead in soup.find_all("thead"):
        rows = thead.find_all("tr", recursive=False) or thead.find_all("tr")
        if len(rows) < 2:
            continue
        labels = [_clean(th).lower() for th in rows[0].find_all("th")]
        if "plan year" not in labels:
            continue
        values = [_clean(th) for th in rows[1].find_all("th")]
        for label, value in zip(labels, values):
            key = HEADER_LABEL_MAP.get(label)
            if key:
                meta[key] = value or None
        break

    # Fallback / cross-check against the hidden inputs on the form.
    for inp in soup.find_all("input", attrs={"type": "hidden"}):
        key = HIDDEN_INPUT_MAP.get(inp.get("name", ""))
        if key and not meta.get(key):
            value = (inp.get("value") or "").strip()
            meta[key] = value or None

    # The report page does not echo back the "Panchayat Level" dropdown,
    # so derive it from which tier columns were rendered.
    if meta["gram_panchayat"]:
        meta["panchayat_level"] = "Gram Panchayat"
    elif meta["block_panchayat"]:
        meta["panchayat_level"] = "Block Panchayat"
    elif meta["district_panchayat"]:
        meta["panchayat_level"] = "District Panchayat"
    else:
        meta["panchayat_level"] = None

    return meta


def _parse_generated_on(soup: BeautifulSoup) -> str | None:
    match = re.search(
        r"Report Generated on\s+([\d/]{8,10}\s+[\d:]{5,8}\s*[AP]M)",
        soup.get_text(" ", strip=True),
    )
    return match.group(1) if match else None


def _activity_table(soup: BeautifulSoup):
    table = soup.find("table", id="myTable")
    if table is not None:
        return table
    # Fallback: the table whose header row starts with "S.No."
    for candidate in soup.find_all("table"):
        head = candidate.find("thead")
        if head and _clean(head.find("th")).lower().startswith("s.no"):
            return candidate
    return None


def _parse_activities(soup: BeautifulSoup) -> list[dict[str, Any]]:
    table = _activity_table(soup)
    if table is None:
        return []

    body = table.find("tbody") or table
    activities = []
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue  # header / spacer row
        values = [_clean(td) for td in cells]
        # Pad short rows (trailing Plan Type cell is sometimes omitted).
        values += [""] * (len(ACTIVITY_FIELDS) - len(values))

        row = {key: (values[i] or None) for i, (_, key) in enumerate(ACTIVITY_FIELDS)}
        row["sl_no"] = _to_int(row["sl_no"] or "")
        row["activity_cost"] = _to_int(values[3])
        activities.append(row)
    return activities


def parse_report(html: str, source: str | None = None) -> dict[str, Any]:
    """Parse one report page into {metadata..., activities: [...]}."""
    soup = _soup(html)
    report = _parse_header(soup)
    report["report_generated_on"] = _parse_generated_on(soup)
    if source:
        report["source_file"] = source
    report["activities"] = _parse_activities(soup)
    report["activity_count"] = len(report["activities"])
    report["total_activity_cost"] = sum(
        a["activity_cost"] or 0 for a in report["activities"]
    )
    return report


def flatten(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One flat dict per activity, with the report metadata repeated.
    Useful for pandas.DataFrame(flatten(report)) or CSV output."""
    meta = {k: v for k, v in report.items() if k != "activities"}
    return [{**meta, **activity} for activity in report["activities"]]


# ----------------------------------------------------- optional: fetching

def fetch_report(
    fyear: str,
    state_code: str,
    panchayat_level: str,
    district_id: str,
    block_id: str,
    gp_id: str,
    captcha: str,
    session=None,
):
    """POST the report form and return the response HTML.

    The form is captcha-gated, so `captcha` must be the six characters you
    read off the image in that same browser/requests session -- this is a
    human-in-the-loop helper, not an unattended crawler. Field names below
    come from the form's own JS; confirm them against the request payload
    in DevTools -> Network for your state, since a few states send extra
    tier fields.
    """
    import requests

    session = session or requests.Session()
    base = "https://egramswaraj.gov.in"
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "Referer": f"{base}/actionPlanReportForGP.do",
        }
    )
    # Establish a JSESSIONID and the captcha bound to it.
    session.get(f"{base}/actionPlanReportForGP.do", timeout=60)

    payload = {
        "fyear": fyear,
        "stateCode": state_code,
        "panchayatLevel": panchayat_level,
        "districtId": district_id,
        "blockId": block_id,
        "gpId": gp_id,
        "captchaAnswer": captcha,
    }
    resp = session.post(
        f"{base}/actionPlanReportForGPFinal.do", data=payload, timeout=120
    )
    resp.raise_for_status()
    return resp.text


# ---------------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="saved HTML file(s); globs allowed")
    ap.add_argument("-o", "--out", help="output .json (default: stdout)")
    ap.add_argument(
        "--flat",
        action="store_true",
        help="emit one flat record per activity instead of nested reports",
    )
    args = ap.parse_args(argv)

    files = [p for pattern in args.paths for p in sorted(glob.glob(pattern))] or args.paths

    reports = []
    for path in files:
        with open(path, encoding="utf-8", errors="replace") as fh:
            report = parse_report(fh.read(), source=path)
        if not report["activities"]:
            print(f"warning: no activity rows found in {path}", file=sys.stderr)
        reports.append(report)

    if args.flat:
        payload: Any = [row for r in reports for row in flatten(r)]
    else:
        payload = reports[0] if len(reports) == 1 else reports

    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        n = sum(r["activity_count"] for r in reports)
        print(f"wrote {args.out}: {len(reports)} report(s), {n} activities")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())