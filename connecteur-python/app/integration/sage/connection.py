"""Couche de connexion SQL Server (Sage 100) : pool pyodbc et curseurs."""
import logging
import queue
import re
import threading
import time
from contextlib import contextmanager, nullcontext

import pyodbc

from app.config.manager import get_db_config
from app.core.constants import CONNECT_RETRY_DELAYS_S

logger = logging.getLogger("t-connector.db")


_pool = None

_pool_lock = threading.Lock()

_db_lock = threading.Lock()

_pool_queue = None

_pool_size = 0

# Erreurs ODBC transitoires : un retry court evite de bloquer un worker et de
# transformer un timeout reseau isole en "Configuration base de donnees
# incomplete" (audit 2026-10-21 : 704 Login timeout, 19 le 21/10).

_TRANSIENT_MARKERS = (
    "timeout expired", "hyt00", "08001", "08s01",
    "communication link failure", "en cours d'utilisation",
    "80003", "server is unavailable",
    "configuration base de donnees incomplete",
)

def _is_transient(exc):
    """Vrai pour une erreur ODBC/reseau qui peut reussir apres retry."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)

def _pool_capacity():
    try:
        cfg = get_db_config()
        lo = int(cfg.get("pool_min", 2) or 2)
        hi = int(cfg.get("pool_max", 10) or 10)
    except (ValueError, TypeError):
        lo, hi = 2, 10
    lo = max(1, min(lo, 8))
    hi = max(lo, min(hi, 16))
    return lo, hi

def _connect():
    cfg = get_db_config()
    if not cfg.get("server") or not cfg.get("database"):
        raise ValueError("Configuration base de donnees incomplete")
    conn = pyodbc.connect(_build_conn_string(cfg),
                          timeout=cfg.get("connection_timeout_ms", 15000) // 1000,
                          autocommit=True)
    try:
        conn.timeout = max(1, int(cfg.get("request_timeout_ms", 30000) or 30000) // 1000)
    except Exception:
        pass
    return conn

def _connect_retry_attempts():
    """Nombre de tentatives de connexion ODBC (defaut 3, borne 1-5)."""
    try:
        value = int(get_db_config().get("connect_retry_attempts", 3) or 3)
    except (TypeError, ValueError):
        return 3
    return max(1, min(value, 5))

def _connect_with_retry(attempts=None):
    """Ouvre une connexion ODBC en retentant les erreurs transitoires.

    Backoff 1/3 s (la troisieme tentative n'attend pas apres son echec).
    Une erreur definitive (42000 hors verrou, config invalide autre) remonte
    immediatement sans retry.
    """
    if attempts is None:
        attempts = _connect_retry_attempts()
    last = None
    for i in range(max(1, attempts)):
        try:
            return _connect()
        except Exception as e:  # noqa: BLE001
            last = e
            if not _is_transient(e):
                raise
            if i >= attempts - 1:
                break
            delay = CONNECT_RETRY_DELAYS_S[min(i, len(CONNECT_RETRY_DELAYS_S) - 1)]
            logger.warning(
                "Pool Sage: connexion %d/%d echouee (%s), retry dans %ds",
                i + 1, attempts, e, delay,
            )
            time.sleep(delay)
    raise last

def _ensure_pool():
    global _pool_queue, _pool_size
    lo, hi = _pool_capacity()
    with _pool_lock:
        if _pool_queue is None:
            _pool_queue = queue.Queue()
            for _ in range(lo):
                try:
                    _pool_queue.put(_connect())
                    _pool_size += 1
                except Exception as e:
                    level = logging.WARNING if _is_transient(e) else logging.ERROR
                    logger.log(level, "Pool Sage: connexion initiale echouee: %s", e)
                    break
            logger.info("Pool Sage initialise: %d connexion(s)", _pool_size)
    return hi

def _healthy(conn):
    try:
        conn.execute("SELECT 1")
        return True
    except Exception:
        return False

def _checkout_timeout_s():
    """Attente max d'un connecteur du pool (defaut 30 s, borne 0-300 s)."""
    try:
        value = int(get_db_config().get("pool_checkout_timeout_ms", 30000) or 30000)
    except (TypeError, ValueError):
        value = 30000
    return max(0, min(value, 300000)) / 1000.0

def _checkout(hi):
    global _pool_size
    timeout = _checkout_timeout_s()
    try:
        conn = _pool_queue.get(block=True, timeout=timeout)
    except queue.Empty:
        conn = None
    if conn is not None and not _healthy(conn):
        try:
            conn.close()
        except Exception:
            pass
        with _pool_lock:
            _pool_size = max(0, _pool_size - 1)
        conn = None
    if conn is None:
        with _pool_lock:
            if _pool_size < hi:
                try:
                    conn = _connect_with_retry()
                    _pool_size += 1
                except Exception as e:
                    level = logging.WARNING if _is_transient(e) else logging.ERROR
                    logger.log(level,
                               "Pool Sage: connexion a la demande echouee: %s", e)
    if conn is None:
        try:
            conn = _pool_queue.get(block=True, timeout=timeout)
            if not _healthy(conn):
                try:
                    conn.close()
                except Exception:
                    pass
                with _pool_lock:
                    _pool_size = max(0, _pool_size - 1)
                conn = None
        except queue.Empty:
            conn = None
    return conn

def _checkin(conn):
    if conn is None:
        return
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        _pool_queue.put(conn, block=False)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        with _pool_lock:
            global _pool_size
            _pool_size = max(0, _pool_size - 1)

def _sanitize_db_val(val):
    if val is None or isinstance(val, (int, float, bool)):
        return val
    if isinstance(val, str):
        stripped = val.strip()
        if not stripped:
            return val
        has_comma = "," in stripped
        cleaned = stripped.replace(",", ".")
        if has_comma:
            try:
                f = float(cleaned)
                if f == int(f):
                    return int(f)
                return f
            except (ValueError, TypeError):
                pass
        if re.match(r"^-?\d+(\.\d+)?$", cleaned):
            try:
                f = float(cleaned)
                if f == int(f):
                    return int(f)
                return f
            except (ValueError, TypeError):
                pass
    return val

class _SafeRow:
    def __init__(self, row):
        self._row = row

    def __getitem__(self, key):
        return _sanitize_db_val(self._row[key])

    def __len__(self):
        return len(self._row)

    def __iter__(self):
        for i in range(len(self._row)):
            yield _sanitize_db_val(self._row[i])

    def __repr__(self):
        return repr(self._row)

class _SafeCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, *args, **kwargs):
        self._cursor.execute(*args, **kwargs)
        return self

    def executemany(self, *args, **kwargs):
        self._cursor.executemany(*args, **kwargs)
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        return _SafeRow(row) if row is not None else None

    def fetchall(self):
        return [_SafeRow(row) for row in self._cursor.fetchall()]

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def close(self):
        try:
            self._cursor.close()
        except Exception:
            pass

