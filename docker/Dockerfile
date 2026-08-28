# Odisha PR&DW chatbot: FastAPI backend plus the built dashboard, served from
# one origin so a tester needs a single URL.
#
# The database is not baked in. At startup the entrypoint downloads the
# version-pinned DuckDB snapshot from private S3, verifies its object version,
# byte count and SHA-256, checks the aggregate gate, and only then publishes it
# to task-local storage. A task that cannot prove its database never serves.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SNAPSHOT_PATH=/var/snapshot/database.duckdb \
    DB_ENGINE=duckdb_file

WORKDIR /app

# curl is used by the container healthcheck only.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY docker/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Snapshot retrieval: our package, its CLI, and the committed manifest.
COPY src/deploy /app/src/deploy
COPY scripts/fetch_snapshot.py /app/scripts/fetch_snapshot.py
COPY infra/snapshots/full_state.json /app/manifest/full_state.json

# The consumer application and the built dashboard.
COPY vendor/Ask /app/Ask
COPY vendor/static /app/static
COPY docker/serve.py /app/Ask/serve.py
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && mkdir -p /var/snapshot \
 && useradd --create-home --uid 10001 app \
 && chown -R app:app /var/snapshot /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
