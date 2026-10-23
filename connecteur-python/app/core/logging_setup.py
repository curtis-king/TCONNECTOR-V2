"""Configuration des logs (extrait de main.py)."""
import logging
import os
import threading
import time
from logging.handlers import RotatingFileHandler

from app.core.paths import data_dir

LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
# 20 Mo x 5 : couvre une journee d'exploitation sans saturer la rotation
# (proposition 09, incident du 2026-10-21).
MAX_LOG_SIZE = 20 * 1024 * 1024
LOG_BACKUP_COUNT = 5
DEDUP_WINDOW_S = 60

_configured = False
_configured_lock = threading.Lock()


class DedupFilter(logging.Filter):
    """N'emet un message identique qu'une fois toutes les `window_s` secondes."""

    def __init__(self, window_s=DEDUP_WINDOW_S):
        super().__init__()
        self.window_s = max(0, int(window_s))
        self._seen = {}
        self._lock = threading.Lock()

    def filter(self, record):
        if self.window_s <= 0:
            return True
        try:
            message = record.getMessage()[:200]
        except Exception:  # noqa: BLE001 - un log ne doit jamais lever
            message = str(record.msg)[:200]
        key = (record.name, record.levelno, message)
        now = time.time()
        with self._lock:
            last = self._seen.get(key, 0)
            if now - last < self.window_s:
                return False
            self._seen[key] = now
            if len(self._seen) > 5000:
                cutoff = now - self.window_s
                self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
            return True


def setup_logging(log_level="info", log_dir=None):
    """Configure output.log et error.log (idempotent sur un meme process)."""
    global _configured
    if log_dir is None:
        log_dir = os.path.join(data_dir())
    os.makedirs(log_dir, exist_ok=True)

    numeric_level = getattr(logging, str(log_level).upper(), logging.INFO)

    with _configured_lock:
        if _configured:
            logging.getLogger().setLevel(numeric_level)
            return

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
        # Les rejets en boucle ne doivent plus saturer error.log.
        error_handler.addFilter(DedupFilter(DEDUP_WINDOW_S))
        logging.getLogger().addHandler(error_handler)
        _configured = True
