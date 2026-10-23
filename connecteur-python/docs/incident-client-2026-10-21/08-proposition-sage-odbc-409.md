# Proposition 08 — ODBC Sage, 409 et écritures différées

> **Priorité** : 🟡 P2 — la certification aboutit mais n'est pas toujours enregistrée dans Sage.
> **Fichiers** : `app/integration/sage/database.py` (`_checkout`, `get_pool`), `app/sync/engine.py` (`_handle_already_certified`), `app/integration/sfec/pending_writes.py` (nouveau)
> **Réf. rapport** : `00-rapport-analyse-logs.md` §3.8

---

## 1. Symptômes

| Message | Occurrences | Fenêtre |
|---|---|---|
| `Login timeout expired` | **704** | 53 le 17/09, 632 le 21/09, **19 le 21/10** |
| `Ce serveur SQL n'existe pas ou son accès n'est pas autorisé` | 10 | 21/10 |
| `Configuration base de donnees incomplete` | 14 | démarrage |
| `Cet élément est en cours d'utilisation` | 8 | écritures Sage du 21/10 |
| `SFEC API error 409 — invoice with the same invoice number already exists` | **22** | 21/10 |

Séquence typique du 21/10 :

```text
18:27:31 [t-connector.sync.bi] ERROR: Ecriture SFEC echouee (deja certifiee) pour 25VFA3142:
         ('42000', "[42000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]
          Cet élément est en cours d'utilisation.")
18:29:02 [t-connector.sync] ERROR: Certification echouee pour 25VFA3142:
         SFEC API error 409: invoice with the same invoice number already exists
```

La certification **existe** côté SFEC, mais le connecteur ne parvient pas à l'écrire dans Sage. Il ne marque donc pas la facture, et la rejoue → nouveau 409.

## 2. Causes racines

### 2.1 `_checkout()` ne réessaie pas

`database.py:96-131` : si `_connect()` échoue une fois, `conn` reste `None` et l'appelant reçoit `None`, d'où `Configuration base de donnees incomplete`. Aucun backoff, aucune distinction erreur transitoire / définitive.

### 2.2 `get_pool()` non plus

`database.py:356-379` tente une seule connexion, puis lève.

### 2.3 Le 409 dépend d'un GET qui peut échouer

`_handle_already_certified()` (`engine.py:356-397`) :

```python
    if sfec_inv:
        ...
        write_sfec_to_invoice(...)      # peut lever (verrou Sage)
    else:
        mark_certified(invoice["id"], "DEJA_CERTIFIE")   # <-- sans preuve !
```

Deux défauts :

1. si l'écriture Sage échoue, l'exception est seulement loguée : rien n'est mémorisé, la facture sera rejouée ;
2. si le GET SFEC échoue (réseau, 404 temporaire), la facture est marquée `DEJA_CERTIFIE` **sans numéro de certification** → elle ne sera plus jamais tentée, et Sage n'aura jamais la certification.

### 2.4 Le cache SFEC n'est pas invalidé après un 409

`_SFEC_LOOKUP_TTL = 300` (`engine.py:46`). Après un 409, le cache peut encore ignorer la facture pendant 5 minutes → le GET direct est tenté, mais si le réseau est instable, on retombe dans le cas 2.3.

## 3. Correctif

### 3.1 Retry + backoff dans le pool

`database.py:96-131` — nouvelle version de `_checkout` :

```python
_TRANSIENT_MARKERS = (
    "timeout expired", "hyt00", "08001", "08s01",
    "communication link failure", "en cours d'utilisation",
    "80003", "server is unavailable",
)


def _is_transient(exc):
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def _connect_with_retry(attempts=3):
    last = None
    for i in range(attempts):
        try:
            return _connect()
        except Exception as e:      # noqa: BLE001
            last = e
            if not _is_transient(e):
                raise
            delay = (1, 3, 7)[min(i, 2)]
            logger.warning("Pool Sage: connexion %d/%d echouee (%s), retry dans %ds",
                           i + 1, attempts, e, delay)
            time.sleep(delay)
    raise last


def _checkout(hi):
    global _pool_size
    try:
        conn = _pool_queue.get(block=True, timeout=30)
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
                    logger.error("Pool Sage: connexion a la demande echouee: %s", e)
    return conn
```

