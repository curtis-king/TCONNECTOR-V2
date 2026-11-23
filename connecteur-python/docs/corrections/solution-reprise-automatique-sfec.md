# Solution — Reprise automatique des écritures SFEC → Sage

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/corrections/`
> **Statut** : IMPLÉMENTÉ — 2026-11-09
> **Périmètre** : file `sfec_pending_writes`, cycles de sync,
> auto-certification, supervision et API d'exploitation.
> **Complète** : `docs/incident-client-2026-10-21/08-proposition-sage-odbc-409.md`
> et `docs/checking/suivi/solution-statut-certification-sfec.md`.

---

## 1. Symptôme

La facture `FCDFG022` était certifiée sur SFEC, mais restait affichée
**« Certifiée SFEC — Sage en attente »** alors que Sage était de nouveau
accessible. La ligne correspondante restait indéfiniment dans
`sfec_pending_writes`, sans rejeu.

---

## 2. Cause racine

Le rejeu (`_flush_pending_writes()` → `pending_writes.flush()`) était appelé
uniquement à l'intérieur de `auto_certify_invoices()`. Or les deux fonctions
de sync sortaient **avant** cet appel dès qu'un pull Sage échouait :

```python
# app/sync/engine.py — avant correctif
def sync_all():
    with _lock:
        try:
            _cache["sales_invoices"] = fetch_sales_invoices()
            ...
        except Exception as e:
            ...
            return          # <-- auto_certify_invoices() jamais appelé

    auto_certify_invoices()  # <-- contient le rejeu
```

Trois précisions importantes par rapport au diagnostic initial :

1. **`sync_incremental()` était aussi concerné** — et c'est lui qui tourne
   presque tout le temps : `_polling_loop()` l'utilise dès qu'un premier sync
   a réussi (`engine.py`, choix `sync_incremental` / `sync_all`).
2. Le problème réel n'est donc pas « `sync_all` a échoué », mais
   **« le pull Sage a échoué, donc le rejeu n'a pas été tenté »**, quelle que
   soit la fonction de sync utilisée.
3. `MAX_ATTEMPTS` n'a jamais arrêté le rejeu : la requête `flush()` ne filtre
   pas sur `attempts`. Une erreur **transitoire** (verrou Sage, coupure) était
   donc rejouée indéfiniment — comportement voulu — mais une erreur
   **définitive** (facture absente, identifiant mort) bouclait elle aussi sans
   fin, sans alerte.

---

## 3. Règles retenues

- Le rejeu des écritures SFEC est une **tâche de fond indépendante du pull**.
  Une panne de lecture Sage ne doit jamais empêcher d'écrire une
  certification déjà obtenue.
- Une erreur **transitoire** ne fait jamais perdre la certification : la ligne
  reste rejouable sans limite et aucun `A_SURVEILLER` n'est posé.
- Une erreur **définitive** (`PermanentSfecWriteError`) bloque la ligne :
  elle est conservée pour l'audit, mais n'est plus rejouée, et une anomalie
  locale est enregistrée (`mark_to_monitor`).
- Une coupure de connexion Sage interrompt le lot en cours : inutile de
  tenter les 19 lignes suivantes, elles échoueront à l'identique.
- L'opérateur dispose d'un compteur et d'un déclencheur manuel, sans attendre
  le prochain cycle.

---

## 4. Modifications

### 4.1 `app/sync/engine.py`

- `_flush_pending_writes(force=False)` est désormais **throttlé**
  (`_FLUSH_MIN_INTERVAL_S = 30`) pour éviter deux rejeux par cycle quand
  l'intervalle de polling est court.
- Nouveau point d'entrée public `flush_pending_writes(force=True)`, utilisé
  par l'API d'exploitation.
- `sync_all()` et `sync_incremental()` appellent `_flush_pending_writes()`
  **avant** le `try` de lecture Sage : le rejeu a donc lieu même si le pull
  échoue.
- Les deux fonctions retournent `True` / `False` pour distinguer un cycle
  complet d'un cycle interrompu (les appelants historiques ignoraient la
  valeur de retour, aucun changement d'API).

### 4.2 `app/integration/sage/database.py`

- Nouvelle exception `PermanentSfecWriteError(ValueError)`, levée par
  `write_sfec_to_invoice()` pour :
  - identifiant de facture invalide ;
  - table `documents` introuvable ;
  - `UPDATE` à **0 ligne** (facture supprimée ou renumérotée).
- Le reste des erreurs (verrou `80003`, timeout, coupure réseau) reste
  transitoire.

### 4.3 `app/integration/sfec/pending_writes.py`

- Colonne `blocked INTEGER NOT NULL DEFAULT 0`, avec migration automatique
  (`ALTER TABLE`) pour les bases existantes et ajout au `CREATE TABLE` de
  `init_database()`.
- `flush()` sélectionne `WHERE COALESCE(blocked, 0) = 0`.
- Sur `PermanentSfecWriteError` : `attempts += 1`, `blocked = 1`,
  `last_error` renseigné, `mark_to_monitor()` appelé une fois, ligne
  conservée.
- Sur erreur de connexion : arrêt du lot après la première tentative.
- `enqueue()` rouvre une ligne bloquée (`blocked = 0`) si une nouvelle
  certification arrive.
- Nouveau `pending_stats()` → `{total, blocked, retrying}` ; `pending_count()`
  reste compatible.

### 4.4 `app/core/health.py`

- `pending_writes` expose le total ; nouveau champ
  `pending_writes_blocked`.
- Alerte `degraded` dès qu'une écriture est bloquée définitivement.

### 4.5 `app/web/routes/sync_api.py`

| Route | Méthode | Rôle |
|-------|---------|------|
| `/api/sfec/pending` | `GET` | `{total, blocked, retrying}` |
| `/api/sfec/pending/flush` | `POST` | Force un rejeu immédiat (202, garde anti-concurrence) |

Les deux routes sont protégées par `_login_required`, comme le reste de
`sync_api`.

---

## 5. Flux après correctif

```text
_polling_loop()
    └─ sync_all() ou sync_incremental()
         ├─ _flush_pending_writes()        <-- TOUJOURS, avant le pull
         │     └─ flush() : write_sfec_to_invoice()
         │           ├─ succès        -> DELETE de la ligne
         │           ├─ transitoire   -> attempts += 1, ligne conservée
         │           ├─ définitive    -> blocked = 1 + anomalie locale
         │           └─ connexion KO  -> arrêt du lot pour ce cycle
         ├─ pull Sage (peut échouer : le rejeu a déjà eu lieu)
         └─ auto_certify_invoices()
               └─ _flush_pending_writes() (no-op si < 30 s)
