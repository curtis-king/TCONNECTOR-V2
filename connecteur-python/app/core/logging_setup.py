"""Configuration des logs (extrait de main.py)."""
import logging
import os
from logging.handlers import RotatingFileHandler

from app.core.paths import data_dir

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
MAX_LOG_SIZE = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3


def setup_logging(log_level="info", log_dir=None):
    if log_dir is None:
        log_dir = os.path.join(data_dir())
    os.makedirs(log_dir, exist_ok=True)

    numeric_level = getattr(logging, str(log_level).upper(), logging.INFO)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "output.log"),
        maxBytes=MAX_LOG_SIZE,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )

    stream_handler = logging.StreamHandler()
    logging.basicConfig(
        level=numeric_level,
        format=LOG_FORMAT,
        handlers=[file_handler, stream_handler],
    )

    error_handler = RotatingFileHandler(
        os.path.join(log_dir, "error.log"),
        maxBytes=MAX_LOG_SIZE,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(error_handler)
