# Proposition 02 — Fin de la tempête de retentatives

> **Priorité** : 🔴 P0 — 11 000+ lignes de log, 22 × 409, écritures Sage inutiles.
> **Fichiers** : `app/integration/sage/database.py`, `app/sync/engine.py`, `app/integration/sage/writer.py` (409) + `scripts/`
> **Tests** : `tests/test_certification_retry.py` (nouveau)
> **Réf. rapport** : `00-rapport-analyse-logs.md` §3.2

---

## 1. Symptômes

| Preuve | Valeur |
|---|---|
| `Incoherence totaux` dans `error.log` | **14 866** pour **191** factures → ~78 tentatives/facture |
| Passes d'auto-certification | **64** en 1 h 30, `0 certifiees`, 33 ou 194 erreurs |
| `Statuts bloques remis a NULL` | 16 factures à 18:33:11, puis cycle immédiat |
| Même facture tentée 6 fois | 18:02:02,612 → 18:02:03,006 |
| `409 invoice with the same invoice number already exists` | **22** |

## 2. Causes racines

### 2.1 `reset_stuck_en_cours()` efface `ERREUR`

`app/integration/sage/database.py:1716-1732` :

```python
UPDATE {} SET SFEC_STATUT = NULL
WHERE SFEC_STATUT IN ('EN_COURS', 'ERREUR')   # <-- ERREUR efface
  AND DO_Domaine = 0
  AND SFEC_DATE_CERTIF IS NULL
  AND (SFEC_NUM_CERTIF IS NULL OR SFEC_NUM_CERTIF = '')
```

Appelée **à chaque** `auto_certify_invoices()` (`engine.py:527-529`), donc une facture marquée `ERREUR` redevient immédiatement éligible.

### 2.2 Aucun verrou sur `auto_certify_invoices()`

`sync_all()` (`engine.py:196-230`) relâche `_lock` **avant** d'appeler `auto_certify_invoices()`. Une nouvelle sync (scheduler 30 s, API, BI) peut donc démarrer pendant la certification → deux passes concurrentes sur les mêmes factures, d'où les tentatives multiples et les 409.

### 2.3 Les rejets définitifs (400/422) sont marqués `ERREUR`

`_certify_single_worker` (`engine.py:479-487`) :

```python
elif status_code in (400, 422):
    mark_certification_failed(invoice["id"], ...)   # rejouable a l'infini
```

Or un 422 est une **décision locale** (`validate_sfec_payload`) : rejouer ne changera rien tant que la donnée n'est pas corrigée. L'API SFEC, elle, **n'utilise pas 422** : ses rejets de validation sont des **400**. Les deux doivent donc être traités comme définitifs.

### 2.4 Pas de dédoublonnage

`to_certify` reçoit toutes les entrées de `invoices_snapshot`, sans filtre sur `id`/`numero`.

## 3. Correctif

### 3.1 Ne plus réinitialiser `ERREUR`

**Bloc à remplacer** (`database.py:1716-1732`) :

```python
def reset_stuck_en_cours():
    t = get_table_map()
    if not t["documents"]:
        return
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = NULL
                WHERE SFEC_STATUT IN ('EN_COURS', 'ERREUR')
                  AND DO_Domaine = 0
                  AND SFEC_DATE_CERTIF IS NULL
                  AND (SFEC_NUM_CERTIF IS NULL OR SFEC_NUM_CERTIF = '')
            """.format(t["documents"]))
            if cur.rowcount > 0:
                logger.info("Statuts bloques remis a NULL: %d factures", cur.rowcount)
    except Exception:
        pass
```

**Bloc corrigé** :

```python
def reset_stuck_en_cours(reset_errors=False):
    """Débloque les certifications interrompues.

    - `EN_COURS` : un process précédent a crashé pendant la certification ;
      ce statut doit être libéré, sinon la facture reste bloquée à vie.
    - `ERREUR` : NE DOIT PAS être remis à NULL en fonctionnement normal.
      Sinon chaque passe rejoue les mêmes factures à l'infini (audit
      2026-10-21 : 14 866 rejets pour 191 factures, 64 passes en 1 h 30).
      `reset_errors=True` est réservé à une opération d'upgrade explicite
      (script ponctuel), jamais au cycle automatique.
    """
    t = get_table_map()
    if not t["documents"]:
        return
    statuts = ("EN_COURS", "ERREUR") if reset_errors else ("EN_COURS",)
    placeholders = ", ".join(["?"] * len(statuts))
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = NULL
                WHERE SFEC_STATUT IN ({})
                  AND DO_Domaine = 0
                  AND SFEC_DATE_CERTIF IS NULL
                  AND (SFEC_NUM_CERTIF IS NULL OR SFEC_NUM_CERTIF = '')
            """.format(t["documents"], placeholders), statuts)
            if cur.rowcount > 0:
                logger.info("Statuts %s remis a NULL: %d factures",
                            "EN_COURS/ERREUR" if reset_errors else "EN_COURS",
                            cur.rowcount)
    except Exception:
        pass
```

