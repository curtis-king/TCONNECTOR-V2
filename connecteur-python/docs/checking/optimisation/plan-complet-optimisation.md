# Plan Complet d'Optimisation — T-Connector

> **Date** : 2026-09-24  
> **Projet** : `connecteur-python` (copie Windows : `C:\Users\tp_m2\Documents\projects\TCONNECTOR-V2\connecteur-python`)  
> **Objectif** : Éliminer les 2 min par requête, tenir la charge "grandes boîtes" (5k+ factures sync), tests de performance automatisés et reproductibles partout.

---

## 0. État initial (constats)

| Symptôme | Cause racine |
|---|---|
| 2 min / requête | N+1 Sage (1001 req/1000 factures) + double lock + SFEC 54s/facture + probe 17s + 8 threads bloqués |
| `/billing` lent | 15 scans SQLite par affichage |
| Auto-certif | O(n²) + doublons POST SFEC |
| DB | Pas d'index composites, `date()` tue les index, `SELECT *` |
| SFEC | Pagination non bornée, probe à chaque appel, certif sync dans la page |
| Concurrency | 1 connexion ODBC + 1 lock global + 1 SQLite partagée |

---

## 1. Optimisations Code (Phases O1→O4)

### O1 — Batch Sage + Index hoist + TOP ventes  (**DONE**)

| Fichier | Changement |
|---|---|
| `app/integration/sage/database.py` | Nouvelle `fetch_doc_lines_batch(domaine, keys, chunk_size=200)` + 4 boucles utilisent `_lines_map` au lieu de `fetch_doc_lines` unitaire |
| `database.py:678,770,1067,1144` | `idx = {c:i for i,c in enumerate(columns)}` hoisté (plus de `columns.index`) |
| `database.py:549` | `limit_clause = "TOP 1000"` par défaut sur ventes |

**Gate** : Requêtes ODBC `1001 → ~3` (log `t-connector.db`), sync ÷10.

---

### O2 — Auto-certif O(n) + dédupe + batch articles  (**DONE**)

| Fichier | Changement |
|---|---|
| `app/sync/engine.py:542-552` | `by_numero`, `avoir_ids_par_ref` indexés avant boucle |
| `engine.py:584,591` | `by_numero.get(ref)` + `avoir_ids_par_ref.get(ref)` au lieu de `next()/any()` |
| `app/sync/article_sync.py` | `_upsert_products` : préchargement `ref IN (...)` chunk 500 + `executemany` |
| `database.py:1443-1451` | `_merge_article_stock` : `by_ref={}` puis 1 passe |

**Gate** : 0 doublon `to_certify` (log), import 10k articles sans `SELECT` par article.

---

### O3 — Pack index SQLite + sargable + LIMIT + 60s  (**DONE**)

| Action | Fichier/Ligne |
|---|---|
| 27 indexes composites `IF NOT EXISTS` | `storage/db.py:302-309` (dans `SCHEMA`) |
| Index `utilisateurs(lower(email))` | `user_auth.py:135-136` (hors SCHEMA car table créée plus tard) |
| `date(date_ticket)=date('now')` → plage | `pos.py:416,419,425` |
| `strftime(...)` → plage mois | `billing.py:53` |
| 5 `LIMIT` (contacts 200, push 100, certify 50, POS 1000, SFEC 500) | `bidirectional.py:55,85,217`, `pos.py:107`, `invoices.py:264/271` |
| `polling_interval_ms: 30000 → 60000` | `config.json:16` |
| Bi-sync ≥ 60s | config + restart |

**Gate** : `EXPLAIN QUERY PLAN` → `SEARCH USING INDEX`, tick bi-sync < 1s sur 1k factures.

---

### O4 — Agrégation `/billing` + colonnes explicites + join  (**DONE**)

| Sous-phase | Changement |
|---|---|
| O4.1 `count_invoices()` | 1 requête `SUM(CASE...)` SQLite (ta version `SUM(condition)`) |
| O4.2 `billing_page()` | 7 requêtes supprimées → `stats = invoice_engine.count_invoices()` |
| O4.3 Colonnes explicites (46 cols, exit QR/signature/notes) | `invoices.py:250`, `bidirectional.py:24/82/213` |
| O4.4 `+=` → `join()` | 1/17 spots (`directory.py:75`), reste dette marginale |

**Gate** : `/billing` 15 scans → 1, payload ÷3, TTFB < 500 ms.

---

## 2. Framework de Tests de Performance Automatisé

### Structure
```
tests/perf/
├── __init__.py
├── conftest.py              # fixtures + mesure + écriture jsonl
├── test_api_perf.py         # TTFB endpoints
├── test_sync_perf.py        # durées sync + comptage requêtes
├── test_db_perf.py          # requêtes SQLite, index usage
├── thresholds.json          # seuils (versionné)
├── perf-results.jsonl       # sortie (gitignore)
└── mocks/
    ├── sfec.py              # mock SFEC client
    └── sage.py              # mock Sage database
```

### `thresholds.json` (versionné)
```json
{
  "ttfb_ms": {"billing": 500, "pos": 400, "certified_print": 800},
  "sync": {"full_sec": 60, "incremental_sec": 15},
  "db": {"max_queries_per_request": 20}
}
```

