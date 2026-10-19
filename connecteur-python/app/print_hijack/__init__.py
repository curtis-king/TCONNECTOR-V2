"""Print hijacking — watch dossier PDF -> facture conforme -> impression.

Scénario (V1 minimal) : l'usager enregistre le PDF d'une facture Sage dans
un dossier surveillé ; le connecteur identifie la facture, régénère la
version conforme SFEC (mentions + QR) et l'imprime seul sur l'imprimante
configurée. L'original est archivé, les échecs partent en quarantaine.

Activation : config `print_hijack.enabled = true`.
"""
import logging
import os
import threading
from logging.handlers import RotatingFileHandler

from app.config.manager import get_config
from app.core.paths import base_dir, data_dir

logger = logging.getLogger("t-connector.print_hijack")

_LOG_FORMAT = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
_watcher = None
_lock = threading.Lock()


def _setup_file_log():
    if getattr(logger, "_tc_configured", False):
        return
    try:
        path = os.path.join(data_dir(), "print_hijack.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        logger.addHandler(handler)
        logger.propagate = True
    except Exception as e:  # noqa: BLE001
        logger.warning("Log print_hijack indisponible: %s", e)
    logger._tc_configured = True  # type: ignore[attr-defined]


def resolve_dir(value, default_rel):
    """Résout un chemin de dossier (relatif -> racine de l'app)."""
    value = (value or default_rel).strip()
    if os.path.isabs(value):
        return value
    return os.path.join(base_dir(), value.replace("/", os.sep))


def get_print_hijack_config():
    cfg = get_config().get("print_hijack", {}) or {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "watch_dir": resolve_dir(cfg.get("watch_dir"), "data/WATCH/IN"),
        "out_dir": resolve_dir(cfg.get("out_dir"), "data/WATCH/OUT"),
        "done_dir": resolve_dir(cfg.get("done_dir"), "data/WATCH/DONE"),
        "quarantine_dir": resolve_dir(cfg.get("quarantine_dir"), "data/WATCH/QUARANTINE"),
        "printer_name": str(cfg.get("printer_name", "") or ""),
        "sumatra_path": str(cfg.get("sumatra_path", "") or ""),
        "stable_delay_s": float(cfg.get("stable_delay_s", 2) or 2),
        "stable_retries": int(cfg.get("stable_retries", 5) or 5),
        "poll_interval_s": int(cfg.get("poll_interval_s", 5) or 5),
        "dedup_window_min": int(cfg.get("dedup_window_min", 10) or 10),
        "max_pages": int(cfg.get("max_pages", 50) or 50),
        "max_size_mb": int(cfg.get("max_size_mb", 20) or 20),
        "dry_run": bool(cfg.get("dry_run", False)),
    }


def start_print_hijack():
    """Démarre le watcher si `print_hijack.enabled` (idempotent)."""
    global _watcher
    cfg = get_print_hijack_config()
    if not cfg["enabled"]:
        logger.info("Watcher PDF desactive (print_hijack.enabled=false)")
        return None
    with _lock:
        if _watcher is not None and _watcher.is_alive():
            return _watcher
        _setup_file_log()
        from app.print_hijack.folder_watcher import FolderWatcher
        _watcher = FolderWatcher(cfg)
        _watcher.start()
        logger.info("Watcher PDF demarre (IN=%s, imprimante='%s', dry_run=%s)",
                    cfg["watch_dir"], cfg["printer_name"], cfg["dry_run"])
        return _watcher


def stop_print_hijack():
    global _watcher
    with _lock:
        if _watcher is not None:
            try:
                _watcher.stop()
            except Exception as e:  # noqa: BLE001
                logger.warning("Arret watcher PDF: %s", e)
            _watcher = None


def get_watcher():
    return _watcher
