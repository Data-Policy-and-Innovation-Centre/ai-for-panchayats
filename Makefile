.DEFAULT_GOAL  := help
.PHONY: help setup pull ingest publish-raw push run run-staging run-profile \
        run-profile-staging run-accounting run-accounting-staging run-expenditure run-expenditure-staging warehouse warehouse-staging sample exhibits deliver \
        box-paths status _require_mode_env
BOX_REMOTE      ?= box
BOX_PROJECT_ROOT ?= /2. Projects/11. PR&DW/AI for Panchayats
INCOMING_REMOTE ?= $(BOX_REMOTE):'$(BOX_PROJECT_ROOT)/Data/Raw/'
RAW_LOCAL       ?= data/raw/
EXHIBITS_REMOTE ?= $(BOX_REMOTE):'$(BOX_PROJECT_ROOT)/Analysis/Exhibits/'
EXHIBITS_LOCAL  ?= outputs/
-include .env
SHELL          := /bin/bash
.SHELLFLAGS    := -euo pipefail -c
USER_BIN       := $(HOME)/.local/bin
export PATH    := $(USER_BIN):$(PATH)

help:
	@echo ""
	@echo "  make setup           First-time setup on a new machine"
	@echo "  make pull            Get latest code, deps, and approved DVC data"
	@echo "  make ingest DATA=f.csv Copy an original source file from Box"
	@echo "  make run-profile      Publish+normalize the GP profile extract (#123)"
	@echo "  make run-accounting   Publish+normalize the accounting extract (#129)"
	@echo "  make run-expenditure  Publish+normalize the expenditure extract (#49)"
	@echo "  make publish-raw DATA=f.csv Copy a local raw file to Box"
	@echo "  make push DATA=f.csv Version and share a locally-ingested file via DVC"
	@echo "  make run             Publish + normalize the scraped tree (full state)"
	@echo "  make run-staging     Same, against a $(SAMPLE_GPS)-GP sample, into staging paths"
	@echo "  make warehouse SNAPSHOT_ID=x  Build the DuckDB and check conformance"
	@echo "  make warehouse-staging SNAPSHOT_ID=x  Same, from the staging registry"
	@echo "  make exhibits        Regenerate all figures and tables"
	@echo "  make deliver         Copy exhibits to Box without deleting remote files"
	@echo "  make box-paths       Show configured Box paths"
	@echo "  make status          Show what has changed"
	@echo ""

setup:
	perl -pi -e 's/\r$$//' scripts/setup.sh
	BOX_REMOTE="$(BOX_REMOTE)" bash scripts/setup.sh

pull: _check_git_clean
	@echo "[1/3] Pulling latest code..."
	@if git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}' >/dev/null 2>&1; then \
	  git pull --ff-only; \
	else \
	  echo "No Git upstream configured; skipping code pull."; \
	fi
	@echo "[2/3] Syncing Python environment..."
	uv sync
	@echo "[3/3] Pulling approved data versions..."
	uv run dvc pull
	@echo "Done."

ingest: _require_data
	@echo "Copying original $(DATA) from Box..."
	rclone copyto $(INCOMING_REMOTE)$(DATA) $(RAW_LOCAL)$(DATA) --progress
	@echo "Ingested $(DATA). The original Box file was not modified."

publish-raw: _require_data
	@echo "Publishing latest $(DATA) to Box..."
	rclone copyto $(RAW_LOCAL)$(DATA) $(INCOMING_REMOTE)$(DATA) --progress
	@echo "Published $(DATA). Existing Box files with other names were not deleted."

push: _require_data _check_git_clean
	@echo "[1/3] Versioning $(DATA) with DVC..."
	uv run dvc add $(RAW_LOCAL)$(DATA)
	@echo "[2/3] Committing version pointer..."
	git add $(RAW_LOCAL)$(DATA).dvc .gitignore
	git commit -m "data: update $(DATA) $$(date +%Y-%m-%d)"
	@echo "[3/3] Pushing to team remote..."
	uv run dvc repro
	uv run dvc push
	git push
	@echo "Done. Team can now pull $(DATA)."

