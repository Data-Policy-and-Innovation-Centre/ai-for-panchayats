# Odisha PR&DW chatbot: FastAPI backend plus the built dashboard, served from
# one origin so a tester needs a single URL.
#
# The database is not baked in. At startup the entrypoint downloads the
# version-pinned DuckDB snapshot from private S3, verifies its object version,
# byte count and SHA-256, checks the aggregate gate, and only then publishes it
# to task-local storage. A task that cannot prove its database never serves.
# Pinned by digest, not by tag (#84). `python:3.13-slim` is a moving target:
# it advances on every patch release and every Debian base rebuild, so the same
# commit built a week apart started from different bytes. That is the largest
# unpinned input in the image -- larger than any single package -- and it is
# invisible, because the tag string never changes.
#
# This digest is a manifest LIST covering amd64 and arm64, so it pins both
# platforms build.sh supports rather than nailing the image to one.
#
# The cost is deliberate: security patches in the base now require a conscious
# bump here instead of arriving on the next rebuild. Bump it, rebuild, deploy,
# and let the deployment prove it -- the same rule the Python pins follow.
FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SNAPSHOT_PATH=/var/snapshot/database.duckdb \
    DB_ENGINE=duckdb_file

WORKDIR /app

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
# Checked with the interpreter that is already here, not with curl. curl was
# the image's ONLY apt package, and `apt-get install curl` is unversioned: it
# resolves against whatever Debian publishes that day, so it floated free of
# the pinned base digest and made "every input is pinned" untrue (#84 review).
# urllib is in the standard library, so dropping the package removes the last
# floating input rather than merely documenting it.
#
# Note this healthcheck is inert in production: Fargate reads the ECS task
# definition's healthCheck, not the image's, and service.tf defines none. It
# is here for `docker run` during development, which is exactly why it is not
# worth an unpinned package.
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
  CMD python -c "import urllib.request as u, sys; sys.exit(0 if u.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)" 2>/dev/null || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
