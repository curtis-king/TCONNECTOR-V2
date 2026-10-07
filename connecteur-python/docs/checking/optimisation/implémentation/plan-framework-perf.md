# Plan d'implémentation — Framework `tests/perf/` (validé)

> Décisions validées : **seuils bloquants jour 1**, **200 factures mockées**, **mocks en mémoire**.
> Principe : 8 fichiers neufs + 2 lignes (`pytest.ini`, `.gitignore`), **zéro modification du code métier**.
> Référence : `C:\Users\tp_m2\Documents\projects\TCONNECTOR-V2\connecteur-python`.

---

## P0. Contexte réutilisé (existant, ne pas réinventer)

- `tests/conftest.py` fournit déjà (visible depuis `tests/perf/`) : `app_instance` (Flask, `TESTING=False`),
  `client` authentifié admin (vrai `POST /login` + CSRF), `admin_credentials`, init SQLite façon `main.py`.
- `tests/test_smoke.py` = modèle de style (routes, statuts acceptables).
- `pytest.ini` : `testpaths = tests`, `addopts = -q` → ajouter le marker `perf`.

---

## P1. Socle

| # | Fichier | Action exacte |
|---|---|---|
| 1.1 | `pytest.ini` | ajouter `markers =` + ligne `perf: tests de performance (TTFB, sync, DB)` |
| 1.2 | `.gitignore` (racine projet) | ajouter `tests/perf/perf-results.jsonl` + `perf.json` |
| 1.3 | `tests/perf/__init__.py` | vide |
| 1.4 | `tests/perf/thresholds.json` | `{"ttfb_ms": {"billing": 500, "pos": 400, "certified_print": 800}, "sync": {"full_sec": 60, "incremental_sec": 15}, "db": {"max_queries_per_request": 20}}` |
| 1.5 | `tests/perf/conftest.py` | fixture `thresholds` (charge le JSON) ; helper `assert_under(ms, *cles)` ; hook `pytest_runtest_makereport` (hookwrapper) → append JSONL `{test, status, duration_ms, timestamp}` dans `tests/perf/perf-results.jsonl` ; fixture `query_counter` (voir P4) |

---

## P2. Mocks en mémoire (`tests/perf/mocks/`)

Stratégie : `unittest.mock.patch(...).start()` dans une fixture **session** (teardown `.stop()`),
jamais `monkeypatch` (scope fonction uniquement). 200 factures canned `FA-PERF-0001…0200`
(dont 3 avoirs avec refs — exerce O2.1), 3 lignes/facture (exerce le batch O1), QR data-URI ~1,5 Ko réaliste.

| Cible de patch | Comportement mocké |
|---|---|
| `app.sync.engine.is_online` | `lambda: True` |
| `app.sync.engine.certify_invoice` | certif immédiate `{numero, signature 64c, qr_code, date}` — **aucun `sleep`** |
| `app.sync.engine.SfecClient` (`list_invoices`, `get_invoice`, `verify_by_invoice_number`) | 200 canned + lookup `by_numero` (sert aussi le test `print`) |
| `app.integration.sage.database.fetch_sales_invoices` (+ `fetch_purchase_invoices`, `fetch_contacts`, `fetch_tax_rates`) | 200 dicts calibrés (statut 2, `CERTIFIE` partiel) |
| `app.integration.sage.database.fetch_doc_lines` + `fetch_doc_lines_batch` | 3 lignes/facture, groupées par pièce |
| `app.integration.sage.writer.write_invoice_to_sage` | `{"success": True}` + compteur d'appels (gate doublons O2) |

---

## P3. `tests/perf/test_api_perf.py` — 3 tests TTFB

```python
@pytest.mark.perf
def test_billing_ttfb(client, thresholds): ... GET /billing ... assert_under(ms, "ttfb_ms", "billing")
def test_pos_ttfb(...)      # /pos, < 400
def test_certified_print_ttfb(...)  # /certified/<uuid-mock>/print, < 800 (lookup SFEC mocké)
```
Client = `client` authentifié de `tests/conftest.py`.

---

## P4. `tests/perf/test_sync_perf.py` + `test_db_perf.py`

- `test_full_sync_duration` : `full_sync()` < 60 s (200 mockées).
- `test_incremental_sync_duration` : `sync_incremental()` < 15 s.
- `test_auto_certify_no_duplicates` : compteur `write_invoice_to_sage` == éligibles uniques.
- `test_db_query_count` : proxy `query_counter` — wrapper `@contextmanager` autour de `get_cursor`,
  patché sur **chaque module consommateur** (liste exacte via `grep -rn "from app.storage.db import get_cursor" app/`
  + ceux qui passent par `sqlite_db.get_cursor`) ; assert < 20/requête page.
- `test_index_usage` : `EXPLAIN QUERY PLAN` des 3 requêtes tick → assert `SEARCH`/`USING INDEX`, jamais `SCAN`
  (déterministe, déjà vert en manuel le 2026-10-06).

---

## P5. CI + premier run vert

- `.github/workflows/perf.yml` : `setup-python` + `pip install -r requirements.txt` +
  `pytest tests/perf -m perf --json-report --json-report-file=perf.json` + upload
  `tests/perf/perf-results.jsonl`, `perf.json`.
- Run local `pytest tests/perf -m perf -v` vert ; si flaky : marges = mesures × 2 avant resserrage.

---

## Ordre d'exécution : P1 → P2 → P3 → P4 → P5. Estimé ~2h.

## Validation finale (gates du plan)

- [ ] `pytest tests/perf -m perf -v` vert en local
- [ ] `perf-results.jsonl` produit (1 ligne/test, `duration_ms` + seuils respectés)
- [ ] `pytest tests/ -m "not perf"` toujours vert (non-régression : `test_smoke`, `test_avoirs`)
- [ ] Miroir WSL synchronisé
