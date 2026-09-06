

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

URL = "https://egramswaraj.gov.in/actexpenditurereport.do"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# fixed POST-body constants
STATE_NAME = "Odisha"
LVL_FLG = "12"
STATUS_LEVEL = "99"

DEFAULT_YEARS = ["2025-2026", "2024-2025", "2023-2024",
                 "2022-2023", "2021-2022", "2020-2021"]

META_COLS = ["planYear", "stateName", "zpName", "blockName", "gpName",
             "gpCode", "planType", "approvalDate", "planCode", "planCodeStatus"]
DATA_COLS = [
    "S.No.", "Activity Code", "Activity Name", "Activity For", "Focus Area",
    "Approved Cost in Action Plan", "Technical Approved Cost",
    "Admin Approved Cost", "Scheme Name", "General", "SC", "ST",
    "Total Expenditure", "Voucher Date", "Voucher No", "Voucher Cost",
]
OUT_COLS = META_COLS + DATA_COLS


@dataclass
class ExpenditureConfig:
    """Everything the scraper needs. Paths and cookie come from the caller."""
    cookie: dict                       # e.g. {"JSESSIONID": "..."}
    lgd_file: Path                     # Cleaned_LGD_codes.json
    plan_csv: Path                     # plan.csv (plan codes per GP/year)
    output_dir: Path                   # root for this source's outputs (under data/raw)
    years: set[str] | None = field(default_factory=lambda: set(DEFAULT_YEARS))  # None = all
    workers: int = 10
    delay: float = 0.1                 # politeness pause inside each worker
    retries: int = 2
    save_html: bool = False            # save every page's HTML (debug only)

    # derived output locations
    @property
    def per_plan_dir(self) -> Path:
        return self.output_dir / "per_plan"

    @property
    def raw_html_dir(self) -> Path:
        return self.output_dir / "raw_html"

    @property
    def master_csv(self) -> Path:
        return self.output_dir / "expenditure_all.csv"

    @property
    def master_xlsx(self) -> Path:
        return self.output_dir / "expenditure_all.xlsx"