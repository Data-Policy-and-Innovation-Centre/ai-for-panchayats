# Agent Notes

## Data Access Restriction
- Treat everything under `data/` as proprietary and sensitive.
- Do not list, read, search, preview, summarize, inspect metadata for, or otherwise access files or directories under `data/`.
- Do not run commands that recurse into `data/`, including broad repository searches or listings, unless they explicitly exclude `data/`.
- Only access a specific path under `data/` when the user gives explicit permission for that specific task and path.

## Project Shape
- This is a Python project managed by `uv`; use `pyproject.toml` and `uv.lock` as the dependency source of truth in generated projects.
- Python is pinned via `.python-version`.
- The local entrypoint is `main.py`; it is not packaged as a console script.

## Commands
- Install/sync dependencies with `uv sync`.
- Run the app with `uv run python main.py`.
- Lint with `uv run ruff check .`.
- Run tests with `uv run pytest`.

## Dependencies And Data
- `dpic` is pulled from the private Git source configured in `pyproject.toml`; dependency resolution needs GitHub SSH access.
- DVC remote settings live in `.dvc/config`; `dvc pull` and `dvc push` require suitable AWS/S3 credentials.
- Box synchronization settings live in `Makefile`; rclone operations require a configured Box remote.
