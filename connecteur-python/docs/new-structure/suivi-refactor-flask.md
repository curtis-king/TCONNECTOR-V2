# SUIVI — Refactorisation Flask (Real Python)

> Fichier de progression unique. Règles : chaque sous-agent écrit **uniquement dans sa section** (append-only), l'orchestrateur k3 met à jour le tableau de bord ci-dessous après validation de chaque mission.
> Référence : `plan-refactor-flask-realpython.md` (même dossier).

| Agent | Mission | Statut | Fin |
|---|---|---|---|
| A1 | Filet de sécurité (tests + snapshots) | ✅ | 2026-09-12 20:16 |
| A2 | Extraction Jinja (templates + static) | ✅ | 2026-09-13 (CSS/JS externalisés différés → A7) |
| A3 | Découpage parking de dashboard.py | 🔄 | — |
| A4 | Blueprints auth + dashboard + sync_api | ⏳ | — |
| A5 | Blueprint config | ⏳ | — |
| A6 | Blueprints billing + pos + directory | ⏳ | — |
| A7 | Factory réelle + clôture | ⏳ | — |

---

## A1 — Sentinel
**Statut** : ✅ TERMINÉ

**Fin** : 2026-09-12 20:16 WAT (date : `2026-09-12 20:16:38 WAT`)

**Fichiers créés** (aucun fichier de `app/` modifié, rien commité) :
- `tests/conftest.py` — fixtures : init SQLite (reprise du démarrage de `main.py`), login admin via vrai POST `/login` (CSRF inclus), clients authentifié + anonyme
- `tests/test_smoke.py` — 5 tests : import de tous les modules `app/*` (hors Windows-only), app importable, **exactement 94 routes**, routes GET sans paramètre sans 500 (authentifié + anonyme)
- `tests/snapshot_routes.py` — générateur de snapshots (exécutable)
- `tests/compare_snapshots.py` — comparateur de non-régression (exécutable, exit 0/1)
- `tests/snapshots/` — 50 snapshots JSON + `_manifest.json` + `_skipped.json`
- `pytest.ini` (`testpaths=tests`) et `requirements-dev.txt` (`pytest>=9.0`)

**Résultats des vérifications** :
- **94 routes** confirmées (`len(app.url_map._rules)`)
- **50 snapshots** générés (49 routes GET sans paramètre dont `/static/css/app.css` pour la route statique, + 1 doublon `/api/contacts` déclaré deux fois) ; **9 routes skippées** (toutes à paramètres obligatoires sans données en base SQLite — factures/tickets/vendeurs vides ; listées dans `_skipped.json`)
- **pytest : 5 passed** (exit 0)
- **compare_snapshots : 50/50 OK** sur 3 rejoues consécutifs (exit 0) — stabilité démontrée

**Choix d'auth (documenté)** : auth RÉELLE via login admin (`login_email`/`login_password` de config.json, migrés en SQLite) plutôt que désactivation — les snapshots capturent les vraies pages authentifiées, flux CSRF réel exercé.

