# Proposition 03 — Connexions SQLite thread-local

> **Priorité** : 🔴 P0 — provoque les 500 au login et des transactions perdues.
> **Fichier** : `app/storage/db.py`
> **Tests** : `tests/test_sqlite_concurrency.py` (nouveau)
> **Réf. rapport** : `00-rapport-analyse-logs.md` §3.3

---

## 1. Symptômes

```
2026-10-21 18:13:47 [app.web.app] ERROR: Exception on /login [POST]
  File "app/web/auth/user_auth.py", line 111, in ensure_schema
    with sqlite_db.get_cursor() as cur:
  File "app/storage/db.py", line 68, in get_cursor
    conn.commit()
SystemError: error return without exception set

[t-connector.sync.bi] ERROR: cannot commit - no transaction is active        (4)
[t-connector.sync.bi] ERROR: bad parameter or other API misuse              (3)
[t-connector.auth]   WARNING: Audit log impossible: error return without exception set
```

Trois 500 consécutifs sur `POST /login` le 21/10 entre 18:13:47 et 18:13:59 : le dashboard est inaccessible tant que le process n'est pas redémarré.

## 2. Cause racine

`app/storage/db.py:20-70` :

```python
_conn = None

def get_connection():
    global _conn
    ...
    _conn = sqlite3.connect(path, timeout=10, check_same_thread=False)

@contextmanager
def get_cursor():
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    ...
```

**Une seule connexion** est partagée par :

| Thread | Référence |
|---|---|
| Flask (requêtes HTTP) | `main.py:111` (`threaded=True`) |
| Scheduler de sync | `app/sync/scheduler.py:43` |
| 5 workers de certification | `engine.py:620` (`ThreadPoolExecutor`) |
| BI sync | `app/sync/bidirectional.py:271` |
| Watcher d'impression | `app/print_hijack/folder_watcher.py:80-84` |
| Contrôle connectivité | `app/sync/connectivity.py:146` |

`check_same_thread=False` **désactive seulement le contrôle** : il ne rend pas la connexion thread-safe. Un `commit()` concurrent pendant qu'un autre thread itère un curseur produit exactement les erreurs observées (`SystemError`, `cannot commit`, `bad parameter`, connexion définitivement corrompue).

## 3. Correctif

### 3.1 Connexion par thread

**Bloc à remplacer** (`db.py:18-61`) :

```python
_lock = threading.Lock()
_conn = None


def _get_db_path():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DB_PATH


def get_connection():
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.execute("SELECT 1")
                return _conn
            except Exception:
                try:
                    _conn.close()
                except Exception:
                    pass
                _conn = None

        path = _get_db_path()
        _conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.execute("PRAGMA busy_timeout=5000")
        logger.info("SQLite connecte: %s", path)
        return _conn


def close_connection():
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
            _conn = None
```

**Bloc corrigé** :

```python
# Une connexion sqlite3 n'est PAS reentrante : la partager entre les
# threads Flask, le scheduler, les workers de certification et le watcher
# provoquait des SystemError sur commit() et des transactions perdues
# (audit 2026-10-21). Connexion dediee par thread, WAL pour la concurrence
# lecteur/lecteur + writer unique.
_local = threading.local()


def _get_db_path():
    os.makedirs(DATA_DIR, exist_ok=True)
    return DB_PATH


def get_connection():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _local.conn = None

    path = _get_db_path()
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    _local.conn = conn
    logger.info("SQLite connecte (thread=%s): %s", threading.current_thread().name, path)
    return conn


def close_connection():
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
```

> `timeout=30` + `busy_timeout=10000` : avec WAL, un seul writer à la fois ; on laisse SQLite patienter au lieu de lever `database is locked`.

### 3.2 Ne committer que si une transaction est ouverte

**Bloc à remplacer** (`db.py:63-74`) :

```python
@contextmanager
def get_cursor():
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close()
```