# The warehouse pipeline. Two targets, not one, because approving a snapshot
# is deliberately a human act: `normalize` prints the config/snapshots.yaml
# stanza, a person pastes it, and that paste is the approval. See
# src/pipeline/snapshots.py, which refuses any entry that is not `approved`.
#
# MODE picks config/prod.env or config/staging.env. Every path differs between
# them, so a staging run cannot overwrite production's registry, Parquet tree
# or database. A sample still cannot be deployed whatever MODE says:
# scripts/build_snapshot_manifest.py refuses to pin anything short of the full
# 6,794-GP roster.
# Provenance stamped onto every raw run. Without these the CLI records its
# "unknown" defaults, normalization copies them into raw_manifest_identity,
# and two artifacts built from different code become indistinguishable --
# which defeats the point of an immutable, hash-verified run.
#
# CODE_SHA carries a -dirty suffix when the tree has uncommitted changes: a
# bare commit hash for a build that did not come from that commit is worse
# than "unknown", because it looks authoritative.
#
# `git status --porcelain` rather than `git diff --quiet HEAD` (#137): the
# latter only sees modifications to *tracked* files, so a new module that was
# never `git add`ed -- the most common way a working tree stops matching its
# commit -- stamped a clean sha. Porcelain also lists untracked files, and
# respects .gitignore, so throwaway worktrees and per-developer settings do
# not produce a false -dirty.
#
# Two ways this could quietly go back to lying, both closed here:
#
#   --untracked-files=normal is passed explicitly, because a developer with
#   status.showUntrackedFiles=no in their git config would otherwise get
#   empty porcelain output and a clean sha over an untracked module. What
#   provenance a build records must not depend on a personal git setting.
#
#   The exit status is checked, not just the output. `git status` failing --
#   an unreadable index, say -- also produces empty output, and treating
#   that as "clean" would be the worst possible reading. Anything other than
#   "succeeded and said nothing" is -dirty.
CODE_SHA    := $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)$(shell \
                 if status=$$(git status --porcelain --untracked-files=normal 2>/dev/null) \
                    && test -z "$$status"; then :; else echo -dirty; fi)
# The mode file is what decides every path this run reads and writes, so it
# is the configuration a rebuild would need to reproduce.
CONFIG_HASH  = $(shell shasum -a 256 $(MODE_ENV) 2>/dev/null | cut -c1-12 || echo unknown)
PIPELINE_SOURCE ?= egramSwaraj
PIPELINE_RAW_ROOT ?= data/raw
PIPELINE_TREE   ?= data/raw/eGramSwaraj_Data/Gram_Panchayat
RUN_ID          := $(or $(RUN_ID),$(shell date +%Y-%m-%d))
MODE            ?= prod
MODE_ENV        := config/$(MODE).env

# The mode files are Make-syntax KEY=value, exactly like .env above, so Make
# reads them directly and exports them to every child process. Shelling out to
# `python -c "load_settings()"` to recover the same paths would run uv twice
# per target to learn what this file already knows.
-include $(MODE_ENV)
export PANCHAYAT_CANONICAL_ROOT
export PIPELINE_SNAPSHOTS
export PANCHAYAT_DB_PATH
export PIPELINE_RAW_ROOT

# Ordered before every pipeline recipe, so a bad MODE fails with a sentence
# rather than with empty paths (`-include` is silent by design, since Make
# parses it before it can know whether the target even needs it).
_require_mode_env:
	@test -f $(MODE_ENV) || { echo "No such mode: $(MODE_ENV)"; exit 1; }
	@for v in PANCHAYAT_CANONICAL_ROOT PIPELINE_SNAPSHOTS PANCHAYAT_DB_PATH \
	          PIPELINE_RAW_ROOT; do \
	  eval "val=\$$$$v"; \
	  test -n "$$val" || { echo "$(MODE_ENV) does not set $$v"; exit 1; }; \
	done

