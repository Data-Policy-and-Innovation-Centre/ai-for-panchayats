"""Root configuration must describe paths without touching the filesystem."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from config import Directories


def test_directories_construction_does_not_touch_filesystem(monkeypatch):
    calls: list[str] = []

    def forbidden(*args, **kwargs):
        calls.append("filesystem")
        raise AssertionError("Directories construction performed filesystem IO")

    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(Path, "stat", forbidden)
    monkeypatch.setattr(Path, "exists", forbidden)
    monkeypatch.setattr(Path, "iterdir", forbidden)

    directories = Directories()

    assert directories.RAW_DATA == directories.DATA / "raw"
    assert calls == []


def test_importing_config_performs_no_filesystem_io():
    """Import in a fresh interpreter, before config can be cached by pytest."""
    script = """
from pathlib import Path

def forbidden(*args, **kwargs):
    raise AssertionError("config import performed filesystem IO")

for name in (
    "mkdir", "stat", "resolve", "exists", "iterdir", "is_file", "is_dir"
):
    setattr(Path, name, forbidden)

import config

assert config.directories.RAW_DATA == config.directories.DATA / "raw"
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_directory_creation_is_explicit_opt_in(monkeypatch, tmp_path: Path):
    attributes = Directories._DIRECTORY_ATTRIBUTES
    for index, attribute in enumerate(attributes):
        monkeypatch.setattr(Directories, attribute, tmp_path / str(index))

    directories = Directories()
    directories.create_directories()

    assert all((tmp_path / str(index)).is_dir() for index in range(len(attributes)))