**Bloc corrigé** :

```python
@contextmanager
def get_cursor():
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        if conn.in_transaction:
            conn.commit()
    except Exception:
        if conn.in_transaction:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        cur.close()
```

Cela supprime `cannot commit - no transaction is active` sur les contextes en lecture seule (`ensure_schema`, `authenticate`, KPI) et évite un `commit` inutile à chaque SELECT.

### 3.3 (Optionnel) Retry sur verrou

Ajouter un helper pour les écritures critiques :

```python
def execute_with_retry(cur, sql, params=(), attempts=3):
    last = None
    for i in range(attempts):
        try:
            cur.execute(sql, params)
            return
        except sqlite3.OperationalError as e:
            last = e
            if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                raise
            time.sleep(0.2 * (2 ** i))
    raise last
```

À utiliser uniquement dans les boucles de sync (pas dans les routes HTTP).

### 3.4 Fermeture propre à l'arrêt

`main.py` appelle `close_all_connections()` sur `SIGINT`/`SIGTERM`. Avec le stockage thread-local, seules les connexions du thread appelant sont accessibles directement ; on tient donc un registre faible par thread (`sqlite3.Connection` n'est pas weakref-able, mais `threading.Thread` l'est) :

```python
import weakref

_all_conns = weakref.WeakKeyDictionary()
_all_lock = threading.Lock()

# a la creation :
with _all_lock:
    _all_conns[threading.current_thread()] = conn

# dans close_all_connections() :
with _all_lock:
    conns = list(_all_conns.values())
    _all_conns.clear()
for c in conns:
    try:
        c.close()
    except Exception:
        pass
```

Le registre faible évite de retenir les connexions des workers éphémères du `ThreadPoolExecutor` après la mort de leur thread.

## 4. Tests

`tests/test_sqlite_concurrency.py` :

```python
import sqlite3
import threading
from app.storage import db


def _worker(n, errors):
    try:
        for i in range(100):
            with db.get_cursor() as cur:
                cur.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            ("k_%d_%d" % (n, i), str(i)))
            with db.get_cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM settings").fetchone()
    except Exception as e:      # noqa: BLE001
        errors.append(e)


def test_concurrence_sans_systemerror():
    db.init_database()
    errors = []
    threads = [threading.Thread(target=_worker, args=(i, errors)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
```

Et :

```python
def test_get_cursor_lecture_seule_ne_commit_pas(monkeypatch):
    commits = []
    with db.get_cursor() as cur:
        cur.execute("SELECT 1")
        monkeypatch.setattr(cur.connection, "commit", lambda: commits.append(1))
    # en lecture seule, aucun commit ne doit etre emis
    assert commits == []
```

> Adapter le second test à l'API `sqlite3` si `cur.connection` n'est pas exposé : vérifier `conn.in_transaction` avant/après.

## 5. Critères d'acceptation

- [ ] 20 threads × 100 écritures + lectures : 0 exception.
- [ ] 24 h sans `SystemError`, `cannot commit`, `bad parameter`.
- [ ] `POST /login` ne renvoie plus 500.
- [ ] `Audit log impossible` disparaît.
- [ ] Les tests existants passent (`pytest -q`), notamment `tests/test_smoke.py`.

## 6. Risques / rollback

| Risque | Parade |
|---|---|
| `database is locked` plus fréquent (WAL + writers multiples) | `busy_timeout=10000`, `synchronous=NORMAL`, retry sur les boucles de sync |
| Connexions qui s'accumulent | Une par thread ; les threads du pool sont réutilisés. Ajouter `close_all_connections()` à l'arrêt |
| Tests qui supposent une connexion unique | Vérifier `tests/conftest.py` ; la plupart utilisent `get_cursor()`, donc transparent |
| Base sur partage réseau (interdit avec WAL) | La base est dans `data/` local, pas sur un partage SMB |

**Rollback** : revert du seul `app/storage/db.py`.