def _server_clause(server, port):
    """Retourne la valeur SERVER=... avec le port intégré.

    Les pilotes MS ODBC 17/18 ignorent la clé « PORT=... » : le port doit être
    collé au nom d'hôte (SERVER=tcp:hote,1444). Les instances nommées
    (hote\\instance) et les serveurs portant déjà un port sont laissés tels
    quels pour ne pas casser les configurations existantes.
    """
    server = (server or "").strip()
    if not server or "\\" in server:
        return server
    prefixed = server.lower().startswith("tcp:")
    host = server[4:] if prefixed else server
    if "," in host:
        return server
    try:
        port = int(port)
    except (TypeError, ValueError):
        return server
    clause = "{},{}".format(host, port)
    return "tcp:" + clause if prefixed else clause

def _build_conn_string(cfg):
    import pyodbc as _pyodbc
    available = [d.strip() for d in _pyodbc.drivers()]
    preferred = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "ODBC Driver 11 for SQL Server",
        "SQL Server",
        "SQL Server Native Client 11.0",
    ]
    driver = next((d for d in preferred if d in available), "SQL Server")

    parts = [
        "DRIVER={{{}}}".format(driver),
        "SERVER={}".format(_server_clause(cfg["server"], cfg.get("port", 1433))),
        "DATABASE={}".format(cfg["database"]),
    ]
    if cfg.get("trusted_connection"):
        parts.append("Trusted_Connection=yes")
    else:
        parts.append("UID={}".format(cfg.get("user", "")))
        parts.append("PWD={}".format(cfg.get("password", "")))
    if cfg.get("encrypt"):
        parts.append("Encrypt=yes")
    else:
        parts.append("Encrypt=no")
    if cfg.get("trust_server_certificate"):
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts)

def get_pool():
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.execute("SELECT 1")
                return _pool
            except Exception:
                try:
                    _pool.close()
                except Exception:
                    pass
                _pool = None

        cfg = get_db_config()
        if not cfg.get("server") or not cfg.get("database"):
            raise ValueError("Configuration base de donnees incomplete")

        _pool = _connect_with_retry()
        logger.info("Connexion SQL Server etablie: %s/%s", cfg["server"], cfg["database"])
        return _pool

def close_pool():
    global _pool, _pool_queue, _pool_size
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.close()
            except Exception:
                pass
            _pool = None
        q, _pool_queue = _pool_queue, None
        _pool_size = 0
    while q is not None:
        try:
            conn = q.get(block=False)
        except queue.Empty:
            break
        try:
            conn.close()
        except Exception:
            pass
    logger.info("Connexion SQL Server fermee")

@contextmanager
def get_cursor():
    hi = _ensure_pool()
    conn = _checkout(hi)
    if conn is None:
        # Repli : ancien chemin mono-connexion sous verrou
        with _db_lock:
            conn = get_pool()
            raw = conn.cursor()
            try:
                raw.arraysize = 1000
            except Exception:
                pass
            cursor = _SafeCursor(raw)
            try:
                yield cursor
                conn.commit()
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass
                raise
            finally:
                cursor.close()
        return
    raw = conn.cursor()
    try:
        raw.arraysize = 1000
    except Exception:
        pass
    cursor = _SafeCursor(raw)
    try:
        yield cursor
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cursor.close()
        _checkin(conn)

def ping_database():
    # Import tardif : `schema` dépend de `connection.get_cursor`.
    from app.integration.sage.schema import discover_tables

    try:
        with get_cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        table_map = discover_tables()
        return {"ok": True, "table_map": table_map}
    except Exception as e:
        return {"ok": False, "error": str(e)}
