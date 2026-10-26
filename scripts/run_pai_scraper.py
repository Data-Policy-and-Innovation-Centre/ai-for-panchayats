import sys
from pathlib import Path

# Bootstrap: add repo root to sys.path so "src" is importable when running
# this script directly (e.g. `python scripts/run_pai_scraper.py`).
# Without this, Python puts the scripts/ directory on sys.path, not the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest.PAI.extractor import scrape_all

if __name__ == "__main__":
    scrape_all()
