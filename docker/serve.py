"""ASGI entry point: the consumer's API plus the dashboard on one origin.

The dashboard is mounted last and at the root, so every API route registered by
`main` is matched first and only unmatched paths fall through to static files.
`html=True` serves index.html for client-side routes.

This wrapper exists so the consumer repository needs no modification to be
deployed here.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from main import app

_static = Path(os.environ.get("STATIC_DIR", "/app/static"))
if _static.is_dir():
    app.mount("/", StaticFiles(directory=str(_static), html=True), name="dashboard")

__all__ = ["app"]