### 3.2 Déblocage unique au démarrage

Dans `engine.py` :

```python
_startup_reset_done = False
```

**Bloc à remplacer** (`engine.py:526-529`) :

```python
    try:
        reset_stuck_en_cours()
    except Exception:
        pass
```

**Bloc corrigé** :

```python
    global _startup_reset_done
    if not _startup_reset_done:
        # Un EN_COURS ne peut provenir que d'un process interrompu : on le
        # libere une seule fois au demarrage, jamais a chaque passe.
        try:
            reset_stuck_en_cours()
        except Exception:
            pass
        _startup_reset_done = True
```

> Les `EN_COURS` créés pendant la vie du process sont libérés par la fin de la tentative (succès, erreur, ou redémarrage suivant). Pas besoin de watchdog tant que la certification est synchrone.

### 3.3 Verrou d'exécution + dédoublonnage

**Bloc à remplacer** (`engine.py:509-513`) :

```python
def auto_certify_invoices():
    config = get_config()
    sfec_cfg = config.get("sfec", {})

    if not sfec_cfg.get("enabled"):
```

**Bloc corrigé** :

```python
_certify_run_lock = threading.Lock()


def auto_certify_invoices():
    # Empeche deux passes concurrentes : sync_all() relache _lock avant
    # d'appeler la certification, donc le scheduler pouvait demarrer une
    # nouvelle sync pendant qu'une certification tournait (audit 21/10 :
    # meme facture tentee 6 fois en 400 ms -> 409 en serie).
    if not _certify_run_lock.acquire(blocking=False):
        logger.info("Auto-certification deja en cours - passe ignoree")
        return
    try:
        _auto_certify_invoices_locked()
    finally:
        _certify_run_lock.release()


def _auto_certify_invoices_locked():
    config = get_config()
    sfec_cfg = config.get("sfec", {})

    if not sfec_cfg.get("enabled"):
```

Le reste du corps de la fonction est déplacé tel quel dans `_auto_certify_invoices_locked()`.

**Dédoublonnage** — dans la boucle de sélection (`engine.py:557-566`) :

```python
    for inv in invoices_snapshot:
        if inv.get("statut_code", 0) != 2:
            continue
```

devient :

```python
    _seen_ids = set()
    _seen_numeros = set()
    for inv in invoices_snapshot:
        if inv.get("statut_code", 0) != 2:
            continue

        _id = inv.get("id")
        _num = inv.get("numero", "")
        if _id in _seen_ids:
            continue
        if _num and _num in _seen_numeros:
            continue
        _seen_ids.add(_id)
        _seen_numeros.add(_num)
```

### 3.4 400/422 → `A_SURVEILLER` avec motif

> **400** = rejet de validation de l'API SFEC (définitif). **422** = rejet local de `validate_sfec_payload` / `check_sage_totals` (définitif aussi). Aucun des deux ne doit être rejoué.

`mark_to_monitor()` (`database.py:1754`) n'accepte pas de motif. On l'étend :

**Bloc à remplacer** :

```python
def mark_to_monitor(invoice_id):
```

**Bloc corrigé** :

```python
def mark_to_monitor(invoice_id, reason=""):
```

et après l'`UPDATE` :

```python
    logger.warning("Facture mise A_SURVEILLER: %s - %s", invoice_id, reason or "verification requise")
```

**Dans `_certify_single_worker`** (`engine.py:479-487`) :

```python
        elif status_code in (400, 422):
            try:
                mark_to_monitor(
                    invoice["id"],
                    "SFEC {}: {}".format(status_code, (error_body or str(e))[:200]),
                )
            except Exception:
                pass
            return ("error_reject", numero)
```

**Dans `retry_certification_queue`** (`engine.py:170-185`) : même changement (`mark_certification_failed` → `mark_to_monitor`).

**Dans `certify_single`** (`engine.py:740-760`) : même changement.

### 3.5 Backoff mémoire sur les erreurs transitoires

Une erreur transitoire (réseau, 502, verrou Sage) ne doit **pas** être marquée `ERREUR` (sinon bloquée à vie) ni rejouée toutes les 30 s.

En tête de `engine.py` :

```python
_retry_after = {}          # numero -> timestamp absolu
_retry_attempts = {}       # numero -> nombre de tentatives consecutives
_RETRY_BACKOFF_S = (60, 300, 900, 3600)
```

Helpers :

```python
def _should_retry(numero):
    return time.time() >= _retry_after.get(numero, 0)


def _schedule_retry(numero):
    n = _retry_attempts.get(numero, 0)
    delay = _RETRY_BACKOFF_S[min(n, len(_RETRY_BACKOFF_S) - 1)]
    _retry_attempts[numero] = n + 1
    _retry_after[numero] = time.time() + delay
    logger.info("Retry SFEC %s dans %ds (tentative %d)", numero, delay, n + 1)


def _reset_retry(numero):
    _retry_attempts.pop(numero, None)
    _retry_after.pop(numero, None)
```

