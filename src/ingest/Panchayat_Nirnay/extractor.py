import time
import requests
from pathlib import Path
from src.ingest.Panchayat_Nirnay.api import download_file
from src.ingest.Panchayat_Nirnay.utils import get_start_index, save_progress

def scrape_catalog(target_gps, headers, list_api, details_api, download_base, output_dir: Path, progress_file: Path):
    start_idx = get_start_index(progress_file)
    output_dir.mkdir(parents=True, exist_ok=True)

    if start_idx >= len(target_gps):
        print("🎉 Catalog complete! All GPs have been scraped.")
        return

    print(f"--- INITIALIZING BATCH RUN (Starting at Index {start_idx}) ---")

    for idx in range(start_idx, len(target_gps)):
        gp = target_gps[idx]
        print(f"\n=======================================================")
        print(f"📍 PROCESSING GP [{idx+1}/{len(target_gps)}]: {gp['gp_name']} (Code: {gp['gp_code']})")
        print(f"=======================================================")
        
        # Build strict folder hierarchy using human-readable names
        gp_folder = output_dir / gp['zp_name'] / gp['bp_name'] / gp['gp_name']
        gp_folder.mkdir(parents=True, exist_ok=True)

        list_payload = {
            "meeting_type_id": 0, "financial_year": "", "state_id": 21, 
            "zp_id": gp['zp_id'], "bp_id": gp['bp_id'], "local_body_code": gp['gp_code'], 
            "skip": 0, "limit": 50
        }

        try:
            res = requests.post(list_api, headers=headers, json=list_payload, timeout=15)
            
            # --- TOKEN EXPIRATION CHECK ---
            if res.status_code == 401:
                print(f"\n[!] ALERT: SKELETON KEY EXPIRED! (HTTP 401)")
                print(f"[!] The server rejected the timestamp/secretkey.")
                print(f"[!] Script paused at Index {idx} ({gp['gp_name']}).")
                print(f"[!] Action Required: Capture a new token pair, update config.py, and rerun. It will automatically resume here.")
                return

            res.raise_for_status()
            meetings_data = res.json()
            
        except Exception as e:
            print(f"[!] FAILED to fetch meeting list for {gp['gp_name']}: {e}")
            continue

        meeting_groups = meetings_data.get('data', [])
        if not meeting_groups:
            print("    [i] No meetings found for this GP.")
        
        for group in meeting_groups:
            for meeting in group.get('meetings', []):
                m_id = meeting['meeting_id']
                m_title = meeting['meeting_title'].replace("/", "-") 
                print(f"\n  --- Fetching Meeting: {m_id} | {m_title[:30]} ---")
                
                # Keep the meeting subfolder as the unique meeting_id
                meeting_folder = gp_folder / str(m_id)
                meeting_folder.mkdir(parents=True, exist_ok=True)

                try:
                    detail_res = requests.post(details_api, headers=headers, json={"meeting_id": m_id}, timeout=15)
                    detail_res.raise_for_status()
                    data_payload = detail_res.json().get('data', {})
                except Exception as e:
                    print(f"    [!] FAILED to fetch metadata for {m_id}: {e}")
                    continue
                
                # Extract Photos
                for photo in data_payload.get('meeting_photos', []):
                    if p := photo.get('photo_url'):
                        save_path = meeting_folder / p.split('file_name=')[-1]
                        download_file(f"{download_base}?{p}", save_path, headers)
                        time.sleep(1) 

                # Extract Documents
                for doc_type in ['agenda_copy', 'decision_copy', 'attendance_copy']:
                    for doc in data_payload.get('meeting_documents', {}).get(doc_type, []):
                        if p := doc.get(f"{doc_type.split('_')[0]}_url"):
                            save_path = meeting_folder / f"{doc_type}_{p.split('file_name=')[-1]}"
                            download_file(f"{download_base}?{p}", save_path, headers)
                            time.sleep(1)

                # Extract Videos
                for video in data_payload.get('videos', []):
                    vid_url = video.get('video_url', "")
                    if ".mp4" in vid_url and "file_name=" in vid_url:
                        try:
                            p = vid_url.split('paths": ["')[1].split('"]')[0]
                            save_path = meeting_folder / p.split('file_name=')[-1]
                            download_file(f"{download_base}?{p}", save_path, headers)
                            time.sleep(1)
                        except IndexError: pass

                time.sleep(2) 

        # Save progress after completely finishing a GP
        save_progress(progress_file, idx)
        print(f"✓ GP {gp['gp_name']} fully completed. Progress saved.")

    print("\n--- BATCH RUN COMPLETE ---")