```

---

## 6. Tests

Fichier ajouté : `tests/test_pending_writes_recovery.py`.

| Cas | Vérification |
|-----|--------------|
| `sync_all` avec pull en échec | le rejeu est appelé, la fonction retourne `False` |
| `sync_incremental` avec pull en échec | idem |
| erreur définitive | ligne `blocked = 1`, `attempts = 1`, `mark_to_monitor` appelé, pas de second rejeu |
| coupure de connexion | une seule tentative pour le lot, les lignes suivantes restent à `attempts = 0` |
| point d'entrée public | `flush_pending_writes(force=True)` supprime la ligne après succès |

Le test historique `test_flush_ne_downgrade_pas_apres_dix_echecs` reste vert :
un verrou transitoire est toujours rejoué au-delà de 10 tentatives, sans
passage en anomalie.

Validation exécutée :

```bash
cd connecteur-python
.venv/bin/python -m pytest tests -q --ignore=tests/perf \
  -m "not sandbox and not integration"
```

Résultat : **suite complète verte** (0 échec).

---

## 7. Procédure d'exploitation

1. Vérifier l'état de la file :

   ```bash
   curl -s http://127.0.0.1:3000/api/sfec/pending
   # {"total": 1, "blocked": 0, "retrying": 1}
   ```

2. Fermer la facture / session Sage qui verrouille l'enregistrement.
3. Forcer un rejeu sans attendre le polling :

   ```bash
   curl -X POST http://127.0.0.1:3000/api/sfec/pending/flush
   ```

4. Contrôler dans Sage :

   ```sql
   SELECT SFEC_STATUT, SFEC_NUM_CERTIF, SFEC_DATE_CERTIF
   FROM F_DOCENTETE
   WHERE DO_Piece = 'FCDFG022';
   ```

5. Si `blocked = 1`, consulter `last_error` dans `sfec_pending_writes` et
   l'anomalie correspondante sur `/pending` : le rejeu automatique est arrêté,
   une action humaine est requise (facture supprimée, ID incohérent…).

Le rejeu automatique reprend seul au cycle suivant (`30 s` par défaut,
`db.polling_interval_ms`) dès que Sage répond. Si le polling est désactivé
(`db.polling_enabled = false`), aucun rejeu de fond n'a lieu : utiliser la
route `POST /api/sfec/pending/flush`.

---

## 8. Migration et retour arrière

- **Migration** : automatique et idempotente. `init_pending_table()` ajoute la
  colonne `blocked` si la table existe déjà ; `init_database()` la crée sur
  les installations neuves.
- **Retour arrière** : remettre les appels de rejeu dans
  `_auto_certify_invoices_locked()` uniquement, retirer les routes
  `/api/sfec/pending*` et les champs `blocked` / `pending_writes_blocked`.
  La colonne `blocked` peut rester sans effet (valeur par défaut `0`).

---

## 9. Limites connues

- Le rejeu reste **mono-processus** : une seule instance du connecteur doit
  écrire dans `sfec_pending_writes`.
- Si SQLite est indisponible, `enqueue()` échoue et l'appelant conserve son
  chemin de retry classique (comportement inchangé).
- Le throttle de 30 s est une constante ; il n'est pas encore exposé dans
  `config.json`.