# Stage 1-2: freeze the scraped tree as an immutable raw run, then normalize
# it to canonical Parquet. Ends by printing the registry stanza to paste.
run: _require_mode_env
	@echo "[$(MODE)] publishing raw run $(RUN_ID) from $(PIPELINE_TREE)..."
	uv run python main.py ingest \
	  --raw-root $(PIPELINE_RAW_ROOT) --source $(PIPELINE_SOURCE) --run-id $(RUN_ID) \
	  --code-sha $(CODE_SHA) --config-hash $(CONFIG_HASH) \
	  --payload-tree $(PIPELINE_TREE)
	@echo "[$(MODE)] verifying every published file against its hash..."
	uv run python main.py validate-run $(PIPELINE_RAW_ROOT)/$(PIPELINE_SOURCE)/$(RUN_ID)
	@echo "[$(MODE)] normalizing..."
	uv run python main.py normalize \
	  --run-path $(PIPELINE_RAW_ROOT)/$(PIPELINE_SOURCE)/$(RUN_ID) \
	  --output-root $(PANCHAYAT_CANONICAL_ROOT)
	@echo ""
	@echo "Next: paste the stanza above, then"
	@echo "  make warehouse MODE=$(MODE) SNAPSHOT_ID=<id>"

# SAMPLE_GPS makes the sample the target actually promises. It copies whole
# GP folders out of the downloaded tree, so the sample is the same bytes, the
# same layout and the same code path as the real thing -- just 1/1000th of it.
#
# Selected with a glob and an array slice rather than `ls | head`, which is a
# trap under the `pipefail` in .SHELLFLAGS above: on the real 6,794-folder
# tree `ls` writes more than a pipe buffer, `head` exits at 20, and `ls` dies
# of SIGPIPE with status 141 -- which pipefail promotes to a recipe failure,
# so `make sample` fails on exactly the tree it is meant for. A slice consumes
# the whole listing, so there is no early reader to signal. (Both sort the
# same way; the glob additionally skips stray non-directories, which `cp -R`
# would not have wanted anyway.)
SAMPLE_GPS  ?= 20
SAMPLE_TREE := data/interim/sample/Gram_Panchayat

