import sys
from datetime import datetime
from pathlib import Path

# 1. Dynamically find the project root
# __file__ = this script
# .resolve() = gets the absolute, full path
# .parent = the 'scripts' folder
# .parent.parent = the 'ai-for-panchayats' root folder
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 2. Adding the root to sys.path 
sys.path.append(str(PROJECT_ROOT))

from src.ingest.sabhasaar.config import SOURCE_PORTAL, STATE_CODE, HEADERS, FILTERS
from src.ingest.sabhasaar.extractor import run_extraction

if __name__ == "__main__":
    RUN_DATE = datetime.now().strftime("%Y-%m-%d")
    
    OUTPUT_DATASET = PROJECT_ROOT / "data" / "raw" / f"SabhaSaar_{RUN_DATE}"
    
    run_extraction(
        base_url=SOURCE_PORTAL,
        state_code=STATE_CODE,
        headers=HEADERS,
        filters=FILTERS,
        output_base_path=str(OUTPUT_DATASET) 
    )