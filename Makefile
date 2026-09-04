.DEFAULT_GOAL  := help
.PHONY: help setup pull ingest publish-raw push run run-staging warehouse \
        warehouse-staging sample exhibits deliver box-paths status _require_mode_env
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
CODE_SHA    := $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo unknown)$(shell \
                 test -z "$$(git status --porcelain 2>/dev/null)" || echo -dirty)
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

warehouse: _require_mode_env
	@test -n "$(SNAPSHOT_ID)" || { \
	  echo "SNAPSHOT_ID is required: make warehouse SNAPSHOT_ID=<id>"; exit 1; }
	uv run python scripts/build_warehouse.py --database $(CANDIDATE_DB) \
	  build --snapshot-id $(SNAPSHOT_ID)
	@echo ""
	@echo "[$(MODE)] checking conformance..."
	@echo "  --skip-reconciliation: voucher/dim_code have no loader yet (#46, #48,"
	@echo "  #129), so the published totals cannot be hit. The build is provisional"
	@echo "  until they do -- see #50. Geography coverage is NOT skipped here: it"
	@echo "  is what catches a sample built as if it were the state."
	uv run python scripts/check_warehouse_conformance.py \
	  $(CANDIDATE_DB) --skip-reconciliation $(CONFORMANCE_EXTRA)
	@mv -f $(CANDIDATE_DB) $(PANCHAYAT_DB_PATH)
	@echo "[$(MODE)] promoted to $(PANCHAYAT_DB_PATH)"

# Staging skips geography as well: a 20-GP sample is not the state, and is
# not pretending to be.
warehouse-staging:
	@$(MAKE) warehouse MODE=staging SNAPSHOT_ID=$(SNAPSHOT_ID) \
	  CONFORMANCE_EXTRA=--skip-geography

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
