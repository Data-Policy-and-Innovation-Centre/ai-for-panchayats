import os
import sys
import subprocess

# 1. Define absolute project locations
PROJECT_ROOT = "/home/dpico/ai-for-panchayats"
TARGET_DIR = os.path.join(PROJECT_ROOT, "src/ingest/meri_panchayat")
SRC_DIR = os.path.join(PROJECT_ROOT, "src")

# 2. Sequence of files to run
SCRIPTS_TO_RUN = [
    "village_population.py",
    "panchayat_funds.py",
    "panchayat_payment_register.py",
    "action_plans.py",
    "activity_summary.py",
    "beneficiaries.py",
    "work_activities.py"
]

def run_pipeline():
    print("==================================================")
    print("  Starting Meri Panchayat Ingestion Pipeline")
    print("==================================================")
    print(f"Project Root: {PROJECT_ROOT}\n")

    # 3. Build a dual-layer PYTHONPATH environment.
    # This forces Python to look in BOTH 'src' and the local folder,
    # resolving 'import config' and 'from ingest...' perfectly.
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{SRC_DIR}:{TARGET_DIR}:{env.get('PYTHONPATH', '')}"

    for script in SCRIPTS_TO_RUN:
        script_path = os.path.join(TARGET_DIR, script)
        
        if not os.path.exists(script_path):
            print(f"[WARNING] File not found: {script_path}. Skipping...")
            continue

        print(f"🚀 Running: {script}")
        try:
            # We keep the cwd as PROJECT_ROOT since your scripts/configs 
            # likely map paths relative to where you stand in the terminal.
            subprocess.run(
                [sys.executable, script_path],
                cwd=PROJECT_ROOT,
                env=env,
                check=True
            )
            print(f"✅ Successfully completed: {script}\n")
            
        except subprocess.CalledProcessError as e:
            print(f"\n❌ [CRITICAL ERROR] {script} failed with exit code {e.returncode}")
            print("Aborting pipeline immediately to prevent partial data state.")
            sys.exit(e.returncode)

    print("==================================================")
    print("  Pipeline Completed Successfully!")
    print("==================================================")

if __name__ == "__main__":
    run_pipeline()