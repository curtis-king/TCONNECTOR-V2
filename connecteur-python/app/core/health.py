"""Supervision minimale : etat SQLite, SFEC, impressions et alertes.

Le detail n'est expose que sur la boucle locale (voir /health). Aucun ping
Sage n'est declenche ici : le cache du moteur evite d'ouvrir une connexion
ODBC a chaque appel de supervision.
"""
import logging
from datetime import datetime, timezone

from app.config.manager import get_config, get_sfec_api_key
from app.storage import db as sqlite_db

logger = logging.getLogger("t-connector.health")

# Seuils de la proposition 09 (§6).
MAX_PENDING_WRITES = 50
MAX_MINUTES_WITHOUT_SFEC = 60


def is_loopback(remote_addr):
    """True si la requete vient de la machine (health non expose au LAN)."""
    addr = (remote_addr or "").strip()
    if addr == "::1" or addr == "localhost":
        return True
    if addr.startswith("::ffff:"):
        addr = addr[7:]
    return addr == "127.0.0.1" or addr.startswith("127.")


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _minutes_since(value, now):
    dt = _parse_iso(value)
    if dt is None:
        return None
    return max(0.0, (now - dt).total_seconds() / 60.0)


def collect_health(now=None):
    """Retourne un etat JSON-safe, sans jamais lever d'exception."""
    now = now or datetime.now(timezone.utc)
    cfg = get_config() or {}
    sfec_cfg = cfg.get("sfec", {}) or {}
    sqlite_ok = False
    try:
        with sqlite_db.get_cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            sqlite_ok = bool(cur.fetchone())
    except Exception as e:  # noqa: BLE001
        logger.warning("Health: SQLite inaccessible: %s", e)

    cache = {}
    metrics = {}
    try:
        from app.sync.engine import get_cache, get_metrics
        cache = get_cache() or {}
        metrics = get_metrics() or {}
    except Exception as e:  # noqa: BLE001
        logger.debug("Health: moteur indisponible: %s", e)

    last_sync = cache.get("last_sync_at")
    last_sfec_sync = cache.get("last_sfec_sync_at")
    pending_accounting = int(metrics.get("pending_accounting", 0) or 0)

    pending_writes = 0
    pending_writes_blocked = 0
    try:
        from app.integration.sfec.pending_writes import pending_stats
        stats = pending_stats()
        pending_writes = int(stats.get("total", 0) or 0)
        pending_writes_blocked = int(stats.get("blocked", 0) or 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("Health: comptage ecritures differees impossible: %s", e)

    alerts = []
    if pending_writes > MAX_PENDING_WRITES:
        alerts.append("sfec_pending_writes={} (> {})".format(
            pending_writes, MAX_PENDING_WRITES))
    if pending_writes_blocked > 0:
        alerts.append(
            "sfec_pending_writes: {} ecriture(s) bloquee(s) definitivement"
            .format(pending_writes_blocked))
    if pending_accounting > 0:
        age = _minutes_since(last_sfec_sync, now)
        if age is None or age > MAX_MINUTES_WITHOUT_SFEC:
            age_txt = "jamais" if age is None else "{} min".format(int(age))
            alerts.append(
                "{} facture(s) en attente et derniere sync SFEC: {}".format(
                    pending_accounting, age_txt)
            )

    status = "ok"
    if not sqlite_ok:
        status = "error"
    elif alerts:
        status = "degraded"

    return {
        "status": status,
        "ok": sqlite_ok,
        "db": "up" if sqlite_ok else "down",
        "checks": {
            "sqlite": "up" if sqlite_ok else "down",
            "sage_cache": "populated" if cache.get("db_connected") else "empty",
            "sfec": (
                "configured" if get_sfec_api_key(sfec_cfg) else
                "missing_api_key" if sfec_cfg.get("enabled") else "disabled"
            ),
        },
        "sfec_enabled": bool(sfec_cfg.get("enabled")),
        "last_sync": last_sync,
        "last_sfec_sync": last_sfec_sync,
        "pending_writes": pending_writes,
        "pending_writes_blocked": pending_writes_blocked,
        "pending_accounting": pending_accounting,
        "alerts": alerts,
    }
