import os
import sys
import json
import time
from pathlib import Path

# Adjust Python path so it can find the src package when run from the scripts/ directory
project_root = Path(__file__).resolve().parent.parent
sys.path.append(str(project_root / "src"))

from eGramSwaraj_panchayat_profile.scrapper import scrape_panchayat_info
from eGramSwaraj_panchayat_profile.config import OUTPUT_DIR

def load_panchayat_parameters(filepath=None):
    """
    Loads the hierarchical list of parameters for all panchayats.
    """
    if filepath is None:
        filepath = project_root / "data" / "raw" / "Cleaned_LGD_codes.json"
        
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"Error: {filepath} not found. Please provide the JSON file containing the LGD codes.")
    
    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    state_id = str(data.get("state_code", "21"))
    parameters_list = []
    
    # Flatten the hierarchical JSON into a list of parameters
    for zilla in data.get("zillas", []):
        label1 = str(zilla.get("zp_lgd_code"))
        for block in zilla.get("blocks", []):
            label11 = str(block.get("bp_lgd_code"))
            for gp in block.get("gps", []):
                label111 = str(gp.get("gp_lgd_code"))
                
                parameters_list.append({
                    "stateId": state_id,
                    "localBodyTypeCode": "3",  # 3 indicates Gram Panchayat level
                    "label1": label1,
                    "label11": label11,
                    "label111": label111,
                    "gp_name": gp.get("gp_name"),
                    "bp_name": block.get("bp_name"),
                    "zp_name": zilla.get("zp_name")
                })
                
    return parameters_list

def main():
    # Load parameters for all panchayats
    parameters_list = load_panchayat_parameters()
    print(f"Loaded {len(parameters_list)} panchayats to scrape from the cleaned LGD codes file.")
    
    # Ensure the output directory exists
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    for params in parameters_list:
        panchayat_id = params.get("label111")
        panchayat_name = params.get("gp_name", "Unknown")
        
        filename = f"panchayat_{panchayat_id}.json"
        output_file = os.path.join(OUTPUT_DIR, filename)
        
        # Skip if the file already exists
        if os.path.exists(output_file):
            print(f"Skipping {panchayat_name} (ID: {panchayat_id}) - File already exists.")
            continue
            
        print(f"Scraping data for {panchayat_name} (ID: {panchayat_id})...")
        
        data = scrape_panchayat_info(
            params.get("stateId"), 
            params.get("localBodyTypeCode"), 
            params.get("label1"), 
            params.get("label11"), 
            panchayat_id
        )
        
        if data:
            # Store the request parameters inside the data for reference
            data['request_params'] = params
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
                
            print(f"Data successfully saved to {output_file}")
        else:
            print(f"Failed to retrieve data for {panchayat_name} (ID: {panchayat_id})")
            
        # Polite delay to prevent server overload
        time.sleep(0.5)

if __name__ == "__main__":
    main()
