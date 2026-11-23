# Plan d'upgrade qualité — T-CONNECTOR SFEC

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/corrections/`
> **Statut** : **PROPOSÉ** — 2026-11-09 — en attente de validation avant implémentation
> **Périmètre** : qualité interne (architecture, erreurs, hygiène, sécurité,
> configurabilité, tests). **Aucun changement fonctionnel attendu** hors
> paramétrage.
> **Référence** : évaluation du 2026-11-09 — note globale **15/20**, cible
> **17–18/20**.

---

## 1. Objet

Ce document décrit l'upgrade technique à mener pour consolider le connecteur
après le correctif de reprise automatique SFEC → Sage. Il couvre les six
chantiers identifiés lors de l'évaluation :

1. découpage de `app/sync/engine.py` ;
2. découpage de `app/integration/sage/database.py` ;
3. typage des erreurs (réduction des `except Exception`) ;
4. hygiène : `datetime.utcnow`, `print()`, constantes magiques ;
5. sécurisation des identifiants SQL et revue sécurité ;
6. configurabilité (`flush`, `MAX_ATTEMPTS`, tailles de lot).

Deux chantiers complémentaires sont proposés : tests de contrat / CI (7) et
performance (8, optionnel).

**Ce document est une spécification, pas une implémentation.** Chaque lot est
indépendant, livrable et réversible.

---

## 2. Photo de départ (baseline)

Mesures relevées le 2026-11-09 sur `connecteur-python/`.

| Indicateur | Valeur | Commentaire |
|------------|--------|-------------|
| Lignes `app/` | 18 707 | hors tests, docs, scripts |
| Lignes `tests/` | 8 884 | 313 tests `def test_` |
| Suite de tests | verte | hors `perf`, `sandbox`, `integration` |
| Top fichiers | `database.py` 2 151, `engine.py` 1 821, `endpoints.py` 1 459, `pdf.py` 1 267, `storage/db.py` 1 203 | 5 fichiers = 42 % du code applicatif |
| `except Exception` | 257 | dont `database.py` 58, `engine.py` 55 |
| `except:` nu | 0 | bon point |
| `datetime.utcnow()` | 15 | déprécié Python 3.12+ |
| `print()` applicatifs | 7 | tous dans `domain/seed.py` |
| SQL avec `.format(nom_table)` | 10 | `database.py` uniquement |
| Lignes > 100 caractères | 165 | lisibilité |
| Routes HTTP | 111 | 109 + 2 (`/api/sfec/pending*`) |
| Config polling | 120 000 ms | `config.json` client ; défaut code 30 000 ms |

### 2.1 Photo après les lots 1 à 6 (2026-11-23)

Mesures reprises sur la branche `upgrade/qualite-code`.

| Indicateur | Baseline | Après | Commentaire |
|------------|----------|-------|-------------|
| Lignes `app/` | 18 707 | 19 018 | + tests d'exploitation et modules extraits |
| Lignes `tests/` | 8 884 | — | 348 tests collectés (+35) |
| Suite de tests | verte | verte | 348 passed, ~1 min (Sage isolé) |
| `engine.py` | 1 821 | 1 453 | `state`, `invoice_rules`, `retry`, `sfec_errors` extraits |
| `database.py` | 2 151 | 268 | façade ; implémentations dans `connection`, `schema`, `queries/*` |
| Plus gros module applicatif | 2 151 | 1 453 (`engine.py`) | cible ≤ 600 hors orchestration |
| `except Exception` | 257 | 238 | 19 convertis ; règle `# frontiere` documentée |
| `datetime.utcnow()` | 15 | 0 | `app/core/timeutil.py` |
| `print()` applicatifs | 7 | 0 | `seed.py` → logger |
| SQL `.format(identifiant)` | 10 | 0 | `quote_ident()` (LOT 5) |
| Routes HTTP | 111 | 101 | dont 2 d'exploitation SFEC |

### Façades déjà présentes (important)

Le découpage est **déjà amorcé** : les modules cibles existent mais ne
contiennent que des ré-exports.

- `app/integration/sage/connection.py` → ré-exporte `get_pool`, `close_pool`,
  `get_cursor`, `ping_database`.
- `app/integration/sage/schema.py` → ré-exporte `discover_tables`,
  `get_table_map`, `get_domain_config`, `ensure_sfec_columns`.
- `app/integration/sage/queries/{invoices,contacts,articles,references}.py` →
  ré-exportent les fonctions de `database.py`.
- `app/integration/sage/writer.py` est déjà autonome (618 lignes).

L'upgrade consiste donc à **déplacer les implémentations** dans ces modules et
à conserver `database.py` comme façade de compatibilité. Risque de régression
fortement réduit : les imports existants continuent de fonctionner.

---

## 3. Objectifs et critères de sortie

### 3.1 Objectifs

- Réduire la charge cognitive : aucun fichier applicatif > 600 lignes.
- Rendre les erreurs **actionnables** : une erreur = une classe = une
  politique de reprise.
- Supprimer les APIs dépréciées et les sorties console.
- Neutraliser le risque d'injection par identifiant SQL.
- Rendre les paramètres d'exploitation ajustables sans rebuild.
- Mesurer la couverture et tester les contrats réels Sage/SFEC.

### 3.2 Cibles mesurables (definition of done globale)

| Indicateur | Cible |
|------------|-------|
| Plus gros fichier applicatif | ≤ 600 lignes |
| `engine.py` / `database.py` | ≤ 400 lignes chacun (façade + doc) |
| `except Exception` | ≤ 80 (baisse ≥ 65 %) |
| `datetime.utcnow()` | 0 |
| `print()` applicatif | 0 |
| SQL construit par `.format(identifiant)` | 0 (passage par `quote_ident()`) |
| Lignes > 100 caractères | ≤ 80 |
| Suite de tests | ≥ 313 tests, toujours verte |
| Couverture | mesurée, ≥ 70 % sur `app/sync` + `app/integration` |
| Contrats Sage/SFEC | au moins 1 test de contrat par adaptateur (si accès dispo) |

---

## 4. Principes directeurs

1. **Aucun big bang.** Un lot = une branche = une PR = un rollback possible.
2. **Compatibilité ascendante.** Les façades (`engine.py`, `database.py`,
   `connection.py`, `schema.py`, `queries/*`) gardent les mêmes noms publics.
3. **Comportement inchangé par défaut.** Les nouveaux paramètres reprennent
   les valeurs actuelles (flush 30 s, lot 20, log tous les 10 essais).
4. **Tests avant déplacement.** On ajoute/renforce les tests du module avant
   de déplacer le code (filet de sécurité).
5. **Pas de refonte fonctionnelle déguisée.** Les bugs métier se traitent
   dans des lots dédiés, pas ici.

### Hors périmètre

- Refonte POS / PDF / impression.
- Changement de moteur de base ou de framework web.
- Refonte UI.
- Modification des règles de certification SFEC.

---

## 5. Lots de travail

### LOT 1 — Découper `app/sync/engine.py`

**Constat.** 1 821 lignes, 50 fonctions, 7 responsabilités : état/cache,
règles avoirs, retry, certification, écritures en attente, pull Sage,
polling. État global (`_cache`, `_retry_queue`, verrous) partagé partout.

**Cible.** Package `app/sync/` :

| Module cible | Contenu déplacé |
|--------------|-----------------|
| `app/sync/state.py` | `_cache`, `_lock`, `get_cache()`, `get_metrics()`, `polling_active()` |
| `app/sync/invoice_rules.py` | `_as_float`, `_as_bool`, `_is_credit_note`, `_has_zero_amount`, `_credit_label`, `_should_mark_credit_failure`, `_manual_links`, `_resolve_credit_parent`, `_parent_sfec_number`, `_prepare_credit_note`, `_build_credit_note_index` |
| `app/sync/retry.py` | `_retry_after`, `_retry_attempts`, `_retry_queue`, `_retry_lock`, `_should_retry`, `_schedule_retry`, `_reset_retry`, `_enqueue_for_retry`, `_process_retry_queue`, `get_retry_queue`, `_queue_uncertified_for_retry` |
| `app/sync/certification.py` | `_invalidate_sfec_lookup`, `_sfec_error_json`, `overcredit_details`, `overcredit_message`, `on_account_reference_conflict`, `_has_certification_proof`, `_needs_certification_recovery`, `_build_sfec_lookup`, `_persist_local_certification`, `_certification_result_from_invoice`, `_handle_already_certified`, `reconcile_sfec_status`, `_certify_single_worker`, `auto_certify_invoices`, `_auto_certify_invoices_locked`, `certify_single`, `mark_invoice_to_monitor`, `_log_certify_error` |
| `app/sync/pending.py` | `_queue_pending_write`, `_pending_write_done`, `_flush_pending_writes`, `flush_pending_writes` |
| `app/sync/pull.py` | `sync_all`, `sync_incremental`, `sync_sfec_invoices` |
| `app/sync/polling.py` | `_polling_loop`, `start_polling`, `stop_polling`, `apply_config` |
| `app/sync/engine.py` | **façade** : ré-exporte tous les noms publics + docstring d'orientation |

**Points d'attention.**

- `_lock` : aujourd'hui un seul verrou protège le cache. Le conserver dans
  `state.py` et l'importer ; ne pas multiplier les verrous sans analyse.
- Imports circulaires : `certification.py` → `state.py`, `retry.py`,
  `pending.py` ; `polling.py` → `pull.py` + `certification.py`. `engine.py`
  n'importe rien des autres modules (sens unique façades → implémentations).
- Tests : 12 fichiers importent `app.sync.engine`. La façade garantit zéro
  modification ; on ajoute malgré tout un test d'API publique
  (`tests/test_engine_public_api.py`) qui vérifie la présence des symboles.

**Effort.** 3–4 j/h. **Risque.** Moyen (état global), atténué par la façade.

**Implémenté le 2026-11-23 (branche `upgrade/qualite-code`).**

| Module | Contenu effectivement déplacé | Lignes |
|--------|-------------------------------|--------|
| `app/sync/state.py` | `_cache`, `_lock`, `_stop_event`, `_retry_queue`, `_retry_lock`, `_certify_run_lock`, `_retry_after`, `_retry_attempts`, `_RETRY_BACKOFF_S`, `_logged_errors`, `get_cache()`, `get_retry_queue()`, `polling_active()` et setters du thread | 74 |
| `app/sync/invoice_rules.py` | tous les helpers avoirs (`_as_float` → `_build_credit_note_index`) | 279 |
| `app/sync/retry.py` | `_should_retry`, `_has_certification_proof`, `_needs_certification_recovery`, `_schedule_retry`, `_reset_retry`, `_log_certify_error` | 47 |
| `app/sync/sfec_errors.py` | `_sfec_error_json`, `overcredit_details`, `overcredit_message`, `on_account_reference_conflict` | 70 |
| `app/sync/engine.py` | orchestration : pull, certification, écritures en attente, polling, façade des imports | 1 453 |

Choix assumé par rapport à la cible initiale : `certification.py`, `pending.py`,
`pull.py` et `polling.py` ne sont **pas** créés dans ce lot. Les tests
(`tests/test_certification_retry.py`, `test_sage_odbc_409.py`,
`test_sfec_avoirs.py`, …) remplacent directement des noms de `engine`
(`engine.certify_invoice`, `engine.SfecClient`, `engine.write_sfec_to_invoice`,
`engine._cache`, …). Déplacer l'orchestration casserait ces points
d'injection et imposerait de réécrire une dizaine de fichiers de test dans un
lot censé ne rien changer au comportement. `engine.py` reste donc la façade
d'orchestration (1 453 lignes), avec un état et des règles désormais isolés.

`invoice_rules.py` résout ses dépendances d'E/S via `_dependency(name)`, qui
lit `app.sync.engine` à l'appel : les substitutions de test continuent de
fonctionner, y compris pour les fonctions déplacées.

---

### LOT 2 — Découper `app/integration/sage/database.py`

**Constat.** 2 151 lignes, 57 fonctions : pool ODBC, découverte de schéma,
requêtes, écritures, marquages SFEC, maintenance.

**Cible.** Déplacer les implémentations dans les modules existants.

| Module cible | Contenu déplacé |
|--------------|-----------------|
| `connection.py` | `_pool*`, `_pool_lock`, `_db_lock`, `_pool_queue`, `_pool_size`, `_pool_capacity`, `_connect`, `_connect_with_retry`, `_ensure_pool`, `_healthy`, `_checkout`, `_checkin`, `_build_conn_string`, `get_pool`, `close_pool`, `get_cursor`, `ping_database` |
| `schema.py` | `_TABLE_MAP`, `_TABLE_COLUMNS`, `_safe_float`, `get_devise_map`, `_map_devise`, `_discover_columns`, `_has_col`, `_col_expr`, `_detect_sfec_columns`, `_status_label`, `_sfec_type_match`, `_sanitize_db_val`, `_refresh_sfec_columns`, `discover_tables`, `get_table_map`, `get_domain_config`, `_doc_types`, `sales_doc_types`, `credit_doc_types`, `ensure_sfec_columns`, `list_all_tables` |
| `queries/invoices.py` | `fetch_sales_invoices`, `_fetch_sales_invoices_fallback`, `_doc_lines_query_parts`, `_parse_doc_line`, `fetch_doc_lines`, `fetch_doc_lines_batch`, `fetch_purchase_invoices`, `_fetch_purchase_invoices_fallback`, `_get_tiers_col`, `_tiers_name_expr` |
| `queries/contacts.py` | `fetch_contacts`, `fetch_contact_by_code` |
| `queries/articles.py` | `fetch_articles`, `_merge_article_stock` |
| `queries/references.py` | `fetch_tax_rates`, `fetch_ledger_accounts` |
| `writer.py` | `_parse_invoice_id`, `write_sfec_to_invoice`, `reset_stuck_en_cours`, `reset_sfec_monitor`, `mark_certifying`, `mark_to_monitor`, `mark_certified`, `mark_certification_failed`, `fetch_certified_invoices` |
| `database.py` | **façade** : ré-exports historiques + note de dépréciation |

**Dépendances internes à casser.**

- Les requêtes utilisent `get_cursor`, `get_table_map`, `_TABLE_COLUMNS`,
  `_detect_sfec_columns`. Dépendance descendante `queries → schema` et
  `queries → connection`, jamais l'inverse.
- `_TABLE_MAP` / `_TABLE_COLUMNS` doivent rester **uniques** (état de
  découverte), donc rester dans `schema.py` et être importés par référence
  (`from ...schema import _TABLE_COLUMNS` sur le module, pas `from ... import
  _TABLE_COLUMNS` qui figerait l'objet si réassigné).
- `writer.py` importe déjà `database` pour certaines fonctions : inverser la
  dépendance (writer → connection/schema) et retirer l'import circulaire.

**Effort.** 4–5 j/h. **Risque.** Élevé (SQL dynamique, fallbacks), atténué par
les 313 tests et la façade.

**Implémenté le 2026-11-23 (branche `upgrade/qualite-code`).**

| Module | Contenu effectivement déplacé | Lignes |
|--------|-------------------------------|--------|
| `connection.py` | pool pyodbc (`_pool*`, `_connect*`, `_checkout`, `_checkin`, `_ensure_pool`), `_SafeRow`/`_SafeCursor`, `_sanitize_db_val`, `_server_clause`, `_build_conn_string`, `get_pool`, `close_pool`, `get_cursor`, `ping_database`, `_is_transient` | 431 |
| `schema.py` | `_TABLE_MAP`/`_TABLE_COLUMNS`/`_DOMAIN_CONFIG`/`_SFEC_COLUMNS`, `_sfec_type_match`, `ensure_sfec_columns`, `_discover_columns`, `_has_col`, `_col_expr`, `_detect_sfec_columns`, `_status_label`, `discover_tables`, `get_table_map`, `get_domain_config`, `_doc_types`/`sales_doc_types`/`credit_doc_types`, `_get_tiers_col`, `_tiers_name_expr`, `_refresh_sfec_columns`, `list_all_tables` | 367 |
| `queries/invoices.py` | `fetch_sales_invoices` (+ fallback), `fetch_purchase_invoices` (+ fallback) | 571 |
| `queries/document_lines.py` | `_doc_lines_query_parts`, `_parse_doc_line`, `fetch_doc_lines`, `fetch_doc_lines_batch` | 224 |
| `queries/certifications.py` | `fetch_certified_invoices` | 53 |
| `queries/contacts.py` | `fetch_contacts`, `fetch_contact_by_code` | 152 |
| `queries/articles.py` | `fetch_articles`, `_merge_article_stock` | 137 |
| `queries/references.py` | `fetch_tax_rates`, `fetch_ledger_accounts` (+ ré-export `list_all_tables`) | 85 |
| `values.py` | `_safe_float`, `_DEFAULT_DEVISE_MAP`, `get_devise_map`, `_map_devise` | 25 |
| `errors.py` | `PermanentSfecWriteError` ajouté à la taxonomie | 25 |
| `database.py` | **façade** : écritures/marquages SFEC + ré-exports | 268 |

Écarts assumés par rapport à la cible initiale :

- `_safe_float` et les conversions de devises vont dans `values.py` (et non
  `schema.py`) : ce sont des conversions de valeurs, pas du schéma.
- Les écritures/marquages SFEC **restent** dans `database.py` au lieu de
  `writer.py`. `writer.py` est déjà autonome et importe `database` ; y déplacer
  les écritures créerait un cycle. `database.py` reste donc la façade des
  écritures et ne fait plus que ~230 lignes de logique.
- `queries/invoices.py` est scindé en trois (`invoices`, `document_lines`,
  `certifications`) pour respecter la cible « aucun fichier > 600 lignes ».
- `queries/invoices.py` ré-expose les écritures via un `__getattr__` tardif,
  sans cycle `database → queries.invoices`.
- Un seul test a été adapté : `tests/test_sage_odbc_409.py` patche désormais
  `connection` pour les 4 tests du pool (les fonctions ont changé de module).
  Aucun comportement n'est modifié.

---

### LOT 3 — Typage des erreurs

**Constat.** 257 `except Exception`, dont 113 dans les deux fichiers les plus
critiques. Les erreurs sont classées par **message** (`_is_transient`,
marqueurs `80003`, `42000`…), ce qui est fragile et non testable proprement.
Le correctif récent a introduit `PermanentSfecWriteError` : première brique.

**Cible.** Hiérarchie d'erreurs par couche.

```python
# app/integration/sage/errors.py
class SageError(Exception): ...
class SageUnavailableError(SageError): ...      # connexion, timeout, pool
class SageLockedError(SageError): ...           # 80003, "en cours d'utilisation"
class SageSchemaError(SageError): ...           # table/colonne absente, 42000
class SageWriteConflictError(SageError): ...    # 0 ligne impactée
class PermanentSfecWriteError(SageWriteConflictError): ...  # existant, reclassé

# app/integration/sfec/errors.py
class SfecError(Exception): ...
class SfecNetworkError(SfecError): ...          # offline, timeout, 5xx
class SfecRejectedError(SfecError): ...         # 400/422 définitif
class SfecAlreadyCertifiedError(SfecError): ... # 409 exploitable
class SfecAuthError(SfecError): ...             # 401/403
```

**Mapping proposé (message actuel → classe → politique).**

| Symptôme | Classe | Politique |
|----------|--------|-----------|
| `Communication link failure`, `Login timeout`, `HYT00`, `08001` | `SageUnavailableError` | retry court 1/3 s, arrêt du lot |
| `80003`, `Cet élément est en cours d'utilisation` | `SageLockedError` | retry différé, ligne conservée |
| `Invalid column name`, `42000` | `SageSchemaError` | erreur définitive, alerte |
| `UPDATE 0 ligne` | `PermanentSfecWriteError` | ligne `blocked`, anomalie |
| SFEC 400/422 | `SfecRejectedError` | `A_SURVEILLER`, pas de retry |
| SFEC 409 + GET exploitable | `SfecAlreadyCertifiedError` | récupération, pas de retry |
| SFEC 401/403 | `SfecAuthError` | alerte config, pas de retry |
| SFEC offline/timeout/5xx | `SfecNetworkError` | retry 2/5 s, file d'attente |

**Stratégie de migration (incrémentale, sans casser).**

1. Créer les modules `errors.py` (Sage et SFEC) avec des sous-classes des
   types actuels (`ValueError` conservé pour `PermanentSfecWriteError`).
2. Convertir les points d'émission : `_connect_with_retry`, `get_cursor`,
   `write_sfec_to_invoice`, `client.SfecClient`, `endpoints.certify_invoice`.
3. Convertir les points de capture des 5 fichiers les plus critiques, dans
   l'ordre : `database.py` (58), `engine.py` (55), `pdf.py` (13),
   `routes/config.py` (10), `storage/db.py` (10).
4. Règle de revue : tout nouveau `except Exception` doit être justifié par un
   commentaire `# frontiere` (frontière de process, logging, best-effort).
5. Objectif : ≤ 80 occurrences, avec `logger.exception()` dans les frontières.

**Testabilité.** Chaque classe doit être levable par un message simulé dans un
test unitaire, sans Sage réel (les constructeurs restent compatibles avec les
messages pyodbc).

**Effort.** 3–4 j/h (progressif, peut chevaucher LOT 1/2). **Risque.** Moyen :
une erreur mal classée peut changer une politique de retry. Mitigation :
table de mapping testée + conservation temporaire des marqueurs texte.

**Implémenté le 2026-11-23 (branche `upgrade/qualite-code`).**

- `app/integration/sage/errors.py` : `SageError`, `SageUnavailableError`,
  `SageLockedError`, `SageSchemaError`, `SageWriteConflictError`,
  `PermanentSfecWriteError` (reclassé sous `SageWriteConflictError` en
  conservant `ValueError`), `classify_sage_error()` et `is_transient()`.
- `app/integration/sfec/errors.py` : `SfecError`, `SfecNetworkError`,
  `SfecRejectedError`, `SfecAlreadyCertifiedError`, `SfecAuthError`,
  `SfecHttpError`, `NetworkOfflineError`, `build_http_error()`. Les attributs
  `status_code` / `error_body` / `error_json` / `retry_after` sont conservés.
- Points d'émission convertis : `connection._connect_with_retry` (erreur
  typée, `raise ... from`), `client._request` (timeout/réseau → `SfecNetworkError`,
  statuts → sous-classe dédiée).
- Points de capture convertis dans `connection.py` (22 → 4 `except Exception`,
  les 4 restants encadrent un `yield` : rollback avant propagation) et
  `pending_writes.py` (`_is_connection_error` s'appuie sur
  `SageUnavailableError` au lieu des seuls marqueurs).
- Règle de revue adoptée : tout `except Exception` restant porte un commentaire
  `# frontiere` (process, logging, best-effort, rollback).
- Compteur global : 257 → 238. La cible ≤ 80 reste un objectif des lots
  suivants : `engine.py` (51) et `pdf.py` (13) concentrent l'essentiel.
  Aucun comportement de retry n'est modifié par défaut (table de mapping
  couverte par `tests/test_error_taxonomy.py`).

---

### LOT 4 — Hygiène et obsolescences

#### 4.1 `datetime.utcnow()` → helper unique

Créer `app/core/timeutil.py` :

```python
def utcnow() -> datetime:                 # timezone-aware
def utcnow_iso() -> str:                  # "...Z"
def to_iso(dt) -> str:
def parse_iso(value) -> datetime | None:
```

Remplacer les 15 occurrences :

| Fichier | Occurrences |
|---------|-------------|
| `app/sync/engine.py` | 3 |
| `app/sync/bidirectional.py` | 3 |
| `app/sync/connectivity.py` | 2 |
| `app/integration/sfec/endpoints.py` | 2 |
| `app/domain/pos.py` | 2 |
| `app/domain/invoices.py` | 1 |
| `app/integration/sage/writer.py` | 1 |
| `app/core/health.py` | 1 (`_parse_iso` existe déjà) |

Contrainte : le format persisté (`...Z`) doit rester **identique** pour ne pas
invalider les données `last_sync_at`, `queued_at`, etc.

#### 4.2 `print()` → `logger`

7 occurrences dans `app/domain/seed.py`. Remplacer par le logger
`t-connector.seed`. Vérifier que le script CLI (`scripts/generate_articles.py`,
`main.py --seed`) affiche toujours quelque chose : conserver un `print()` **uniquement**
dans le point d'entrée CLI, jamais dans le module métier.

#### 4.3 Constantes magiques

Centraliser dans `app/core/constants.py` ou dans les modules concernés :

- `FLUSH_MIN_INTERVAL_S = 30`
- `PENDING_FLUSH_BATCH = 20`
- `MAX_ATTEMPTS = 10` (devient paramétrable, cf. LOT 6)
- `RETRY_BACKOFF_S = (1, 3)`, `RETRY_DELAYS_S = (2, 5)`
- `SFEC_LOOKUP_TTL = 300`
- `MAX_WATCH_IN`, `MAX_PENDING_WRITES`, `MAX_MINUTES_WITHOUT_SFEC`

**Effort.** 1 j/h. **Risque.** Faible. **Tests.** Vérifier les formats de date
et les messages de log existants (snapshots).

---

### LOT 5 — Identifiants SQL et revue sécurité

#### 5.1 Identifiants SQL

**Constat.** 10 requêtes construites par `.format(tbl)` /
`.format(t["documents"])` dans `database.py`. Les noms viennent de
`INFORMATION_SCHEMA` (avec fallback `LIKE`), donc d'une source externe. Le
risque d'injection est faible mais réel : une table nommée
`F_DOCENTETE; DROP ...` (ou une base compromise) serait interpolée.

**Cible.** `app/integration/sage/sql.py` :

```python
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

def quote_ident(name: str) -> str:
    """Valide puis bracket-quote un identifiant SQL Server."""
    if not _IDENT_RE.match(name or ""):
        raise SageSchemaError("Identifiant SQL invalide: {!r}".format(name))
    return "[{}]".format(name.replace("]", "]]"))
```

Remplacer les 10 sites : `"... FROM {} ...".format(quote_ident(tbl))`.
Interdire en revue tout `.format()` sur un nom de table/colonne.

**Test.** Table de cas : noms valides (`dbo_F_DOCENTETE`, `dbo.F_DOCENTETE`),
noms invalides (`F_DOC; DROP`, `[x]`, vide) → `SageSchemaError`.

#### 5.2 Revue sécurité (constats)

Points **déjà bons** :

- `config.json` est gitignoré ; permissions `0600` appliquées à la sauvegarde ;
- surcharges d'environnement `TCONNECTOR_DB_PASSWORD`, `SFEC_API_KEY`,
  `SFEC_SANDBOX_API_KEY`, `TCONNECTOR_SESSION_SECRET` non persistées ;
- `auth_enabled=false` interdit, secret de session faible rejeté/averti ;
- routes protégées par `_login_required`, CSRF présent ;
- 0 `except:` nu ; pas de `eval/exec` détecté dans le périmètre lu.

Points à traiter dans ce lot :

1. **Secrets en clair dans `config.json`** (mot de passe DB, clés SFEC) :
   documenter la procédure de bascule vers les variables d'environnement ;
   ajouter un avertissement au démarrage si `config.json` contient un secret
   et que la variable d'environnement correspondante est absente.
2. **Sauvegardes / exports** : vérifier qu'aucun export de config ou log
   n'écrit les secrets (masquage dans `/api/config`, `/api/audit`).
3. **Identifiants SQL** : cf. 5.1.
4. **Rate limiting login** : vérifier l'existence d'un verrouillage après N
   échecs ; sinon le proposer en lot séparé (impact fonctionnel).
5. **Dépendances** : ajouter `pip-audit` en CI (LOT 7) et figer les versions.

**Effort.** 1,5–2 j/h. **Risque.** Faible (validation d'identifiants), moyen
pour la bascule des secrets (procédure d'exploitation).

---

### LOT 6 — Configurabilité

**Constat.** Les paramètres de reprise sont des constantes :
`_FLUSH_MIN_INTERVAL_S = 30`, `flush(limit=20)`, `MAX_ATTEMPTS = 10`.

**Cible.** Section `sfec` de `config.json` (et `_defaults()` dans
`app/config/manager.py`) :

| Clé | Défaut | Effet |
|-----|--------|-------|
| `sfec.pending_flush_interval_s` | `30` | throttle du rejeu |
| `sfec.pending_flush_batch` | `20` | lignes rejouées par cycle |
| `sfec.pending_log_every` | `10` | fréquence des logs d'échec transitoire |
| `sfec.lookup_ttl_s` | `300` | TTL du cache lookup SFEC |
| `sfec.lookup_max_pages` | `500` | déjà existant, à valider |
| `db.polling_interval_ms` | `120000` (client) | existant |

**Important — ne pas reproduire l'ambiguïté `MAX_ATTEMPTS`.** Le paramètre est
renommé `pending_log_every` : il ne limite **pas** les tentatives, il espace
les logs. La limite réelle est `blocked` (erreur définitive). La documentation
et l'UI doivent employer ce vocabulaire.

**Implémentation.**

- Lecture via `get_config()` à chaque cycle (pas de cache de constante) ou
  rechargement sur `apply_config()`.
- Validation : entiers positifs, bornes (`batch` 1–200, `interval` 5–3600).
- Valeurs invalides → log warning + défaut, jamais d'exception au démarrage.
- `/api/config` expose les nouvelles clés.

**Tests.** Défauts, surcharge, valeur invalide, rechargement à chaud.

**Effort.** 1 j/h. **Risque.** Faible.

---

### LOT 7 — Tests de contrat et CI

**Constat.** 313 tests, très bons sur les règles métier et les régressions,
mais majoritairement à base de mocks. Les tests `sandbox` / `integration` ne
tournent pas sans accès réseau/Sage. Aucune mesure de couverture ni pipeline
CI versionné n'a été identifié.

**Cible.**

1. **Couverture** : `pytest-cov`, rapport par module ; seuil progressif
   (60 % → 70 %) sur `app/sync` et `app/integration`.
2. **Tests de contrat SFEC** (mock HTTP enregistré ou sandbox) :
   - payload de certification (facture, avoir, exonération, centimes) ;
   - 400/409/422/5xx → classes d'erreur attendues ;
   - idempotence (`verify_by_invoice_number`).
3. **Tests de contrat Sage** (environnement dédié, marqués `integration`) :
   - découverte de schéma sur une base de test ;
   - `write_sfec_to_invoice` : 1 ligne, 0 ligne, colonne absente ;
   - verrou simulé (`80003`) → `SageLockedError`.
4. **Tests de reprise** (compléter l'existant) :
   - Sage down → up → flush automatique sans sync réussi ;
   - `blocked` après erreur définitive, puis réouverture par `enqueue` ;
   - arrêt du lot sur erreur de connexion.
5. **Tests d'API** : `/api/sfec/pending`, `/api/sfec/pending/flush`
   (auth, 202, 409 concurrence).
6. **CI** : workflow `pytest` + `pip-audit` + `ruff` (ou `flake8`) sur les
   fichiers modifiés. Pas de blocage sur le legacy au départ : seuil sur le
   diff uniquement.

**Effort.** 4–6 j/h, dépend de l'accès Sage/SFEC. **Risque.** Faible.

---

### LOT 8 — Performance (optionnel)

À ne lancer qu'après mesure, pas avant.

- Profiler un cycle complet (`sync_all`, `auto_certify`, `flush`) sur un jeu
  représentatif (ex. 1 000 factures).
- Candidats identifiés : appels ODBC séquentiels dans `_auto_certify`
  (ThreadPool existant, vérifier la taille), `fetch_doc_lines_batch` (chunk
  200), flush plafonné à 20.
- Ajouter des index SQLite si les requêtes `/pending` / `/invoices` le
  justifient.
- Ne pas optimiser sans chiffres avant/après.

**Effort.** 2 j/h. **Risque.** Faible.

---

## 6. Séquencement et effort

| Phase | Lots | Livrable | Effort | Dépend de |
|-------|------|----------|--------|-----------|
| 0 | Filet | Couverture + CI + tests de contrat minimaux | 2–3 j | accès Sage/SFEC |
| 1 | LOT 4 | Hygiène (`utcnow`, `print`, constantes) | 1 j | — |
| 2 | LOT 5 | `quote_ident()` + revue sécurité | 1,5–2 j | — |
| 3 | LOT 6 | Configurabilité | 1 j | LOT 4 |
| 4 | LOT 1 | Découpage `engine.py` | 3–4 j | phase 0 |
| 5 | LOT 2 | Découpage `database.py` | 4–5 j | phase 0 |
| 6 | LOT 3 | Typage des erreurs (progressif) | 3–4 j | LOT 1 + LOT 2 |
| 7 | LOT 7 | Contrats complets + CI | 2–3 j | phase 0 |
| 8 | LOT 8 | Performance (option) | 2 j | tout |

**Total estimé : 20–25 j/h**, hors accès externes. Les phases 1, 2 et 6 peuvent
être menées en parallèle par deux développeurs.

**Ordre recommandé :** 0 → 1 → 2 → 3 → 1 (engine) → 2 (database) → 3 (erreurs)
→ 7. Le LOT 8 est déclenché seulement si un besoin de performance est mesuré.

---

## 7. Matrice de risques

| Risque | Probabilité | Impact | Mitigation |
|--------|-------------|--------|------------|
| Régression lors du déplacement `engine.py` | Moyenne | Élevé | façade de ré-export, tests existants, PR dédiée, pas de changement de logique |
| Régression SQL lors du déplacement `database.py` | Moyenne | Élevé | tests de contrat, diff limité aux déplacements, revue ligne à ligne |
| Politique de retry modifiée par erreur | Moyenne | Moyen | table de mapping testée, conservation temporaire des marqueurs texte |
| Façades contournées par de nouveaux imports | Faible | Moyen | règle de revue + test d'API publique |
| Indisponibilité Sage/SFEC pour les tests | Élevée | Moyen | mocks enregistrés + tests `integration` marqués, non bloquants en CI |
| Bascule des secrets mal préparée | Faible | Élevé | procédure documentée, double lecture env/config, rollback immédiat |
| Fenêtre de déploiement client | Moyenne | Moyen | lots indépendants, déploiement hors heures, service Windows redémarrable |
| Dérive documentaire (doc ≠ code) | Moyenne | Faible | doc mise à jour dans la même PR, checklist |

---

## 8. Compatibilité, migration, rollback

### 8.1 Compatibilité

- **Imports** : toutes les façades conservent les noms publics. Aucun script
  client ni test ne doit être modifié.
- **API HTTP** : aucune route supprimée ou renommée.
- **Base SQLite** : aucune migration destructive. Les colonnes ajoutées sont
  nullables ou avec défaut.
- **Config** : nouvelles clés optionnelles avec défauts ; anciens
  `config.json` valides sans modification.
- **Format des dates** : inchangé (`...Z`).
- **Service Windows** : même point d'entrée `main.py`, même comportement.

### 8.2 Migration des données

Aucune migration de données n'est nécessaire. Pour mémoire, la colonne
`sfec_pending_writes.blocked` (correctif 2026-11-09) est déjà migrée
automatiquement.

### 8.3 Rollback

- Chaque lot est une PR indépendante : revert simple.
- Les modules déplacés restent accessibles via la façade : un revert partiel
  ne casse pas les imports.
- La config ajoutée est ignorée par l'ancien code (clés inconnues tolérées).
- Le rollback des secrets consiste à remettre les valeurs dans `config.json`.

---

## 9. Plan de tests et critères d'acceptation

### 9.1 Par lot

| Lot | Tests obligatoires |
|-----|--------------------|
| LOT 1 | suite complète verte + test d'API publique `engine` |
| LOT 2 | suite complète verte + tests de contrat SQL/lecture/écriture |
| LOT 3 | un test par classe d'erreur (émission + politique de retry) |
| LOT 4 | formats de dates inchangés, logs sans `print`, constantes centralisées |
| LOT 5 | `quote_ident` (valides/invalides), aucune interpolation nue restante |
| LOT 6 | défauts, surcharge, valeurs invalides, rechargement |
| LOT 7 | couverture mesurée, CI verte, contrats exécutés ou explicitement skippés |
| LOT 8 | mesure avant/après documentée |

### 9.2 Critères d'acceptation globaux

- [ ] Suite complète verte : `pytest tests --ignore=tests/perf -m "not sandbox and not integration"`.
- [ ] Tous les indicateurs du §3.2 atteints.
- [ ] Aucun changement de comportement observable hors nouvelles clés de config.
- [ ] `git diff` de chaque lot limité au périmètre annoncé (pas de reformatage massif).
- [ ] Documentation mise à jour dans la même PR.
- [ ] Rollback testé sur au moins un lot (procédure rejouée).

---

## 10. Documentation à produire / mettre à jour

| Document | Action |
|----------|--------|
| `docs/corrections/plan-upgrade-qualite-code.md` | ce document |
| `docs/corrections/solution-reprise-automatique-sfec.md` | compléter §limites (paramètres configurables) |
| `docs/incident-client-2026-10-21/08-proposition-sage-odbc-409.md` | pointer vers la nouvelle taxonomie d'erreurs |
| `docs/checking/suivi/solution-statut-certification-sfec.md` | vocabulaire `pending_log_every` / `blocked` |
| `docs/architecture.md` (à créer) | carte des modules `app/sync` et `app/integration/sage` |
| `docs/exploitation.md` (à créer) | variables d'environnement, secrets, endpoints de supervision |
| `docs/tests.md` (à créer) | stratégie de test, marqueurs, couverture, CI |

---

## 11. Décisions à valider avant implémentation

1. **Accès aux environnements** : dispose-t-on d'un Sage de test et d'un accès
   SFEC sandbox pour les tests de contrat ? Sinon, on se limite aux mocks
   enregistrés.
2. **Façades** : on conserve `engine.py` et `database.py` comme façades
   (recommandé) ou on casse les imports internes ?
3. **Typage des erreurs** : accepte-t-on une conversion progressive (marqueurs
   texte conservés temporairement) ou une conversion complète d'un coup ?
4. **Secrets** : veut-on basculer dès ce upgrade vers les variables
   d'environnement, ou seulement documenter/avertir ?
5. **CI** : où l'exécuter (GitHub Actions, serveur interne) et avec quels
   secrets ?
6. **Fenêtre de déploiement** : les lots peuvent-ils être livrés séparément
   chez le client, ou faut-il un seul lot ?
7. **Cible de couverture** : 70 % sur `sync` + `integration` est-elle
   acceptable ?

---

## 12. Annexes

### 12.1 Inventaire `engine.py` (50 fonctions)

Répartition cible :

| Domaine | Fonctions | Module |
|---------|-----------|--------|
| État/cache | 3 | `state.py` |
| Règles factures/avoirs | 11 | `invoice_rules.py` |
| Retry | 7 | `retry.py` |
| Certification | 18 | `certification.py` |
| Écritures en attente | 4 | `pending.py` |
| Pull | 3 | `pull.py` |
| Polling | 4 | `polling.py` |
| **Total** | **50** | |

### 12.2 Inventaire `database.py` (57 fonctions)

| Domaine | Fonctions | Module |
|---------|-----------|--------|
| Pool/connexion | 13 | `connection.py` |
| Schéma/découverte/utilitaires | 19 | `schema.py` |
| Requêtes factures | 10 | `queries/invoices.py` |
| Requêtes tiers | 2 | `queries/contacts.py` |
| Requêtes articles | 2 | `queries/articles.py` |
| Requêtes références | 2 | `queries/references.py` |
| Écritures/marquages | 9 | `writer.py` |
| **Total** | **57** | |

### 12.3 Exemples de conversion d'erreurs

Avant :

```python
try:
    row = _fetch_one(invoice_id)
except Exception as e:
    logger.warning("Lecture impossible: %s", e)
    return None
```

Après :

```python
try:
    row = _fetch_one(invoice_id)
except SageUnavailableError:
    raise                      # laisser remonter pour arrêt du lot
except SageLockedError as e:
    logger.warning("Verrou Sage sur %s, reprise différée: %s", invoice_id, e)
    return None
```

### 12.4 Exemple de sécurisation d'identifiant

Avant :

```sql
SELECT ... FROM {} WHERE ...
""".format(t["documents"])
```

Après :

```sql
SELECT ... FROM {} WHERE ...
""".format(quote_ident(t["documents"]))
```

### 12.5 Commandes de référence

```bash
cd connecteur-python

# suite de référence
.venv/bin/python -m pytest tests -q --ignore=tests/perf \
  -m "not sandbox and not integration"

# couverture (après LOT 7)
.venv/bin/python -m pytest tests --cov=app --cov-report=term-missing

# inventaires
grep -rc "except Exception" app --include="*.py" | sort -t: -k2 -rn
grep -rn "datetime.utcnow" app --include="*.py"
grep -rn "\.format(tbl)\|\.format(t\[" app --include="*.py"
```
