import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.append(str(PROJECT_ROOT))

from src.ingest.sabhasaar.config import SOURCE_PORTAL, STATE_CODE, HEADERS, FILTERS
from src.ingest.sabhasaar.extractor import run_extraction
from config import directories

if __name__ == "__main__":
    RUN_DATE = datetime.now().strftime("%Y-%m-%d")
    
    OUTPUT_DATASET = directories.RAW_DATA / f"SabhaSaar_{RUN_DATE}"
    
    run_extraction(
        base_url=SOURCE_PORTAL,
        state_code=STATE_CODE,
        headers=HEADERS,
        filters=FILTERS,
        output_base_path=str(OUTPUT_DATASET) 
    )