sample:
	@test -d "$(PIPELINE_TREE)" || { \
	  echo "No scraped tree at $(PIPELINE_TREE); pull it from Box first."; exit 1; }
	@rm -rf $(SAMPLE_TREE) && mkdir -p $(SAMPLE_TREE)
	@shopt -s nullglob; gps=($(PIPELINE_TREE)/*/); \
	  for gp in "$${gps[@]:0:$(SAMPLE_GPS)}"; do cp -R "$${gp%/}" $(SAMPLE_TREE)/; done
	@echo "sample: $$(ls $(SAMPLE_TREE) | wc -l | tr -d ' ') GP folders in $(SAMPLE_TREE)"

run-staging: sample
	@$(MAKE) run MODE=staging PIPELINE_TREE=$(SAMPLE_TREE)

# The same two stages for the flat-CSV reference extract (#123), which is a
# separate source with its own run and its own snapshot.
#
# This exists because doing it by hand is a trap, not for convenience. The
# printed "paste this into ..." stanza names $(PIPELINE_SNAPSHOTS), which is
# only set because `-include $(MODE_ENV)` above exports it -- so a hand-typed
# `python main.py ingest` outside make prints the PRODUCTION registry path
# whatever mode you believe you are in, and following that hint parks a
# staging snapshot in the real registry. Going through make is what keeps the
# two modes apart.
PROFILE_SOURCE  ?= egramswaraj_profile
PROFILE_RUN_ID  ?= $(RUN_ID)
PROFILE_CSV     ?= data/raw/eGramSwaraj_Data/Panchayat_profile/eGramSwaraj_panchayat_master.csv
PROFILE_RUN     := $(PIPELINE_RAW_ROOT)/$(PROFILE_SOURCE)/$(PROFILE_RUN_ID)

run-profile: _require_mode_env
	@test -f "$(PROFILE_CSV)" || { \
	  echo "No profile extract at $(PROFILE_CSV); pull it with"; \
	  echo "  make ingest DATA='eGramSwaraj_Data/Panchayat_profile/$(notdir $(PROFILE_CSV))'"; \
	  exit 1; }
	@echo "[$(MODE)] publishing profile run $(PROFILE_RUN_ID) from $(PROFILE_CSV)..."
	uv run python main.py ingest \
	  --raw-root $(PIPELINE_RAW_ROOT) --source $(PROFILE_SOURCE) --run-id $(PROFILE_RUN_ID) \
	  --code-sha $(CODE_SHA) --config-hash $(CONFIG_HASH) \
	  --payload $(notdir $(PROFILE_CSV))=$(PROFILE_CSV)
	@echo "[$(MODE)] verifying the published file against its hash..."
	uv run python main.py validate-run $(PROFILE_RUN)
	@echo "[$(MODE)] normalizing..."
	uv run python main.py normalize --run-path $(PROFILE_RUN) \
	  --output-root $(PANCHAYAT_CANONICAL_ROOT)
	@echo ""
	@echo "Next: paste the stanza above, then build with BOTH snapshots --"
	@echo "  make warehouse MODE=$(MODE) SNAPSHOT_ID='<scrape-id> --snapshot-id <profile-id>'"

# The sample shares the whole 6,794-row extract on purpose: gp_profile rows
# for GPs outside the sample are quarantined as orphans, which is the path
# worth exercising rather than avoiding.
run-profile-staging:
	@$(MAKE) run-profile MODE=staging

# The same two stages for the nested accounting extract (#129), which fills
# `voucher`. A tree rather than one file, so it publishes with --payload-tree
# exactly as `run` does; going through make is what keeps prod and staging
# registries apart, for the reason spelled out above run-profile.
ACCOUNTING_SOURCE ?= egramswaraj_accounting
ACCOUNTING_RUN_ID ?= $(RUN_ID)
ACCOUNTING_TREE   ?= data/raw/eGramSwaraj_Data/Expenditure/Accounting_All_GPs
ACCOUNTING_RUN    := $(PIPELINE_RAW_ROOT)/$(ACCOUNTING_SOURCE)/$(ACCOUNTING_RUN_ID)

run-accounting: _require_mode_env
	@test -d "$(ACCOUNTING_TREE)" || { \
	  echo "No accounting tree at $(ACCOUNTING_TREE); pull it from Box with"; \
	  echo "  rclone copy \"$(INCOMING_REMOTE)eGramSwaraj_Data/Expenditure/Accounting_All_GPs\" \\"; \
	  echo "    $(ACCOUNTING_TREE) --transfers 16 --progress"; \
	  exit 1; }
	@echo "[$(MODE)] publishing accounting run $(ACCOUNTING_RUN_ID) from $(ACCOUNTING_TREE)..."
	uv run python main.py ingest \
	  --raw-root $(PIPELINE_RAW_ROOT) --source $(ACCOUNTING_SOURCE) --run-id $(ACCOUNTING_RUN_ID) \
	  --code-sha $(CODE_SHA) --config-hash $(CONFIG_HASH) \
	  --payload-tree $(ACCOUNTING_TREE)
	@echo "[$(MODE)] verifying every published file against its hash..."
	uv run python main.py validate-run $(ACCOUNTING_RUN)
	@echo "[$(MODE)] normalizing..."
	uv run python main.py normalize --run-path $(ACCOUNTING_RUN) \
	  --output-root $(PANCHAYAT_CANONICAL_ROOT)
	@echo ""
	@echo "Next: paste the stanza above, then build with ALL snapshots --"
	@echo "  make warehouse MODE=$(MODE) SNAPSHOT_ID='<scrape-id> --snapshot-id <profile-id> --snapshot-id <accounting-id>'"

run-accounting-staging:
	@$(MAKE) run-accounting MODE=staging

# The same two stages for the activity-wise expenditure extract (#49), which
# fills activity_expenditure and activity_voucher. One 770 MB CSV, so it
# publishes with --payload like run-profile rather than --payload-tree.
EXPENDITURE_SOURCE ?= egramswaraj_expenditure
EXPENDITURE_RUN_ID ?= $(RUN_ID)
EXPENDITURE_CSV    ?= data/raw/eGramSwaraj_Data/Expenditure/Activity_wise_Expenditure_all_GPs/expenditure_all.csv
EXPENDITURE_RUN    := $(PIPELINE_RAW_ROOT)/$(EXPENDITURE_SOURCE)/$(EXPENDITURE_RUN_ID)

run-expenditure: _require_mode_env
	@test -f "$(EXPENDITURE_CSV)" || { \
	  echo "No expenditure extract at $(EXPENDITURE_CSV); pull it from Box with"; \
	  echo "  make ingest DATA='eGramSwaraj_Data/Expenditure/Activity_wise_Expenditure_all_GPs/expenditure_all.csv'"; \
	  exit 1; }
	@echo "[$(MODE)] publishing expenditure run $(EXPENDITURE_RUN_ID) from $(EXPENDITURE_CSV)..."
	uv run python main.py ingest \
	  --raw-root $(PIPELINE_RAW_ROOT) --source $(EXPENDITURE_SOURCE) --run-id $(EXPENDITURE_RUN_ID) \
	  --code-sha $(CODE_SHA) --config-hash $(CONFIG_HASH) \
	  --payload $(notdir $(EXPENDITURE_CSV))=$(EXPENDITURE_CSV)
	@echo "[$(MODE)] verifying the published file against its hash..."
	uv run python main.py validate-run $(EXPENDITURE_RUN)
	@echo "[$(MODE)] normalizing..."
	uv run python main.py normalize --run-path $(EXPENDITURE_RUN) \
	  --output-root $(PANCHAYAT_CANONICAL_ROOT)
	@echo ""
	@echo "Next: paste the stanza above, then build with ALL snapshots."

run-expenditure-staging:
	@$(MAKE) run-expenditure MODE=staging

# Stage 3-4: build the warehouse from an approved snapshot, then check it.
#
# The build goes to a candidate path and is renamed over the real one only
# after conformance passes (#135). build_warehouse.py is itself atomic -- it
# loads into a temp file and os.replace()s the target -- but that replace
# happens before this recipe reaches the checker, so a build that loads
# cleanly and then fails conformance would otherwise have already overwritten
# the last good database. At full state that costs a ~90-minute rebuild, and
# it leaves a known-bad file sitting at the path every later command reads.
#
# A failed check leaves the candidate in place on purpose: it is the artifact
# you need to look at to find out why, and the previous database is still
# where it was. `mv` is a rename within one directory, so the promotion is
# atomic too.
CANDIDATE_DB := $(PANCHAYAT_DB_PATH).candidate

# Exempt the one total whose source is known to be short (#171), by name, so
# the other two are enforced on every full-state build. Before #175 this was a
# blanket --skip-reconciliation, which meant a defect that silently doubled
# activity_expenditure could reach a green build AND a green conformance run.
# Staging overrides it: a 20-GP sample cannot hit a full-state total at all.
RECONCILIATION ?= --exempt-reconciliation reconciliation.voucher_amount_total

warehouse: _require_mode_env
	@test -n "$(SNAPSHOT_ID)" || { \
	  echo "SNAPSHOT_ID is required: make warehouse SNAPSHOT_ID=<id>"; exit 1; }
	uv run python scripts/build_warehouse.py --database $(CANDIDATE_DB) \
	  build --snapshot-id $(SNAPSHOT_ID)
	@echo ""
	@echo "[$(MODE)] checking conformance..."
	@# Derived from the flags actually passed, never spelled a second time:
	@# a recipe that describes a build it is not running is how the note
	@# above this one came to claim activity_expenditure had no loader.
	@case "$(RECONCILIATION) $(CONFORMANCE_EXTRA)" in \
	  *--skip-reconciliation*) \
	    echo "  Reconciliation is OFF: a 20-GP sample cannot hit a full-state" ; \
	    echo "  total, so all three are skipped." ;; \
	  *) \
	    echo "  Reconciliation is ON for activity_expenditure and planned_cost," ; \
	    echo "  which match production exactly. Only voucher_amount_total is" ; \
	    echo "  exempted, because its extract covers 6,436 of 6,794 GPs (#171)" ; \
	    echo "  and #172 would move it even then -- and it is exempted by name," ; \
	    echo "  so the other two are still checked. Baselines are full-state as" ; \
	    echo "  of #175; #62 and #50 track what remains." ;; \
	esac
	@case "$(CONFORMANCE_EXTRA)" in \
	  *--skip-geography*) \
	    echo "  Geography coverage is skipped: this build is not the state." ;; \
	  *) \
	    echo "  Geography coverage is NOT skipped: it is what catches a sample" ; \
	    echo "  built as if it were the state." ;; \
	esac
	uv run python scripts/check_warehouse_conformance.py \
	  $(CANDIDATE_DB) $(RECONCILIATION) $(CONFORMANCE_EXTRA)
	@mv -f $(CANDIDATE_DB) $(PANCHAYAT_DB_PATH)
	@echo "[$(MODE)] promoted to $(PANCHAYAT_DB_PATH)"

# Staging skips geography as well: a 20-GP sample is not the state, and is
# not pretending to be.
warehouse-staging:
	@$(MAKE) warehouse MODE=staging SNAPSHOT_ID=$(SNAPSHOT_ID) \
	  RECONCILIATION=--skip-reconciliation CONFORMANCE_EXTRA=--skip-geography

exhibits:
	uv run dvc repro
	@echo "Pipeline complete. Check outputs/ for regenerated exhibits."

deliver:
	@echo "Delivering exhibits to Box..."
	rclone copy $(EXHIBITS_LOCAL) $(EXHIBITS_REMOTE) --progress
	@echo "Exhibits delivered. Existing Box files were not deleted."

box-paths:
	@echo "BOX_REMOTE=$(BOX_REMOTE)"
	@echo "BOX_PROJECT_ROOT=$(BOX_PROJECT_ROOT)"
	@echo "INCOMING_REMOTE=$(INCOMING_REMOTE)"
	@echo "EXHIBITS_REMOTE=$(EXHIBITS_REMOTE)"

status:
	@echo "=== Git ==="
	@git status --short
	@echo ""
	@echo "=== DVC (local) ==="
	@uv run dvc status
	@echo ""
	@echo "=== DVC (remote) ==="
	@uv run dvc status --cloud

_check_git_clean:
	@git diff --quiet && git diff --cached --quiet || \
	  (echo "Uncommitted changes. Commit or stash first." && exit 1)

_require_data:
	@test -n "$(DATA)" || \
	  (echo "Usage: make <ingest|push> DATA=yourfile.csv" && exit 1)

_check_prereqs:
	@command -v git    >/dev/null || (echo "Install Git: https://git-scm.com/downloads" && exit 1)
	@command -v uv     >/dev/null || (echo "Install uv: https://docs.astral.sh/uv/" && exit 1)
	@command -v rclone >/dev/null || (echo "Install rclone: https://rclone.org/install/" && exit 1)
	@command -v aws    >/dev/null || (echo "Install AWS CLI" && exit 1)