Dans la sélection :

```python
        if not _should_retry(_num):
            continue
```

Dans `_certify_single_worker` :

- succès → `_reset_retry(numero)` ;
- `NetworkOfflineError` → la file offline existante, pas de marquage ;
- `502` → `mark_to_monitor` **ou** laisser `NULL` + `_schedule_retry` ;
- verrou Sage (`80003`, `42000`) → laisser `NULL` + `_schedule_retry` ;
- autre exception → laisser `NULL` + `_schedule_retry`, avec un plafond (après 4 tentatives → `A_SURVEILLER`).

Exemple pour le bloc `else` :

```python
        else:
            if _retry_attempts.get(numero, 0) >= 4:
                try:
                    mark_to_monitor(invoice["id"], str(e)[:200])
                except Exception:
                    pass
            else:
                _schedule_retry(numero)
            return ("error", numero)
```

### 3.6 Reprise unique des `ERREUR` existantes (upgrade)

Le client a **177** factures `ERREUR` en base. Sans action, elles resteront bloquées après le correctif. Script ponctuel :

`scripts/reset_sfec_erreur_once.py`

```python
"""Remet a NULL les SFEC_STATUT='ERREUR' sans certification (one-shot).

A lancer UNE FOIS apres le deploiement du correctif : les factures
repassent une fois dans l'auto-certification, puis sont classees
CERTIFIE / A_SURVEILLER. Ne touche jamais aux CERTIFIE.
"""
import logging
from app.config.manager import load_config
from app.core.logging_setup import setup_logging
from app.integration.sage.database import reset_stuck_en_cours

if __name__ == "__main__":
    load_config()
    setup_logging(log_level="info")
    logging.getLogger("t-connector.reset").info("Reset ponctuel des ERREUR SFEC")
    reset_stuck_en_cours(reset_errors=True)
```

### 3.7 Journalisation

- `logger.warning` une seule fois par `(numero, motif)` et par process (`_logged_errors = set()`), sinon `logger.debug`.
- Cible : `error.log` < 100 lignes/h en régime nominal (contre 15 000 sur la fenêtre analysée).

## 4. Tests

`tests/test_certification_retry.py` :

1. **`reset_stuck_en_cours` ne touche pas `ERREUR`** : monkeypatch `get_cursor`, vérifier que le SQL exécuté ne contient que `'EN_COURS'` (et `'EN_COURS','ERREUR'` seulement avec `reset_errors=True`).
2. **Une passe concurrente est ignorée** : lancer deux threads appelant `auto_certify_invoices()` avec un `time.sleep` dans `_auto_certify_invoices_locked` ; vérifier que le 2ᵉ log `deja en cours` et qu'il n'y a qu'un appel à `certify_invoice`.
3. **Facture en double dans le snapshot** : snapshot avec 2 entrées même `numero` → un seul `certify_invoice`.
4. **400/422 → A_SURVEILLER** : `certify_invoice` lève une exception avec `status_code = 400` puis `422` → `mark_to_monitor` appelé, `mark_certification_failed` **pas** appelé, aucun nouvel appel HTTP.
5. **Backoff** : 4 erreurs transitoires → 4ᵉ appel marque `A_SURVEILLER`, les tentatives sont espacées.

## 5. Critères d'acceptation

- [ ] Après déploiement, une facture en 422 n'apparaît **qu'une fois** dans `error.log`.
- [ ] `Auto-certification terminee` ne dépasse pas `~33 erreurs` à la première passe, puis tend vers 0.
- [ ] Aucun `Statuts bloques remis a NULL` dans les logs (hors démarrage).
- [ ] 0 `409` en régime nominal.
- [ ] Les **177 `ERREUR` historiques** (clusters A et C) passent en **`CERTIFIE`** après le script one-shot ; les **12 autres factures bloquées** (clusters B et D) passent en **`A_SURVEILLER`** ; 2 restent à rapatrier.
- [ ] `error.log` < 100 lignes/h.

## 6. Risques / rollback

| Risque | Parade |
|---|---|
| Un `EN_COURS` bloqué en cours de process | Redémarrage du service (reset au démarrage) ; bouton manuel possible |
| Les 177 `ERREUR` repartent d'un coup | Le script est lancé **après** le correctif centimes, pendant une fenêtre calme |
| `A_SURVEILLER` devient un fourre-tout | Motif obligatoire + page « À surveiller » listant les raisons |
| Deux instances du service tournent | Le verrou est local au process ; ajouter un verrou fichier si le déploiement le permet |

**Rollback** : revert de `engine.py` + `database.py`. Les statuts `A_SURVEILLER` peuvent être remis à `NULL` avec `reset_stuck_en_cours(reset_errors=True)`.
