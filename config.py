"""Project-wide directories, settings, and logging helpers."""

import os
from pathlib import Path

from loguru import logger
from pydantic import ConfigDict
from pydantic_settings import BaseSettings
from tqdm import tqdm


class Directories:
    """Directories used by the project."""

    # ``absolute`` normalizes the usual ``__file__`` value without resolving
    # symlinks or consulting the filesystem.
    ROOT_DIR = Path(__file__).absolute().parent

    SRC = ROOT_DIR / "src"
    SCRIPTS = ROOT_DIR / "scripts"
    NOTEBOOKS = ROOT_DIR / "notebooks"
    TESTS = ROOT_DIR / "tests"

    DATA = ROOT_DIR / "data"
    RAW_DATA = DATA / "raw"
    INTERIM_DATA = DATA / "interim"
    PROCESSED_DATA = DATA / "processed"
    EXTERNAL_DATA = DATA / "external"

    OUTPUTS = ROOT_DIR / "outputs"
    FIGURES = OUTPUTS / "figures"
    REPORTS = OUTPUTS / "reports"
    TABLES = OUTPUTS / "tables"
    LOGS = ROOT_DIR / "logs"

    _DIRECTORY_ATTRIBUTES = (
        "SRC",
        "SCRIPTS",
        "NOTEBOOKS",
        "TESTS",
        "DATA",
        "RAW_DATA",
        "INTERIM_DATA",
        "PROCESSED_DATA",
        "EXTERNAL_DATA",
        "OUTPUTS",
        "FIGURES",
        "REPORTS",
        "TABLES",
        "LOGS",
    )

    def create_directories(self) -> None:
        """Create the project directories when a caller explicitly opts in."""
        for attribute in self._DIRECTORY_ATTRIBUTES:
            directory = getattr(self, attribute)
            directory.mkdir(parents=True, exist_ok=True)


directories = Directories()


class Settings(BaseSettings):
    """Settings loaded from environment variables and the project `.env` file."""

    ENV: str = os.getenv("ENV", "local")
    DEBUG: bool = os.getenv("DEBUG", "True").lower() in ("true", "1", "yes")

    DB_URL: str | None = os.getenv(
        "DB_URL"
    )
    DB_PASSWORD: str | None = os.getenv("DB_PASSWORD")

    AWS_ACCESS_KEY_ID: str | None = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY: str | None = os.getenv("AWS_SECRET_ACCESS_KEY")
    AWS_REGION: str = os.getenv("AWS_REGION", "ap-south-1")
    AWS_S3_BUCKET_NAME: str | None = os.getenv("AWS_S3_BUCKET_NAME")

    model_config = ConfigDict(env_file=directories.ROOT_DIR / ".env")


# Keep the historical module-level ``settings`` object without probing a
# repository .env file while importing this module. Passing the already
# normalized DEBUG value also preserves the old defaulting behavior when an
# unrelated environment uses a non-boolean DEBUG label such as "release".
settings = Settings(
    _env_file=None,
    DEBUG=os.getenv("DEBUG", "True").lower() in ("true", "1", "yes"),
)


def stop_logging_to_console(
    filename: str | Path = directories.LOGS / "main.log",
    mode: str = "a",
) -> None:
    """Stop console logging and write log messages to a file."""
    for handler_id in list(logger._core.handlers.keys()):
        logger.remove(handler_id)

    logger.add(
        filename,
        format="{file}:{function}:{line} {time} {level} {message}",
        level="INFO",
        colorize=True,
        catch=True,
        mode=mode,
    )


def resume_logging_to_console() -> None:
    """Resume console logging using tqdm-safe writes."""
    logger.add(lambda msg: tqdm.write(msg, end=""), colorize=True)
