"""Configuration and constants for the eGramSwaraj (Accounting) scraper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# --- fixed site constants ---
BASE = "https://egramswaraj.gov.in"
FILEREDIRECT = f"{BASE}/FileRedirect.jsp"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MONTHS = {1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
          7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December"}

DEFAULT_STATE = "21"   # Odisha
DEFAULT_FIN_YEARS = ["2025-2026", "2024-2025", "2023-2024",
                     "2022-2023", "2021-2022", "2020-2021"]

# 15 flattened report columns (Receipts | Payments | Contra | Journal)
COLUMNS = ["receipt_date", "receipt_voucher_no", "receipt_type", "receipt_amount",
           "payment_date", "payment_voucher_no", "payment_type", "payment_amount", "case_record",
           "contra_date", "contra_voucher_no", "contra_amount",
           "journal_date", "journal_voucher_no", "journal_amount"]

# columns of the combined CSV (one row per voucher)
CSV_FIELDS = ["district_name", "district_code", "block_name", "block_code",
              "gp_name", "gp_code", "year", "year_status", "opening_balance",
              "kind", "month", "date", "voucher_no", "voucher_type", "amount", "voucher_id"]


@dataclass
class ScraperConfig:
    """Everything the scraper needs. Paths and cookie come from the caller."""
    cookie: dict                       # e.g. {"JSESSIONID": "..."}
    lgd_file: Path                     # Cleaned_LGD_codes.json
    output_dir: Path                   # per-year JSON tree root
    master_csv: Path                   # combined CSV path
    state: str = DEFAULT_STATE
    districts: list[str] = field(default_factory=list)          # [] = all districts
    fin_years: list[str] = field(default_factory=lambda: list(DEFAULT_FIN_YEARS))
    workers: int = 4
    min_interval: float = 0.25         # seconds between request starts (global cap)
    max_retries: int = 3
    retry_backoff: float = 4.0