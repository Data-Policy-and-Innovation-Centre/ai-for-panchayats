"""eGramSwaraj GP JSON -> relational CSV flattener (AA, PL, PP, RE, TA)."""

from .config import (BATCH_SIZE, INPUT_DIR, KINDS, MASTER_MAX_ROWS, OUTPUT_DIR,
                     configure_logging)
from .extractor import build_master, flatten_file, process

__all__ = [
    "BATCH_SIZE",
    "INPUT_DIR",
    "KINDS",
    "MASTER_MAX_ROWS",
    "OUTPUT_DIR",
    "build_master",
    "configure_logging",
    "flatten_file",
    "process",
]