`get_pool()` (`database.py:356-379`) : remplacer `pyodbc.connect(...)` par un appel à `_connect_with_retry()`.

### 3.2 Ne jamais marquer « déjà certifié » sans preuve

`engine.py:393-397` :

**Bloc à remplacer**

```python
    else:
        try:
            mark_certified(invoice["id"], "DEJA_CERTIFIE")
        except Exception:
            pass
```

**Bloc corrigé**

```python
    else:
        # 409 sans GET exploitable : on NE marque PAS la facture. Sans numéro
        # de certification, la marquer DEJA_CERTIFIE la rendrait invisible et
        # Sage resterait sans preuve (audit 2026-10-21 : 22 x 409).
        logger.warning(
            "409 pour %s mais certification SFEC illisible: facture laissee "
            "en attente et verification replanifiee", numero,
        )
        _invalidate_sfec_lookup()
```

Avec :

```python
def _invalidate_sfec_lookup():
    global _sfec_lookup_cache, _sfec_lookup_ts
    _sfec_lookup_cache = None
    _sfec_lookup_ts = 0
```

À appeler aussi **avant** `verify_by_invoice_number` dans `_handle_already_certified` (le cache vient d'être contredit par un 409).

> Au passage, `_handle_already_certified()` doit aussi persister le champ `invoice_number` renvoyé par SFEC : c'est le candidat naturel pour `original_sfec_invoice_number` d'un avoir (proposition 05 §3.7). Ne pas confondre avec `sfec_num_certif` (la signature).

### 3.3 File d'écriture différée

Une certification SFEC est **idempotente** : si l'écriture Sage échoue, il suffit de la rejouer. On la persiste localement au lieu de la perdre.

`app/integration/sfec/pending_writes.py` (nouveau) :

```python
"""File d'attente des certifications SFEC a ecrire dans Sage.

Une certification obtenue de SFEC ne doit jamais etre perdue parce que
Sage est verrouille (audit 2026-10-21). On la stocke en SQLite et on la
rejoue a chaque cycle ; l'ecriture est idempotente.
"""
import logging

from app.integration.sage.database import write_sfec_to_invoice, mark_to_monitor
from app.storage.db import get_cursor

logger = logging.getLogger("t-connector.sfec.pending")

MAX_ATTEMPTS = 10


def init_pending_table():
    with get_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sfec_pending_writes (
                invoice_id TEXT PRIMARY KEY,
                certification_number TEXT,
                signature TEXT,
                qr_code TEXT,
                certification_date TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)


def enqueue(invoice_id, certification_number, signature, qr_code,
            certification_date, error=""):
    init_pending_table()
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO sfec_pending_writes (
                invoice_id, certification_number, signature, qr_code,
                certification_date, last_error
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(invoice_id) DO UPDATE SET
                certification_number = excluded.certification_number,
                signature = excluded.signature,
                qr_code = excluded.qr_code,
                certification_date = excluded.certification_date,
                last_error = excluded.last_error
        """, (invoice_id, certification_number, signature, qr_code,
              certification_date, error[:300]))
    logger.info("Certification SFEC mise en file pour %s", invoice_id)


def flush(limit=20):
    init_pending_table()
    with get_cursor() as cur:
        cur.execute("""SELECT * FROM sfec_pending_writes
                       ORDER BY created_at LIMIT ?""", (limit,))
        rows = cur.fetchall()

    done = 0
    for row in rows:
        try:
            write_sfec_to_invoice(
                invoice_id=row["invoice_id"],
                certification_number=row["certification_number"],
                signature=row["signature"],
                qr_code=row["qr_code"],
                certification_date=row["certification_date"],
            )
        except Exception as e:      # noqa: BLE001
            attempts = (row["attempts"] or 0) + 1
            with get_cursor() as cur:
                cur.execute("""UPDATE sfec_pending_writes
                               SET attempts = ?, last_error = ? WHERE invoice_id = ?""",
                            (attempts, str(e)[:300], row["invoice_id"]))
            if attempts >= MAX_ATTEMPTS:
                try:
                    mark_to_monitor(row["invoice_id"],
                                    "Ecriture Sage impossible apres {} tentatives: {}".format(
                                        attempts, str(e)[:120]))
                except Exception:
                    pass
                logger.error("Abandon ecriture SFEC %s apres %d tentatives",
                             row["invoice_id"], attempts)
            continue

        with get_cursor() as cur:
            cur.execute("DELETE FROM sfec_pending_writes WHERE invoice_id = ?",
                        (row["invoice_id"],))
        done += 1

    if done:
        logger.info("Ecritures SFEC en attente rejouees: %d", done)
    return done
```

Branchement dans `_handle_already_certified()` :

```python
        if cert_number or qr:
            try:
                write_sfec_to_invoice(
                    invoice_id=invoice["id"],
                    certification_number=cert_number,
                    signature=sig,
                    qr_code=qr,
                    certification_date=cert_date,
                )
                ...
            except Exception as ex:
                enqueue(invoice["id"], cert_number, sig, qr, cert_date, str(ex))
```

Et dans `auto_certify_invoices()` (au début ou à la fin de la passe) : `flush(limit=20)`.

### 3.4 Retry du GET SFEC

`SfecClient.verify_by_invoice_number()` : 3 tentatives (2 s / 5 s) sur `NetworkOfflineError`, timeout et 5xx. Sur 404 : retour `None` immédiat (la certification n'existe pas encore).

### 3.5 Classification des erreurs

| Erreur | Traitement |
|---|---|
| `Login timeout`, `HYT00`, `08S01`, `Communication link failure` | transitoire → retry pool (1/3/7 s) |
| `80003` / `Cet élément est en cours d'utilisation` | verrou Sage → file différée, retry au cycle suivant |
| `42000` (syntaxe/permission) | définitif → log ERROR + `A_SURVEILLER` |
| `Configuration base de donnees incomplete` | config/pool pas prêt → retry, log WARNING (pas ERROR) |

## 4. Configuration recommandée

`config.json` :

```json
"db": {
  "server": "10.42.0.47",
  "connection_timeout_ms": 8000,
  "request_timeout_ms": 30000,
  "pool_min": 2,
  "pool_max": 6
}
```

Un timeout de connexion plus court (8 s) évite de bloquer un worker 15 s, puisque le retry prend le relais.

## 5. Tests

1. `_is_transient` : vrai pour `HYT00`, `80003`, `en cours d'utilisation` ; faux pour `42000`.
2. `_connect_with_retry` : 2 échecs transitoires puis succès → 3 appels, retour non nul.
3. `enqueue` + `flush` : écriture Sage en échec 2 fois puis succès → ligne supprimée, 3 tentatives.
4. `flush` après 10 échecs → `mark_to_monitor` appelé, ligne conservée.
5. 409 sans GET → `mark_certified` **non** appelé, `enqueue`/`invalidate` appelés.

## 6. Critères d'acceptation

- [ ] 0 `Configuration base de donnees incomplete` en régime nominal.
- [ ] Une facture certifiée côté SFEC mais non écrite dans Sage est rejouée automatiquement et finit `CERTIFIE`.
- [ ] 0 `DEJA_CERTIFIE` sans `SFEC_NUM_CERTIF` renseigné.
- [ ] Les 22 factures du 21/10 passent `CERTIFIE` (vérifier `sfec-certified.json`).
- [ ] `error.log` ne contient plus de trace `409` non suivie d'une récupération.

## 7. Risques / rollback

| Risque | Parade |
|---|---|
| Retry qui allonge la durée d'une passe | 3 tentatives max, backoff court (1/3/7 s) |
| File qui grossit | `flush` à chaque cycle, abandon après 10 essais + `A_SURVEILLER` |
| Écriture Sage rejouée alors que la facture a été modifiée | `write_sfec_to_invoice` ne touche que les colonnes SFEC |
| Table locale oubliée dans les sauvegardes | Elle est recréée par `init_pending_table()` au démarrage |

**Rollback** : revert de `engine.py` et `database.py` ; la table `sfec_pending_writes` peut rester (inutilisée) ou être supprimée.

---

## 9. État d'implémentation — 2026-10-23

### 9.1 Connexions Sage : retry transitoire et classification

`app/integration/sage/database.py` :

- `_TRANSIENT_MARKERS` et `_is_transient()` classent les erreurs `HYT00`, `08001`, `08S01`, `Communication link failure`, `80003`, `Cet élément est en cours d'utilisation`, `server is unavailable` et `Configuration base de donnees incomplete` comme transitoires. Un `42000` de syntaxe/permission reste définitif.
- `_connect_with_retry(attempts=3)` retente uniquement les erreurs transitoires, avec un backoff de **1 s puis 3 s** (aucune attente après le dernier échec). Une erreur définitive remonte immédiatement.
- `_checkout()` et `get_pool()` utilisent `_connect_with_retry()` au lieu d'un `_connect()`/`pyodbc.connect()` unique. Une connexion initiale en échec est désormais retentée à la demande.
- Le log est un `WARNING` pour une erreur transitoire et un `ERROR` pour une erreur définitive. Le cas `Configuration base de donnees incomplete` ne produit donc plus une trace `ERROR` en régime nominal.
- Une connexion trouvée dans la file mais devenue malsaine décrémente aussi `_pool_size`, afin de ne pas bloquer le pool sur un compteur faussé.

### 9.2 409 : aucune certification sans preuve

`app/sync/engine.py` :

- `_invalidate_sfec_lookup()` remet `_sfec_lookup_cache` à `None` et `_sfec_lookup_ts` à `0`. Il est appelé avant le GET direct de `_handle_already_certified()` quand le cache ne contient pas la facture, et après un 409.
- `_handle_already_certified()` retourne `True` uniquement si une preuve (`certification_short_signature` ou QR code) a été trouvée. Sans GET exploitable, la facture **n'est plus marquée** `DEJA_CERTIFIE` : elle reste en attente et sera revérifiée au cycle suivant.
- Les branches `else` qui appelaient `mark_certified(..., "DEJA_CERTIFIE")` sans numéro ont été supprimées. `certify_single()` ne marque plus avant la récupération.
- Le champ `invoice_number` renvoyé par SFEC est conservé en mémoire (`invoice["sfec_invoice_number"]`) et persisté dans `data/sfec-certified.json` par la nouvelle fonction `record_certified_invoice()` (`app/integration/sfec/endpoints.py`). Cette fonction met à jour une fiche existante au lieu de créer un doublon ; `get_certified_by_sage_id()` peut donc fournir `original_sfec_invoice_number` à un avoir.
- Les anciens `DEJA_CERTIFIE` sans `SFEC_NUM_CERTIF` (trace des 22 × 409) ne sont plus ignorés : `_needs_certification_recovery()` les fait repasser par la récupération SFEC si le lookup les contient, ou par une certification/409 sinon. Un `DEJA_CERTIFIE` avec numéro reste ignoré. La file de retry ne considère plus une certification sans numéro comme valide.

### 9.3 File d'écriture différée

Nouveau module `app/integration/sfec/pending_writes.py` et table `sfec_pending_writes` (également créée par `init_database()`) :

- `enqueue()` mémorise `invoice_id`, `certification_number`, `signature`, `qr_code`, `certification_date`, `attempts`, `last_error`.
- `flush(limit=20)` rejoue les écritures Sage non abouties. Une ligne réussie est supprimée ; un échec incrémente `attempts`. À **10 échecs**, `mark_to_monitor()` est appelé une fois, la ligne reste pour l'audit mais n'est plus rejouée (`WHERE attempts < MAX_ATTEMPTS`).
- `flush()` accepte un callback `on_success` ; le moteur l'utilise pour remettre `sfec_statut = CERTIFIE` et `sfec_num_certif` dans le cache sans import circulaire.
- `_flush_pending_writes()` est appelé au début de chaque passe d'auto-certification, avant les contrôles SFEC : l'écriture Sage ne dépend pas d'Internet et une certification obtenue hors ligne n'est pas perdue.
- Les trois chemins d'écriture (`_certify_single_worker`, `_process_retry_queue`, `certify_single`) mettent désormais la certification en file si l'écriture Sage échoue. Le cache passe en `EN_COURS` jusqu'au prochain `flush` réussi. Si même SQLite échoue, l'erreur d'origine est propagée et le retry normal est conservé.
- `write_sfec_to_invoice()` n'est pas modifié : l'écriture reste idempotente et ne touche que les colonnes `SFEC_*`.

### 9.4 Retry du GET SFEC

`app/integration/sfec/client.py` :

- `verify_by_invoice_number()` fait **3 tentatives** avec 2 s puis 5 s sur `NetworkOfflineError`, timeout, erreur réseau et 5xx.
- Un **404 retourne `None` immédiatement** (la certification n'existe pas encore), sans retry.
- Un 400/401/403 retourne `None` immédiatement. Après épuisement, `None` est retourné et l'appelant ne marque pas la facture.
- La pagination existante est conservée dans `_verify_by_invoice_number_once()`.

### 9.5 Tests

`tests/test_sage_odbc_409.py` — **28 tests** :

1. classification `_is_transient` (transitoires, verrou, `42000` définitif) ;
2. `_connect_with_retry` : 2 échecs puis succès, 3 appels, attentes 1/3 s ;
3. `_connect_with_retry` : `42000` non retenté ;
4. `get_pool()` et `_checkout()` passent par `_connect_with_retry()` ;
5. `enqueue` + `flush` : 2 échecs Sage puis succès, ligne supprimée, callback appelé ;
6. `flush` après 10 échecs : `mark_to_monitor` appelé une fois, ligne conservée, plus de rejeu ;
7. callback `_pending_write_done` : cache passé à `CERTIFIE` ;
8. 409 sans GET : `mark_certified` non appelé, lookup invalidé, aucun marquage ;
9. 409 avec GET : écriture Sage, `DEJA_CERTIFIE`, numéro SFEC mémorisé ;
10. 409 avec écriture Sage verrouillée : mise en file, cache `EN_COURS` ;
11. 409 dans `_certify_single_worker` et dans `certify_single` : aucun marquage sans preuve ;
12. certification fraîche dont l'écriture Sage échoue : mise en file ;
13. persistance de `invoice_number` dans `sfec-certified.json`, sans doublon ;
14. GET SFEC : 5xx retenté (2/5 s), 404 immédiat, offline épuisé, 400 non retenté ;
15. invalidation du lookup ;
16. ancien `DEJA_CERTIFIE` sans numéro rejoué, avec numéro non rejoué.

Commande exécutée :

```text
pytest tests/test_sfec_avoirs.py tests/test_sfec_centimes.py \
  tests/test_certification_retry.py tests/test_article_sync.py \
  tests/test_sqlite_concurrency.py tests/test_avoirs.py \
  tests/test_billing_security.py tests/test_dashboard_security.py \
  tests/test_directory_security.py tests/test_smoke.py \
  tests/test_bootstrap.py tests/test_sfec_anomalies.py \
  tests/test_mirror_totals.py tests/test_sage_odbc_409.py -q
→ 151 tests OK
```

### 9.6 Critères d'acceptation restant à vérifier en exploitation

Les critères de la §6 ne peuvent pas être validés uniquement en local : ils nécessitent Sage et SFEC réels.

- [ ] 0 `Configuration base de donnees incomplete` en régime nominal.
- [ ] Une facture certifiée SFEC mais non écrite dans Sage est rejouée au cycle suivant et finit `CERTIFIE`.
- [ ] 0 `DEJA_CERTIFIE` sans `SFEC_NUM_CERTIF` après la première passe de récupération.
- [ ] Les 22 factures du 21/10 passent `CERTIFIE` (contrôle dans `sfec-certified.json` et dans `F_DOCENTETE`).
- [ ] `error.log` ne contient plus de trace `409` non suivie d'une récupération.
- [ ] `sfec_pending_writes` est vide en régime nominal ; toute ligne restante est soit en cours de retry, soit abandonnée après 10 essais et visible dans `/pending` avec un motif.

Aucun `config.json` n'a été modifié : les valeurs recommandées (`connection_timeout_ms: 8000`, `pool_min: 2`, `pool_max: 6`) restent à appliquer sur le poste client si elles ne le sont pas déjà.
