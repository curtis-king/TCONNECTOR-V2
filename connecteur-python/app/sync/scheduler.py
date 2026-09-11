"""Orchestration des tâches de fond (extrait de main.py).

Centralise le démarrage/arrêt du polling, de la synchronisation
bidirectionnelle, de la vérification de connectivité et du préchauffage
du cache SFEC.
"""
import threading
import logging
from datetime import datetime

from app.sync.connectivity import (
    start_background_check,
    stop_background_check,
)
from app.sync.engine import start_polling, stop_polling, sync_sfec_invoices
from app.sync.bidirectional import start_bi_sync, stop_bi_sync

logger = logging.getLogger("t-connector.scheduler")

_cached = {"interval": 30}
_started = False
_lock = threading.Lock()


def start_all(interval=None):
    """Démarre toutes les tâches de fond de manière idempotente."""
    global _started, _cached
    with _lock:
        if _started:
            return
        if interval is not None:
            _cached["interval"] = interval

        logger.info("Demarrage du polling intelligent...")
        start_polling()

        logger.info("Demarrage de la sync bidirectionnelle (intervalle: %ds)...",
                    max(int(_cached["interval"]), 15))
        start_bi_sync(interval=max(int(_cached["interval"]), 15))

        start_background_check()

        sfec_thread = threading.Thread(
            target=_prewarm_sfec_cache,
            daemon=True,
            name="sfec-cache-init",
        )
        sfec_thread.start()

        _started = True
        logger.info("Tâches de fond demarrees")


def _prewarm_sfec_cache():
    try:
        sync_sfec_invoices()
    except Exception as e:  # noqa: BLE001
        logger.warning("P rechauffement cache SFEC: %s", e)


def stop_all():
    """Arrête proprement toutes les tâches de fond."""
    global _started
    with _lock:
        stop_polling()
        stop_bi_sync()
        stop_background_check()
        _started = False
        logger.info("Tâches de fond arretees")
