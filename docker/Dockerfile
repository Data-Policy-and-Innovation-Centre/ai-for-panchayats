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

# What this image is, recorded in the image itself (#85). Nothing served by a
# running task revealed which consumer commit, image tag or snapshot version it
# was serving, so one deployment could not be told from the next by observing
# the app -- which is exactly what post-deploy verification has to do.
#
# Declared as ARGs with no default: an unset build arg would bake the string
# "unavailable" into a real image, and a label that lies is worse than one that
# is absent. build.sh always passes all three.
ARG REPO_COMMIT
ARG CONSUMER_COMMIT
ARG IMAGE_TAG

# Declared here, CONSUMED at the bottom of the file. An ARG declaration does
# not invalidate anything; the first instruction that expands one does, and
# every layer below it with it. Labelling here would put a value that changes
# on every commit above `pip install`, so each build would reinstall every
# pinned package to produce a byte-identical layer.

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

# --- provenance (#85) -------------------------------------------------------
# Last, deliberately. Everything above is identical between two commits that
# change no build input, so it stays cached; only these two layers rebuild.
#
# The same three values twice over, because they are read two different ways:
# `docker inspect` reads labels but needs a registry round trip, and a running
# task has to answer over HTTP without one.
#
# printf rather than a heredoc, so the layer does not depend on the shell's
# heredoc handling, and every value is quoted so an unset arg yields empty
# JSON strings rather than malformed JSON. Written after USER app, into a
# directory that user already owns, so it needs no further chown.
RUN printf '{\n  "repo_commit": "%s",\n  "consumer_commit": "%s",\n  "image_tag": "%s"\n}\n' \
      "$REPO_COMMIT" "$CONSUMER_COMMIT" "$IMAGE_TAG" > /app/BUILD_INFO

LABEL org.opencontainers.image.revision="$REPO_COMMIT" \
      org.opencontainers.image.version="$IMAGE_TAG" \
      org.opencontainers.image.source="https://github.com/Data-Policy-and-Innovation-Centre/ai-for-panchayats" \
      in.dpic.panchayats.consumer-commit="$CONSUMER_COMMIT" \
      in.dpic.panchayats.image-tag="$IMAGE_TAG"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
