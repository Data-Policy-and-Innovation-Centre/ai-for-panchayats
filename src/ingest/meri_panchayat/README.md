# overview
# Meri Panchayat Data Extraction Pipeline 
- This is a scrapping pileline and it collects information from the government's Meri Panchayat website. It gathers details about village funds, development plans, money spent on projects, and village populations.



# 📂 Project Folder Structure

ai-for-panchayats/
├── scripts/
│   └── main_meri_panchayat.py       # Master Orchestrator (The ONLY file you execute)
│
├── src/
│   └── ingest/
│       └── meri_panchayat/
│           ├── config.yaml                   # Master settings dashboard (API keys, States, Years)
│           ├── config.py                     # Converts settings into active Python code & headers
│           ├── base_scraper.py               # Core connection engine (Fires network requests)
│           ├── village_population.py         # Demographic data worker
│           ├── panchayat_payment_register.py # Financial ledger transaction worker
│           ├── action_plans.py               # Planned allocation worker
│           ├── activity_summary.py           # Public infrastructure summary worker
│           ├── beneficiaries.py              # Social scheme impact worker
│           └── work_activities.py            # Deep nested project milestones worker
│
└── data/
    └── raw/
        └── meri_panchayat/          # 📂 All collected data saves here automatically!

# Core & Setup Files
- 1. config.yaml: 
* The settings dashboard. This is where you change target states, add financial years, or swap secure API tokens without touching code. 

#### 2. `config.py`
* The bridge. It automatically converts your YAML settings into active Python variables and creates data handshakes (headers) for the government servers.

#### 3. `base_scraper.py`
* base_scraper.py: The internet connector. It processes all requests and map-queries (Districts, Blocks, Gram Panchayats) safely. If an API stumbles, this script prevents a total crash.

#### 4. `main_meri_panchayat.py`
* This is the only file you need to execute. It launches all individual scrapers back-to-back in the perfect sequence.
* use this code to execute all the files : 
# python scripts/main_meri_panchayat.py
# python src/ingest/meri_panchayat/action_plans.py


# Scrapers
#### 5. `village_population.py`
* village_population.py: Counts local populations and logs structural directory codes.

#### 6. `panchayat_payment_register.py`
* **What it does:** Tracks the money trail. It extracts ledger transaction details, tracking e-Payment Orders (EPOs), bank statuses, voucher dates, and payment modes.

#### 7. `action_plans.py`
* **What it does:** Captures future planning. It extracts development goals, planned budgets, actual funds received, and links to citizen-approved layout PDFs. 


#### 8. `activity_summary.py`
* **What it does:** Tallies local progress. It aggregates asset counts and categorizes public works to give a high-level summary of systemic structural infrastructure investments.

#### 9. `beneficiaries.py`
* **What it does:**  It records social numbers, tracking how many local citizens benefit from welfare programs. 

#### 10. `work_activities.py`
* **What it does:** The most detailed tracker. It unpacks deeply nested project metrics, breaking down expected vs. actual costs, completion statuses, unit metrics, and physical asset stages. 

# How to run the full pipeline:
* python scripts/main_meri_panchayat.py
