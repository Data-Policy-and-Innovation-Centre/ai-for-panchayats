"""eGramSwaraj (Accounting) voucher ingestion."""

from .config import ScraperConfig, DEFAULT_FIN_YEARS
from .scraper import Scraper

__all__ = ["Scraper", "ScraperConfig", "DEFAULT_FIN_YEARS"]
