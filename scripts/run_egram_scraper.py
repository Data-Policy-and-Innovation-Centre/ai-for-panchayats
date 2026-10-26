import sys
import os
import json

# Add project root directory to sys.path to resolve src module imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.ingest.egramSwaraj_API.config import LGD_DATA_FILE, BASE_DIR, PROGRESS_FILE
from src.ingest.egramSwaraj_API.utils import load_progress, save_progress
from src.ingest.egramSwaraj_API.extractor import process_tier

def main():
    if not os.path.exists(LGD_DATA_FILE):
        print(f"Error: Required file not found at '{LGD_DATA_FILE}'.")
        print("Please ensure 'lgd_codes.json' is located inside 'src/ingest/egramSwaraj_API/'.")
        return

    with open(LGD_DATA_FILE, "r", encoding="utf-8") as f:
        lgd_data = json.load(f)

    state_code = lgd_data.get("state_code", "21")
    
    # Initialize data/raw/eGramSwaraj_Data/ directory hierarchy
    zp_base_dir = os.path.join(BASE_DIR, "Zilla_Parishad")
    bp_base_dir = os.path.join(BASE_DIR, "Block_Panchayat")
    gp_base_dir = os.path.join(BASE_DIR, "Gram_Panchayat")
    
    for d in [zp_base_dir, bp_base_dir, gp_base_dir]:
        os.makedirs(d, exist_ok=True)

    progress = load_progress()
    
    print(f"--- Starting eGramSwaraj Extractor for State Code: {state_code} ---")
    print(f"Input Configuration: {LGD_DATA_FILE}")
    print(f"Target Output Directory: {BASE_DIR}")
    print(f"State Tracker Location: {PROGRESS_FILE}\n")

    try:
        process_tier(lgd_data.get("zillas", []), zp_base_dir, "zp_name", "zp_lgd_code", state_code, progress)
        
        for zilla in lgd_data.get("zillas", []):
            process_tier(zilla.get("blocks", []), bp_base_dir, "bp_name", "bp_lgd_code", state_code, progress)
            
            for block in zilla.get("blocks", []):
                process_tier(block.get("gps", []), gp_base_dir, "gp_name", "gp_lgd_code", state_code, progress)
                
    except KeyboardInterrupt:
        print("\n[PAUSED] Process manually interrupted. Saved state safely.")
    except Exception as e:
        print(f"\n[CRITICAL ERROR] Execution failed: {e}")
    finally:
        save_progress(progress)
        print("\nState tracker updated safely.")

if __name__ == "__main__":
    main()