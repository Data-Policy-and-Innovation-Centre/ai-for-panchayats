import time
import requests
import csv
from datetime import datetime
from pathlib import Path
from src.ingest.Panchayat_Nirnay.api import download_file
from src.ingest.Panchayat_Nirnay.utils import safe_name

def scrape_catalog(target_gps, headers, list_api, agenda_api, details_api, download_base, output_dir: Path):
    
    output_dir.mkdir(parents=True, exist_ok=True)
    start_idx = 0

    # 1. Initialize the Audit Log Data Structure
    audit_log = [["Timestamp", "District (ZP)", "Block (BP)", "Gram Panchayat (GP)", "Target / Meeting", "Status", "Details"]]
    
    # Create a unique filename for the report based on the exact time it runs
    run_timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = output_dir / f"Extraction_Audit_Report_{run_timestamp}.csv"

    print(f"--- INITIALIZING AUDIT SCRAPE ---")

    try:
        for idx in range(start_idx, len(target_gps)):
            gp = target_gps[idx]
            zp_name = gp['zp_name']
            bp_name = gp['bp_name']
            gp_name = gp['gp_name']

            print(f"\n=======================================================")
            print(f"📍 PROCESSING GP [{idx+1}/{len(target_gps)}]: {gp_name} (Code: {gp['gp_code']})")
            print(f"=======================================================")
            
            zp_folder = f"{safe_name(zp_name)} ({gp['zp_id']})"
            bp_folder = f"{safe_name(bp_name)} ({gp['bp_id']})"
            gp_folder_name = f"{safe_name(gp_name)} ({gp['gp_code']})"
            
            gp_folder = output_dir / zp_folder / bp_folder / gp_folder_name
            gp_folder.mkdir(parents=True, exist_ok=True)

            payload = {
                "meeting_type_id": 0, "financial_year": "", "state_id": 21, 
                "zp_id": gp['zp_id'], "bp_id": gp['bp_id'], "local_body_code": gp['gp_code'], 
                "skip": 0, "limit": 100
            }

            unique_meetings = {}

            def extract_meetings(data_json):
                for group in data_json.get('data', []):
                    for m in group.get('meetings', []):
                        m_id = m['meeting_id']
                        raw_title = m.get('meeting_title', 'Untitled')
                        clean_title = safe_name(raw_title)[:40] 
                        
                        raw_date = m.get('meeting_start_date', 'UnknownDate')
                        clean_date = raw_date.split('T')[0].split(' ')[0] if raw_date else "UnknownDate"
                        
                        folder_name = f"{clean_date} - {clean_title} ({m_id})"
                        unique_meetings[m_id] = folder_name

            # --- PHASE 1A: Fetch from 'Recent Meetings' Tab ---
            try:
                res_meetings = requests.post(list_api, headers=headers, json=payload, timeout=15)
                if res_meetings.status_code == 401:
                    print(f"\n[!] ALERT: SKELETON KEY EXPIRED! (HTTP 401) on Recent Meetings.")
                    audit_log.append([datetime.now().isoformat(), zp_name, bp_name, gp_name, "Recent Meetings API", "FAILED", "HTTP 401: Skeleton Key Expired"])
                    return
                extract_meetings(res_meetings.json())
            except Exception as e:
                print(f"[!] FAILED to fetch Recent Meetings list: {e}")
                audit_log.append([datetime.now().isoformat(), zp_name, bp_name, gp_name, "Recent Meetings API", "FAILED", str(e)])

            # --- PHASE 1B: Fetch from 'Agenda Items' Tab ---
            try:
                res_agenda = requests.post(agenda_api, headers=headers, json=payload, timeout=15)
                if res_agenda.status_code == 401:
                    print(f"\n[!] ALERT: SKELETON KEY EXPIRED! (HTTP 401) on Agenda Items.")
                    audit_log.append([datetime.now().isoformat(), zp_name, bp_name, gp_name, "Agenda API", "FAILED", "HTTP 401: Skeleton Key Expired"])
                    return
                extract_meetings(res_agenda.json())
            except Exception as e:
                print(f"[!] FAILED to fetch Agenda list: {e}")
                audit_log.append([datetime.now().isoformat(), zp_name, bp_name, gp_name, "Agenda API", "FAILED", str(e)])

            # Log if a GP is totally empty
            if not unique_meetings:
                audit_log.append([datetime.now().isoformat(), zp_name, bp_name, gp_name, "Meeting List", "WARNING", "No meetings found on portal"])
                continue

            print(f"  ✓ Found {len(unique_meetings)} unique meetings after merging lists.")

            # --- PHASE 2:  Dive into Each Unique Meeting ---
            for m_id, folder_name in unique_meetings.items():
                print(f"\n  --- Extracting Meeting: {folder_name} ---")
                meeting_folder = gp_folder / folder_name
                meeting_folder.mkdir(parents=True, exist_ok=True)

                try:
                    detail_res = requests.post(details_api, headers=headers, json={"meeting_id": m_id}, timeout=15)
                    if detail_res.status_code == 401:
                        print(f"\n[!] ALERT: SKELETON KEY EXPIRED! (HTTP 401) during details fetch.")
                        audit_log.append([datetime.now().isoformat(), zp_name, bp_name, gp_name, folder_name, "FAILED", "HTTP 401: Skeleton Key Expired"])
                        return
                    detail_res.raise_for_status()
                    data_payload = detail_res.json().get('data', {})
                except Exception as e:
                    print(f"    [!] FAILED to fetch metadata for {m_id}: {e}")
                    audit_log.append([datetime.now().isoformat(), zp_name, bp_name, gp_name, folder_name, "FAILED", f"Metadata Fetch Error: {e}"])
                    continue
                
                downloaded_count = 0

                # --- Extract General Photos ---
                for photo in data_payload.get('meeting_photos', []):
                    if p := photo.get('photo_url'):
                        save_name = f"[PHOTO]_{p.split('file_name=')[-1]}"
                        download_file(f"{download_base}?{p}", meeting_folder / save_name, headers)
                        downloaded_count += 1

                # --- Extract Register "Documents" ---
                for doc_type in ['agenda_copy', 'decision_copy', 'attendance_copy']:
                    for doc in data_payload.get('meeting_documents', {}).get(doc_type, []):
                        if p := doc.get(f"{doc_type.split('_')[0]}_url"):
                            clean_doc_tag = f"[{doc_type.split('_')[0].upper()}]"
                            save_name = f"{clean_doc_tag}_{p.split('file_name=')[-1]}"
                            download_file(f"{download_base}?{p}", meeting_folder / save_name, headers)
                            downloaded_count += 1

                # Log the success for this specific meeting
                audit_log.append([datetime.now().isoformat(), zp_name, bp_name, gp_name, folder_name, "SUCCESS", f"Downloaded {downloaded_count} files"])

                time.sleep(1) 

            print(f"\n✓ GP {gp_name} fully completed.")

    finally:
        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.writer(csvfile)
                writer.writerows(audit_log)
            print(f"\n[SYSTEM] Run terminated. Audit Report saved to: {csv_path}")
        except Exception as e:
            print(f"\n[SYSTEM] Failed to save Audit Report: {e}")

    print("\n--- BATCH RUN COMPLETE ---")