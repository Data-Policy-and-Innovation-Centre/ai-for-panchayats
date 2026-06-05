"""Project-wide directories, settings, and logging helpers."""

import os
from pathlib import Path

from loguru import logger
from pydantic import ConfigDict
from pydantic_settings import BaseSettings
from tqdm import tqdm


class Directories:
    """Directories used by the project."""

    ROOT_DIR = Path(__file__).resolve().parent

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

    def __init__(self) -> None:
        for directory in [
            self.SRC,
            self.SCRIPTS,
            self.NOTEBOOKS,
            self.TESTS,
            self.DATA,
            self.RAW_DATA,
            self.INTERIM_DATA,
            self.PROCESSED_DATA,
            self.EXTERNAL_DATA,
            self.OUTPUTS,
            self.FIGURES,
            self.REPORTS,
            self.TABLES,
            self.LOGS,
        ]:
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


settings = Settings()


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


