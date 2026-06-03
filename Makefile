.DEFAULT_GOAL  := help
INCOMING_REMOTE := box:'/2. Projects/11. PR&DW/AI for Panchayats/Data/Raw'
RAW_LOCAL       := data/raw/
EXHIBITS_REMOTE := box:'/2. Projects/11. PR&DW/AI for Panchayats/Analysis/Exhibits'
EXHIBITS_LOCAL  := outputs/
SHELL          := /bin/bash
.SHELLFLAGS    := -euo pipefail -c

help:
	@echo ""
	@echo "  make setup           First-time setup on a new machine"
	@echo "  make pull            Get latest code, deps, and approved DVC data"
	@echo "  make ingest DATA=f.csv Copy an original source file from Box"
	@echo "  make publish-raw DATA=f.csv Copy a local raw file to Box"
	@echo "  make push DATA=f.csv Version and share a locally-ingested file via DVC"
	@echo "  make run             Run the analytics pipeline"
	@echo "  make exhibits        Regenerate all figures and tables"
	@echo "  make deliver         Copy exhibits to Box without deleting remote files"
	@echo "  make status          Show what has changed"
	@echo ""

setup: _check_prereqs
	bash scripts/setup.sh

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
	dvc pull
	@echo "Done."

ingest: _require_data
	@echo "Copying original $(DATA) from Box..."
	rclone copy $(INCOMING_REMOTE)$(DATA) $(RAW_LOCAL) --progress
	@echo "Ingested $(DATA). The original Box file was not modified."

publish-raw: _require_data
	@echo "Publishing latest $(DATA) to Box..."
	rclone copy $(RAW_LOCAL)$(DATA) $(INCOMING_REMOTE) --progress
	@echo "Published $(DATA). Existing Box files with other names were not deleted."

push: _require_data _check_git_clean
	@echo "[1/3] Versioning $(DATA) with DVC..."
	dvc add $(RAW_LOCAL)$(DATA)
	@echo "[2/3] Committing version pointer..."
	git add $(RAW_LOCAL)$(DATA).dvc .gitignore
	git commit -m "data: update $(DATA) $$(date +%Y-%m-%d)"
	@echo "[3/3] Pushing to team remote..."
	dvc push
	git push
	@echo "Done. Team can now pull $(DATA)."

run:
	source .venv/bin/activate && dvc repro
	@echo "Pipeline complete. Check outputs/."

exhibits:
	source .venv/bin/activate && dvc repro
	@echo "Pipeline complete. Check outputs/ for regenerated exhibits."

deliver:
	@echo "Delivering exhibits to Box..."
	rclone copy $(EXHIBITS_LOCAL) $(EXHIBITS_REMOTE) --progress
	@echo "Exhibits delivered. Existing Box files were not deleted."

status:
	@echo "=== Git ==="
	@git status --short
	@echo ""
	@echo "=== DVC (local) ==="
	@dvc status
	@echo ""
	@echo "=== DVC (remote) ==="
	@dvc status --cloud

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