**Choix TESTING=False** dans les clients de test : comportement production (vraies réponses 500 au lieu d'exceptions propagées) — permet de snapshotter le comportement actuel y compris les 500 pré-existants.

**Problèmes rencontrés** :
1. **Bug pré-existant `fetch_contacts`** (`app/integration/sage/database.py` l. 1034 : `tp_cols` utilisé avant affectation) → 500 sur `/api/contacts` (x2). Non corrigeable ici (interdiction de toucher `app/`) → déclaré dans `KNOWN_PRE_500` du smoke test + snapshot capturé (toute évolution sera détectée par le comparateur). **À signaler à l'orchestrateur** (fix possible en marge du refactoring).
2. `/api/sfec/certified-from-api` → 500 déterministe « SFEC API 401 : API key is required » (config sans clé) — également dans `KNOWN_PRE_500`.
3. `app.service.windows_service` non importable sous Linux (pywin32) — exclu du test d'import (comportement attendu).
4. **Contenus volatiles** dans les réponses : jeton CSRF injecté dans le HTML (`window.CSRF_TOKEN="…"`, inputs cachés, JSON `/api/csrf`), horodatages ISO `/api/metrics`, journal `/api/audit` (grossit à chaque login). → Normalisation ciblée dans `compare_snapshots.py` (contextes CSRF explicites ; canonicalisation JSON du journal ; `audit_log` vidé avant chaque capture/rejeu — données de test uniquement).

**Décisions prises** :
- Snapshots stockés en JSON (méthode, path, status, content-type, sha256, corps complet texte/base64)
- Normalisation UNIQUEMENT au moment de la comparaison (le corps stocké reste brut)
- Les routes à paramètres seront à compléter si des données réelles apparaissent (le générateur les reprendra automatiquement via la base)

**Usage pour les vagues suivantes** :
```
.venv/bin/python tests/compare_snapshots.py   # avant/après chaque étape — doit rester 50/50 OK
.venv/bin/python -m pytest                    # smoke tests — doit rester vert
```

---

## A2 — Templater
**Statut** : ✅ TERMINÉ — 2026-09-13 07:49

**Mission** : ÉTAPE 1 — câbler les templates Jinja existants dans `app/web/dashboard.py` via `render_template`, avec parité byte-exacte.

### Templates audités/corrigés (tous dans `app/web/templates/`)
- `base.html` : réécrit (squelette byte-exact, variables `css/nav/toast/csrf_json/page_label/page_key/body/content_css`) — remplace `_page()` par `_base_context()` + `render_template("base.html")`.
- `auth/password.html`, `auth/login.html`, `auth/deny.html` : `| safe` sur valeurs `_esc()`-ées.
- `dashboard/{index,invoices,pending,certified,print}.html` : newline initiale, `| safe`, formats `{:,.0f}`.
- `directory/clients.html` : document autonome — `css|safe`, `nav|safe`, `toast|safe` au lieu de link/include/js externe.
- `directory/{vendeurs,sales}.html` : formats `{:,.0f}` + newline.
- `directory/utilisateurs.html` : reproduction fidèle du bug pré-existant `{rows}` littéral (replace `@ROWS@` no-op côté Python, confirmé dans HEAD).
- `billing/list.html` : formats, jonctions de fragments sans newline, `</div>` alignés sur le HTML déséquilibré pré-existant de HEAD (stat_cards jamais fermé).
- `billing/form.html` : newline initiale, retrait d'un `}` surnuméraire (validateTiersNiu), fin sur `</datalist>\n` (le `</div>` final appartient au FOOTER).
- `billing/detail.html` : newline initiale, `| safe` sur `_esc()`-ées, formats `{:,.0f}`.
- `pos/index.html` : conforme (7 vars `| safe`, `stock_ctl_js` brut).
- `pos/ticket_print.html` : `| safe` ajouté (numero, date, caissier, client, company_name, paiement) ; total/recu/monnaie reçoivent des chaînes pré-formatées.
- `config/index.html` : `| safe` sur les 30 vars `_esc()`-ées ; `nt_t`/`nt_f` conservés pour l'onglet notifications (le Python réutilisait `en_t`/`en_f` pour SFEC ET notifications — renommés côté contexte) ; `dash_port` pour le port dashboard (doublon `port` DB/dashboard résolu ainsi).

### Routes câblées (22 `render_template`, 0 `render_template_string` — il ne reste que l'import)
Lot 0 : base + `/compte/mot-de-passe` — Lot 1 : login + deny — Lot 2 : `/`, `/invoices` — Lot 3 : `/pending`, `/certified`, `/print/<ids>` — Lot 4 : `/clients`, `/vendeurs`, `/sales`, `/utilisateurs` — Lot 5 : `/billing`, `/billing/invoice/new`, `/billing/invoice/<id>`, `/billing/invoice/<id>/edit` — Lot 6 : `/pos`, `/pos/ticket/<id>/print` — Lot 7 : `/config` (81 variables de contexte, 3 doublons renommés : `nt_t`/`nt_f`, `dash_port`).

Constantes mortes `LOGIN_HTML` et `_DENY_HTML` supprimées (non référencées, aucun effet de rendu).

### Vérifications finales
- `pytest` : **5 passed** (vert)
- `compare_snapshots.py` : **50/50 OK, 0 DIFF/ERROR**
- Routes : **94** (`len(app.url_map._rules)`)
- Pages non couvertes par snapshots, vérifiées par comparaison directe avant/après (ancienne fonction extraite de `git show HEAD`) : `/sales`, `/vendeurs`, `/utilisateurs` (MATCH), `/billing/invoice/<id>` + `/edit` avec facture riche (MATCH), `/pos/ticket/<id>/print` avec ticket+facture+branches SFEC (MATCH).

### Problèmes rencontrés / décisions
- Jinja2 supprime la newline finale (`keep_trailing_newline=False`) : ajout explicite `+ "\n"` dans `_invoice_form_page`.
- Fixture de test polluant la base partagée `data/tconnector.db` (snapshots DIFF sur /clients, /billing…) : réinitialisation de la base avant chaque comparateur ; scripts utilitaires temporaires supprimés en fin de mission (`tests/_*.py`, `tests/_ref/`).
- **CSS/JS externalisation (points 3-4 du brief) DIFFÉRÉE** : les snapshots de référence embarquent le CSS/JS inline (vérifié : snapshot 001 = 1 `<style>` inline, aucune référence `/static/…`). Passer à `url_for('static', …)` casserait le sha256 des 50 snapshots. `static_content.py` reste utilisé (`read_static("css/app.css")` → constante `CSS`). À traiter en ÉTAPE ultérieure avec régénération des snapshots.
- Convention d'échappement : valeurs `_esc()`-ées côté Python → `{{ x | safe }}` (échappement unique, parité `html.escape` garantie) ; valeurs sûres (ints, `selected`/`checked`) → `{{ x }}` brut.

### Reste à faire
Rien pour l'ÉTAPE 1. Externalisation CSS/JS à planifier séparément (avec régénération des snapshots).

---

## A3 — Splitter
**Statut** : ✅ TERMINÉ — 2026-09-13 08:23

**Mission** : ÉTAPE 3 — découpage MÉCANIQUE de `app/web/dashboard.py` en fichiers « parking » par domaine. AUCUNE logique modifiée, AUCUN blueprint créé : fonctions déplacées à l'identique (mêmes noms, mêmes corps), décorateurs retirés.

### Fichiers créés (`app/web/parking/`, ~2 220 lignes de handlers)
- `__init__.py` — docstring de présentation
- `common.py` — helpers partagés : `_esc`, `_page`, `_base_context`, `_current_page`, constantes UI (`CSS`, `SIDEBAR_ICONS/ITEMS/PERM`, `_sidebar`, `ALERT_ZONE`, `FOOTER`)
- `auth.py` — login, logout, `/compte/mot-de-passe`, `/api/compte/password` + helper `_login_csrf_field`
- `dashboard_pages.py` — `/`, `/invoices`, `/pending`, `/certified`, `/certified/<id>/print`, `/sales`, `/ready`, `/health`
- `config.py` — `/config`, `/api/config*` (12), `/api/db/*`, `/api/tables`, `/api/sfec/*` (8), `/api/tax-rates*` (2), `/api/ledger-accounts` + helper `_bool`
- `billing.py` — `/billing`, `/billing/invoice/*` (3), `/api/invoices*` (9) + helper `_invoice_form_page`
- `pos.py` — `/pos`, `/pos/ticket/<id>/print`, `/api/pos/*` (7), `/api/products*` (3) + helper `get_setting`
- `directory.py` — `/clients`, `/vendeurs`, `/utilisateurs`, `/api/contacts*` (4), `/api/vendeurs*` (5), `/api/utilisateurs*` (5) + helpers `_user_rows`, `_matrix_editor`, `_audit_rows`
- `sync_api.py` — `/api/sync*` (3), `/api/articles/sync`, `/api/connectivity`, `/api/metrics`, `/api/certified*` (2), `/api/retry-queue`, `/api/auth/*` (2), `/api/audit`, `/api/csrf`

### Fichier modifié
- `app/web/dashboard.py` : 2 423 → 461 lignes. Garde l'instance `app`, toute la sécurité (`before_request`, `after_request`, CSRF, `_login_required`, `_ACCESS_RULES`, `_MUTATE_PERMS`, `_required_permission`, `_deny`, `_is_sage_connected_user`) et les imports d'origine (namespace de délégation). En bas : 93 `app.add_url_rule(rule, endpoint, view_func, methods=[...])` via `_add_route(...)`, wrapper `_login_required(...)` appliqué exactement là où le décorateur existait (88 protégées, 5 publiques : login, logout, `/api/csrf`, `/health`, `/ready`).

### Répartition routes → parking (93 = 94 rules − route statique Flask)
| Parking | Routes |
|---|---|
| auth | 4 |
| dashboard_pages | 8 |
| config | 24 |
| billing | 13 |
| pos | 12 |
| directory | 18 |
| sync_api | 14 |

### Mécanique anti-import-circulaire (décision clé)
- Chaque parking importe explicitement flask/stdlib/moteurs + `from app.web.parking.common import …`.
- Les noms définis dans `dashboard.py` utilisés par les handlers (`_current_identity`, `_auth_enabled`, `app`, `logger`…) sont liés **en bas de chaque parking** (`x = _dashboard.x`) : sûr dans les deux ordres d'import, corps des fonctions inchangés.
- Un `__getattr__` (PEP 562) par module sert de filet, sans effet sur les globaux des fonctions.
- `from app.web.parking.common import _esc` placé **en bas** de dashboard.py (l'import en tête créait un cycle : common lie `_current_identity`/`_get_csrf_token` depuis dashboard).

### Vérifications (toutes vertes)
- `python -m compileall app/web` : propre
- `pytest` : **5 passed** (inclut test_import_all_app_modules qui importe chaque parking, et 94 routes exactes)
- `tests/compare_snapshots.py` : **50/50 OK, 0 DIFF/ERROR**
- Contrôle de parité maison (supprimé après usage) : les 93 routes de HEAD vs nouvelle url_map → **URL, endpoints, méthodes HTTP et wrappers `_login_required` strictement identiques**
- Import dans l'ordre inverse (parkings d'abord) : OK

### Décisions / notes pour A4-A6
- `/api/ledger-accounts` rangé dans `config.py` (famille tax-rates/tables Sage).
- `/api/certified*`, `/api/retry-queue`, `/api/auth/*`, `/api/audit`, `/api/csrf` rangés dans `sync_api.py` (« autres /api/* non classés ») — à réévaluer éventuellement au moment des blueprints (certified/retry-queue relèvent métier factures).
- `_user_rows` (directory) et `_is_sage_connected_user` (dashboard) sont du code mort pré-existant — déplacé/gardé tel quel, pas supprimé.
- Helpers mono-domaine rangés dans leur parking ; rien d'autre déplacé.
- RIEN commité (comme demandé).

---

## A4 — Core BP
**Statut** : ⏳ EN ATTENTE

(à remplir par l'agent)

---

## A5 — Config BP
**Statut** : ⏳ EN ATTENTE

(à remplir par l'agent)

---

## A6 — Métier BP
**Statut** : ⏳ EN ATTENTE

(à remplir par l'agent)

---

## A7 — Clôture
**Statut** : ⏳ EN ATTENTE

(à remplir par l'agent)
