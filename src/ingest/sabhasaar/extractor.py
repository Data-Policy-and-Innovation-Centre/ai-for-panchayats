import os
import json
import csv
import re
from datetime import datetime
from indic_transliteration import sanscript
from src.ingest.sabhasaar.api import fetch_json
from src.ingest.sabhasaar.utils import clean_minutes_html, generate_styled_document

def run_extraction(base_url, state_code, headers, filters, output_base_path):
    """Main pipeline for scraping ZP, BP, and GP hierarchical MoM records."""
    print(f"Starting extraction pipeline. Saving to: {output_base_path}")
    os.makedirs(output_base_path, exist_ok=True)

    audit_log = [["District (ZP)", "Block (BP)", "Gram Panchayat (GP)", "File Name", "Status", "Reason / Notes"]]

    audit_data = {
        "run_date": datetime.now().isoformat(),
        "source_portal": base_url,
        "state_code": state_code,
        "filters": filters
    }
    with open(os.path.join(output_base_path, "metadata.json"), "w") as f:
        json.dump(audit_data, f, indent=4)

    try:
        zp_response = fetch_json(f"{base_url}/gp_report/{state_code}", headers=headers, params={"fin_year": filters["fin_year"]})
        if not zp_response or "data" not in zp_response:
            print("Failed to retrieve Zilla Panchayat data.")
            return

        for zp in zp_response["data"]:
            zp_code = zp.get("zp_lgd_code", "N/A")
            zp_name = zp.get("zp_name", f"ZP_{zp_code}").strip()
            print(f"\n--- Processing District: {zp_name} ({zp_code}) ---")

            bp_response = fetch_json(f"{base_url}/gp_report/{state_code}/{zp_code}", headers=headers, params=filters)
            if not bp_response or "data" not in bp_response:
                continue

            for bp in bp_response["data"]:
                bp_code = bp.get("bp_lgd_code", "N/A")
                bp_name = bp.get("bp_name", f"BP_{bp_code}").strip()
                print(f"  Fetching Block: {bp_name}")

                gp_response = fetch_json(f"{base_url}/gp_report/{state_code}/{zp_code}/{bp_code}", headers=headers, params=filters)
                if not gp_response or "data" not in gp_response:
                    continue

                for gp in gp_response["data"]:
                    gp_code = gp.get("local_body_code") or gp.get("gp_lgd_code") or gp.get("gp_code", "N/A")
                    gp_name = gp.get("gp_name", f"GP_{gp_code}").strip()

                    mom_params = {**filters, "local_body_code": gp_code}
                    mom_response = fetch_json(f"{base_url}/dashboard-minutes", headers=headers, params=mom_params)

                    if not mom_response or "meetings" not in mom_response or len(mom_response["meetings"]) == 0:
                        continue

                    print(f"    -> Found {len(mom_response['meetings'])} meetings for {gp_name}. Processing...")

                    # ==========================================
                    # 1. SAVE RAW JSON (One file per GP)
                    # ==========================================
                    json_target_dir = os.path.join(output_base_path, "JSON_Data", zp_name, bp_name)
                    os.makedirs(json_target_dir, exist_ok=True)
                    json_filename = f"{gp_name}_{gp_code}.json"
                    
                    try:
                        with open(os.path.join(json_target_dir, json_filename), "w", encoding="utf-8") as jfile:
                            json.dump(mom_response, jfile, indent=4, ensure_ascii=False)
                        audit_log.append([zp_name, bp_name, gp_name, json_filename, "DOWNLOADED", "RAW JSON DATA"])
                    except Exception as e:
                        print(f"      Error saving JSON {json_filename}: {e}")

                    # ==========================================
                    # 2. SAVE FORMATTED HTML (Multiple files per GP)
                    # ==========================================
                    html_target_dir = os.path.join(output_base_path, "Formatted_Reports", zp_name, bp_name, gp_name)
                    os.makedirs(html_target_dir, exist_ok=True)

                    for idx, meeting in enumerate(mom_response["meetings"]):
                        date = meeting.get("meeting_date", "YYYY-MM-DD")
                        original_title = meeting.get("meeting_title", "Untitled Meeting")
                        title_to_process = original_title

                        # --- TRANSLITERATION ENGINE ---
                        if re.search(r'[\u0B00-\u0B7F]', title_to_process):
                            try:
                                title_to_process = sanscript.transliterate(title_to_process, sanscript.ORIYA, sanscript.ITRANS).lower()
                            except Exception:
                                pass

                        # --- SANITIZATION ---
                        safe_title = "".join([c for c in title_to_process if c.isalpha() or c.isdigit() or c == ' ']).strip()

                        # --- TRUNCATION TRACKER ---
                        truncation_note = ""
                        if len(safe_title) > 150:
                            safe_title = safe_title[:150]
                            truncation_note = "[TITLE TRUNCATED]"
                            print(f"      [WARNING] Title too long. Truncated '{original_title[:15]}...' to '{safe_title}'")
                        elif not safe_title:
                            safe_title = "Untitled"

                        base_filename = f"{date}_{safe_title}_{idx}.html"

                        metadata = {
                            "zp_name": zp_name, "zp_code": zp_code,
                            "bp_name": bp_name, "bp_code": bp_code,
                            "gp_name": gp_name, "gp_code": gp_code,
                            "meeting_type": meeting.get("meeting_type", "Others"),
                            "title": original_title,
                            "date": date
                        }

                        clean_html = clean_minutes_html(meeting.get("minutes", ""))
                        styled_doc = generate_styled_document(metadata, clean_html)

                        status_note = truncation_note if truncation_note else "DOWNLOADED"
                        audit_log.append([zp_name, bp_name, gp_name, base_filename, "DOWNLOADED", status_note])

                        try:
                            with open(os.path.join(html_target_dir, base_filename), "w", encoding="utf-8") as file:
                                file.write(styled_doc)
                        except Exception as e:
                            print(f"      Error saving HTML {base_filename}: {e}")

    finally:
        csv_path = os.path.join(output_base_path, "Extraction_Audit_Report.csv")
        try:
            with open(csv_path, "w", newline="", encoding="utf-8") as csvfile:
                writer = csv.writer(csvfile)
                writer.writerows(audit_log)
            print(f"\n[SYSTEM] Run terminated. Audit Report saved to: {csv_path}")
        except Exception as e:
            print(f"\n[SYSTEM] Failed to save Audit Report: {e}")