### `conftest.py` — fixtures + mesure + JSONL
- Fixtures : `app`, `client`, `auth_headers` (login 1× session)
- Mocks SFEC/Sage (session) : réponses déterministes, pas de réseau
- Mesure temps : `pytest_runtest_setup` + `pytest_runtest_makereport` → `perf-results.jsonl`
- Mocks SFEC : certif immédiate, QR mock, pas de sleep
- Mocks Sage : SQLite en mémoire avec schéma minimal

### Tests

| Fichier | Tests | Seuils |
|---|---|---|
| `test_api_perf.py` | `test_billing_ttfb`, `test_pos_ttfb`, `test_certified_print_ttfb` | TTFB < 500/400/800 ms |
| `test_sync_perf.py` | `test_full_sync_duration` (< 60s), `test_incremental_sync_duration` (< 15s), `test_auto_certify_no_duplicates` | seuils `thresholds.json` |
| `test_db_perf.py` | `test_db_query_count` (< 20 requêtes/requête), `test_index_usage` (EXPLAIN QUERY PLAN → SEARCH USING INDEX) | seuils `thresholds.json` |

### Seuils `thresholds.json`
```json
{
  "ttfb_ms": {"billing": 500, "pos": 400, "certified_print": 800},
  "sync": {"full_sec": 60, "incremental_sec": 15},
  "db": {"max_queries_per_request": 20}
}
```

### Sortie & CI
- Sortie : `tests/perf/perf-results.jsonl` (gitignore)
- CI : GitHub Actions → `pytest tests/perf -m perf --json-report --json-report-file=perf.json`
- Artefact upload : `perf-results.jsonl` + `perf.json`

### Lancement
```bash
# Local
pytest tests/perf -m perf -v

# CI
pytest tests/perf -m perf --json-report --json-report-file=perf.json
```

---

## 3. Gates de Validation Globaux

| Niveau | Métrique | Cible |
|---|---|---|
| **Sync 5k factures** | Temps total | < 2 min (vs 17 min avant) |
| **Page `/billing`** | TTFB | < 500 ms |
| **Page `/pos`** | TTFB | < 400 ms |
| **Impression certifiée** | TTFB | < 800 ms |
| **Sync full** | Durée | < 60 s |
| **Sync incrémental** | Durée | < 15 s |
| **Requêtes DB / requête HTTP** | Count | < 20 (après O3/O4) |
| **EXPLAIN QUERY PLAN** | 3 requêtes tick bi-sync | `SEARCH ... USING INDEX` (pas `SCAN`) |
| **Logs ODBC** | Sync 5k factures | `1001 → ~3` requêtes |

---

## 4. Dette Résiduelle (post-lancement)

| Item | Priorité | Effort |
|---|---|---|
| R11 Export/Import config (merge + backup + masquage) | Haute | Moyenne |
| Étape 4 NIU legacy (tolérance lecture) | Moyenne | Faible |
| O4.4 `join()` restants (16/17) | Basse | Faible |
| `idx_invoices_numero_source` utilisation dans `writer.py` | Basse | Faible |
| CI perf dashboard (Grafana/GitHub) | Moyenne | Moyenne |

---

## 5. Checklist de Livraison (Definition of Done)

- [ ] `compileall` OK
- [ ] `pytest tests/ -m perf -v` vert
- [ ] `pytest tests/perf -m perf -v` vert
- [ ] `EXPLAIN QUERY PLAN` 3 requêtes tick → `SEARCH USING INDEX`
- [ ] Logs ODBC `1001 → ~3` sur sync 1k
- [ ] TTFB `/billing` < 500ms, `/pos` < 400ms, `/certified/.../print` < 800ms
- [ ] Sync 5k factures < 2 min
- [ ] `perf-results.jsonl` produit en CI
- [ ] `thresholds.json` + `perf-results.jsonl` dans artefacts CI

---

## 5. Commandes de Validation Rapide

```bash
# Validation locale complète
python -m compileall -q app
pytest tests/ -m perf -v
pytest tests/perf -m perf -v

# Gates manuels
sqlite3 data/tconnector.db "EXPLAIN QUERY PLAN SELECT * FROM invoices WHERE source IN ('web','pos') AND synced_sage = 0 AND statut != 'brouillon' ORDER BY date_facture ASC;"
# → SEARCH ... USING INDEX idx_invoices_push_sage

grep -c "ODBC.*SELECT" data/output.log   # après sync 1k factures → ~3

curl -s -o /dev/null -w "TTFB:%{time_starttransfer}\n" -b "session=<cookie>" http://localhost:3000/billing
# < 500 ms
```

---

## 6. Références & Fichiers Connexes

| Doc | Chemin |
|---|---|
| Plan Big-O détaillé | `docs/corrections/plan-optimisation-bigO.md` |
| Deep research 2 min | `docs/corrections/recherche-perf-requetes-2min.md` |
| Plan push Sage | `docs/integration/sage/plan-correction-push-sage.md` |
| Corrections pages | `docs/corrections/rapport-correctifs-pages-restants.md` |
| Phases O1-O4 progress | `docs/integration/progress/phase-*.md` |
| Seuils perf | `tests/perf/thresholds.json` |
| Résultats CI | `tests/perf/perf-results.jsonl` (gitignore) |

---

**Fin du plan** — prêt à exécuter en mode build quand tu valides.