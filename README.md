# ai-for-panchayats



## Setup

Run:

```bash
make setup
```

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
