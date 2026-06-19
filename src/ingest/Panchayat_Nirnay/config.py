# --- 1. THE PILOT CATALOG (20 Target GPs) ---
TARGET_GPS = [
    {"zp_name": "Khordha", "zp_id": 321, "bp_name": "Bhubaneswar", "bp_id": 3823, "gp_name": "Andhrua", "gp_code": 119598},
    {"zp_name": "Khordha", "zp_id": 321, "bp_name": "Bhubaneswar", "bp_id": 3823, "gp_name": "Barimunda", "gp_code": 119599},
    {"zp_name": "Khordha", "zp_id": 321, "bp_name": "Bhubaneswar", "bp_id": 3823, "gp_name": "Itipur", "gp_code": 119605},
    {"zp_name": "Cuttack", "zp_id": 309, "bp_name": "Baranga", "bp_id": 3699, "gp_name": "Dadhapatna", "gp_code": 116936},
    {"zp_name": "Cuttack", "zp_id": 309, "bp_name": "Tangi Choudwar", "bp_id": 3707, "gp_name": "Govindapur", "gp_code": 117153},
    {"zp_name": "Bargarh", "zp_id": 306, "bp_name": "Attabira", "bp_id": 3674, "gp_name": "Hirlipali", "gp_code": 116350},
    {"zp_name": "Bargarh", "zp_id": 306, "bp_name": "Barpali", "bp_id": 3676, "gp_name": "Bandhpali", "gp_code": 116397},
    {"zp_name": "Bargarh", "zp_id": 306, "bp_name": "Barpali", "bp_id": 3676, "gp_name": "Bhatigaon", "gp_code": 116400},
    {"zp_name": "Bargarh", "zp_id": 306, "bp_name": "Bheden", "bp_id": 3678, "gp_name": "Bheden", "gp_code": 116438},
    {"zp_name": "Ganjam", "zp_id": 313, "bp_name": "Sheragada", "bp_id": 3747, "gp_name": "Sharagada", "gp_code": 118012},
    {"zp_name": "Ganjam", "zp_id": 313, "bp_name": "Rangeilunda", "bp_id": 3745, "gp_name": "Mendarajpur", "gp_code": 275075},
    {"zp_name": "Ganjam", "zp_id": 313, "bp_name": "Khallikote", "bp_id": 3740, "gp_name": "Chikilli", "gp_code": 117835},
    {"zp_name": "Ganjam", "zp_id": 313, "bp_name": "Rangeilunda", "bp_id": 3745, "gp_name": "Biswamathpur", "gp_code": 117951},
    {"zp_name": "Koraput", "zp_id": 322, "bp_name": "Laxmipur", "bp_id": 3838, "gp_name": "Laxmipur", "gp_code": 119862},
    {"zp_name": "Koraput", "zp_id": 322, "bp_name": "Boipariguda", "bp_id": 3830, "gp_name": "Boipariguda", "gp_code": 119717},
    {"zp_name": "Kandhamal", "zp_id": 318, "bp_name": "Khajuripada", "bp_id": 3790, "gp_name": "Dutimendi", "gp_code": 118939},
    {"zp_name": "Rayagada", "zp_id": 329, "bp_name": "Kalyansingpur", "bp_id": 3914, "gp_name": "Kalyansinghpur", "gp_code": 121162},
    {"zp_name": "Malkangiri", "zp_id": 323, "bp_name": "Kalimela", "bp_id": 3843, "gp_name": "Kalimela", "gp_code": 119936},
    {"zp_name": "Sundargarh", "zp_id": 332, "bp_name": "Lahunipara", "bp_id": 3945, "gp_name": "Haldikudar", "gp_code": 121663},
    {"zp_name": "Sundargarh", "zp_id": 332, "bp_name": "Balisankara", "bp_id": 3936, "gp_name": "Karuabahal", "gp_code": 121526}
]

# --- 2. API HEADERS & SKELETON KEY ---
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Connection": "keep-alive",
    "Content-Type": "application/json",
    "Origin": "https://meetingonline.gov.in",
    "Referer": "https://meetingonline.gov.in/recent?tab=recent_meeting",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36",
    "accesskey": "541C469D-4E5E-4A72-8F84-DB9AF490F362",
    "device-ip": "117.239.20.25",
    "uuid": "cc8e6f63-3e9b-4320-bfcd-738e5f9f98bb",
    
    # !!! UPDATE THESE TWO WITH FRESH VALUES IF THE SCRIPT HALTS !!!
    "secretkey": "b2d238d1131fbfe834a9fad7d4e37df802a15bac31dcda0736fde51873c11c6a",
    "timestamp": "19062026070010"
}

# --- 3. API ENDPOINTS ---
MEETINGS_LIST_API = "https://meetingonline.gov.in/gsn_api/live/gsn_get_recent/v1/meetings"
MEETING_DETAILS_API = "https://meetingonline.gov.in/gsn_api/live/meetings/v2/get_meeting_summary"
DOWNLOAD_BASE = "https://meetingonline.gov.in/gsn_api/live/nrega/v1/download_file/meetings"