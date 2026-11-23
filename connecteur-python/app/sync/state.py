"""État partagé du moteur de synchronisation.

Tout ce qui est mutable et partagé entre les modules `app.sync` vit ici :
cache des factures, verrous, file de retry, état du polling. Les autres
modules importent ces objets (mutés sur place) ; les valeurs réaffectées
passent par les setters pour rester visibles de tous.

`engine.py` reste le propriétaire de son état purement interne (cache du
lookup SFEC, reset de démarrage) : ces noms sont patchés directement par les
tests et ne sont pas partagés avec les autres modules.
"""
import threading

_cache = {
    "sales_invoices": [],
    "purchase_invoices": [],
    "contacts": [],
    "tax_rates": [],
    "ledger_accounts": [],
    "sfec_invoices": [],
    "last_sync_at": None,
    "last_sfec_sync_at": None,
    "db_connected": False,
    "last_error": None,
    "sync_count": 0,
    "certified_count": 0,
    "error_count": 0,
}

_lock = threading.Lock()
_polling_thread = None
_stop_event = threading.Event()

_retry_queue = []
_retry_lock = threading.Lock()

# Empeche deux passes de certification concurrentes : sync_all() relache
# _lock avant d'appeler auto_certify_invoices().
_certify_run_lock = threading.Lock()

# Backoff en memoire des erreurs transitoires (reseau, verrou Sage, 502).
# Aucune ecriture 'ERREUR' en base : une facture doit pouvoir reessayer.
_retry_after = {}
_retry_attempts = {}
_RETRY_BACKOFF_S = (60, 300, 900, 3600)
_logged_errors = set()


def get_cache():
    with _lock:
        return dict(_cache)


def get_retry_queue():
    with _retry_lock:
        return list(_retry_queue)


def polling_active():
    """Indique si le polling est active par config ET si le thread tourne."""
    from app.config.manager import get_config

    enabled = get_config().get("db", {}).get("polling_enabled", True)
    thread = _polling_thread
    return bool(enabled and thread is not None and thread.is_alive())


def polling_thread():
    return _polling_thread


def set_polling_thread(thread):
    global _polling_thread
    _polling_thread = thread
