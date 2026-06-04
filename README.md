# ai-for-panchayats

This repository supports DPIC's work with the Panchayati Raj & Drinking Water
Department, Government of Odisha, on using data science and AI to strengthen
Gram Panchayat capacity. The project focuses on understanding citizen needs
from Panchayat-level records, mapping schemes and public data systems, and
building simple analytics that can reduce administrative burden and support
GP-level planning and decision-making.

Panchayats sit at the center of last-mile governance: citizens voice needs
through Gram Sabhas and local channels, GP leaders translate those needs into
plans and scheme actions, and government systems deliver public works, services,
and welfare benefits. A major workstream for this repository is therefore to
inventory relevant portals, collect source data reproducibly, and keep raw
extracts shareable with the team through Box.

## Setup

Run:

```bash
make setup
```

Do not run setup with `sudo`, including on WSL. The setup script installs
missing user-level tools into `~/.local/bin` where possible and adds that
directory to your shell path.

On WSL, clone this repository inside the Linux filesystem, such as
`~/Documents/GitHub/ai-for-panchayats`, not under `/mnt/c/...`. Creating the
Python virtual environment on the Windows-mounted filesystem can fail with
permission errors.

The Makefile defaults to the shared project Box path, but Box may expose
folders differently depending on a person's access. Check the configured paths:

```bash
make box-paths
```

If your Box path differs, override the project root when running a command:

```bash
make ingest DATA=survey_dump.csv BOX_PROJECT_ROOT="/All Files/AI for Panchayats"
```

You can also put the override in your shell profile:

```bash
export BOX_PROJECT_ROOT="/All Files/AI for Panchayats"
```

If only one endpoint differs, override the full rclone remote instead:

```bash
make deliver EXHIBITS_REMOTE="box:'/Shared/AI for Panchayats/Analysis/Exhibits/'"
```

Import a stakeholder-provided original from the Box incoming folder, then
record an approved version through DVC:

```bash
make ingest DATA=survey_dump.csv
make push DATA=survey_dump.csv
```

Publish a local raw file, such as an API pull, to the Box incoming folder:

```bash
make publish-raw DATA=api_dump.csv
```

## Scraper Notebooks And Raw Data

Use `notebooks/` for notebooks that scrape or download data from government
portals and other source systems. Each notebook should make the source portal,
filters, run date, and output dataset name clear enough for another team member
to rerun or audit the extract.

Ad hoc notebooks are acceptable for discovery, one-off pulls, and validating a
new source. Once a scrape, transform, or analysis step becomes recurring or
needs to be reused by other notebooks, refactor the shared logic into source
code and keep the notebook as a thin runner or demonstration.

Track portal coverage in the
[AI for Panchayats data tracker](https://docs.google.com/spreadsheets/d/1eKl2ChOG1QK7tDIJZG75VvjdylEEDhhROhnSsO7-Sto/edit?gid=0#gid=0).
When adding or updating a scraper notebook, update the tracker with the portal,
dataset name, owner, status, and any access notes.

Scraped data should be written locally under `data/raw/<dataset_name>/`. Do not
commit those raw files to Git. After validating the extract, publish it to the
shared Box raw-data folder:

```bash
make publish-raw DATA=<dataset_name>
```

For example, a notebook that creates `data/raw/gpdp_portal_extract/` should be
published with:

```bash
make publish-raw DATA=gpdp_portal_extract
```

Restore approved project data from DVC:

```bash
make pull
```

Define project-specific processing stages in `dvc.yaml`, then run:

```bash
make run
```

Publish generated figures, tables, and reports to Box without deleting existing
remote files:

```bash
make deliver
```

## Contributing

Keep pull requests small enough for a reviewer to understand in one sitting.
Separate unrelated changes into separate PRs, especially when data, analysis
logic, and report formatting change independently.

Interns should work on feature-based branches, for example
`feature/add-gpdp-scraper` or `analysis/ortps-topic-modeling`. Open pull
requests against `dev`, not `main`; `main` is reserved for reviewed,
release-ready work.

Before opening a PR, run:

```bash
uv run ruff check .
uv run pytest
```

Use Ruff for Python linting. Prefer small, explicit functions and project-local
helpers over one-off notebook-only logic when code will be reused.

If you work with notebooks, install the output-stripping hook once:

```bash
uv run nbstripout --install
```

Commit notebooks only after outputs have been stripped. Do not commit large
rendered notebook outputs, temporary exports, or local execution artifacts.

Data files under `data/` are proprietary by default. Do not commit raw,
interim, processed, or output data directly to Git. Use DVC for approved data
versions and `make deliver` for stakeholder-facing Box delivery.

Use the [pull request template](.github/PULL_REQUEST_TEMPLATE.md) when opening
a PR.

CI expects repository secret `DPIC_GITHUB_SSH_KEY` when private `dpic`
dependency resolution is required.
