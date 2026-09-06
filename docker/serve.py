"""ASGI entry point: the consumer's API plus the dashboard on one origin.

The dashboard is mounted last and at the root, so every API route registered by
`main` is matched first and only unmatched paths fall through to static files.
`html=True` serves index.html for client-side routes.

This wrapper exists so the consumer repository needs no modification to be
deployed here.

`/deployment.json` is registered BEFORE that mount, so it is matched as a route
rather than looked up as a static file and served index.html (#85). It is the
only way to ask a running task what it is: which consumer commit, which image
tag, and which snapshot version it verified at startup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from main import app

# uvicorn runs from /app/Ask, so /app is not on the path and `src.deploy` would
# not resolve. Same convention as scripts/fetch_snapshot.py, which appends its
# own repository root for the same reason.
_APP_ROOT = os.environ.get("APP_ROOT", "/app")
if _APP_ROOT not in sys.path:
    sys.path.append(_APP_ROOT)

from src.deploy.identity import deployment_payload  # noqa: E402

_BUILD_INFO = Path(os.environ.get("BUILD_INFO_PATH", "/app/BUILD_INFO"))
_IDENTITY = Path(os.environ.get("SNAPSHOT_IDENTITY_PATH", "/var/snapshot/identity.json"))


@app.get("/deployment.json")
def deployment() -> dict:
    """What this task is running, for post-deploy verification.

    Read on every request rather than cached at import: the identity file is
    written by the entrypoint before uvicorn starts, but a `docker run` used
    for development may produce it later or not at all, and a stale cached
    "unavailable" would be indistinguishable from a real one.
    """

    return deployment_payload(_BUILD_INFO, _IDENTITY)


_static = Path(os.environ.get("STATIC_DIR", "/app/static"))
if _static.is_dir():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="dashboard")

__all__ = ["app"]
