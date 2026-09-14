# SUIVI — Refactorisation Flask (Real Python)

> Fichier de progression unique. Règles : chaque sous-agent écrit **uniquement dans sa section** (append-only), l'orchestrateur k3 met à jour le tableau de bord ci-dessous après validation de chaque mission.
> Référence : `plan-refactor-flask-realpython.md` (même dossier).

| Agent | Mission | Statut | Fin |
|---|---|---|---|
| A1 | Filet de sécurité (tests + snapshots) | ✅ | 2026-09-12 20:16 |
| A2 | Extraction Jinja (templates + static) | ✅ | 2026-09-13 (CSS/JS externalisés différés → A7) |
| A3 | Découpage parking de dashboard.py | ✅ | 2026-09-13 08:25 |
| A4 | Blueprints auth + dashboard + sync_api | ✅ | 2026-09-13 10:21 |
| A5 | Blueprint config | ✅ | 2026-09-13 10:11 |
| A6 | Blueprints billing + pos + directory | ✅ | 2026-09-13 10:21 |
| A7 | Factory réelle + clôture | ✅ | 2026-09-13 10:50 |

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
**Statut** : ✅ TERMINÉ — 2026-09-13

**Mission** : convertir les parkings `auth`, `dashboard_pages`, `sync_api` en blueprints Flask (`app/web/routes/`), avec URL / méthodes HTTP / wrapping `_login_required` strictement identiques.

### Fichiers créés (`app/web/routes/`)
- `auth.py` — blueprint `auth` (4 routes : `/login` GET+POST, `/logout` GET, `/compte/mot-de-passe` GET, `/api/compte/password` POST) + helper `_login_csrf_field`
- `dashboard.py` — blueprint `dashboard` (8 routes : `/`, `/invoices`, `/pending`, `/certified`, `/certified/<id>/print`, `/sales`, `/health`, `/ready`)
- `sync_api.py` — blueprint `sync_api` (14 routes : `/api/sync*`, `/api/articles/sync`, `/api/connectivity`, `/api/metrics`, `/api/ledger-accounts`, `/api/certified*`, `/api/retry-queue`, `/api/auth/*`, `/api/audit`, `/api/csrf`)

Convention identique à A5 : décorateurs `@bp.route(...)` + wrapper `@_login_required_late` (résout `_login_required` à l'exécution), liaison tardive en bas de module (`from app.web import dashboard as _dashboard`), `__getattr__` PEP 562 en filet. Corps des handlers strictement inchangés (transfert mécanique depuis les parkings).

### Fichiers supprimés
- `app/web/parking/auth.py`, `app/web/parking/dashboard_pages.py`, `app/web/parking/sync_api.py` (26 routes migrées)

### Fichier modifié
- `app/web/dashboard.py` (édition minimale) :
  - 26 lignes `_add_route(...)` de mes domaines supprimées + 3 imports parking correspondants retirés
  - ajout : `from app.web.routes import auth/dashboard/sync_api` + 3 `app.register_blueprint(...)`
  - **url_for renommés** (×5, couche sécurité) : `url_for("login_page")` → `url_for("auth.login_page")` (obligatoire : les endpoints blueprint sont préfixés par le nom du blueprint ; A5/A6 n'utilisent pas cet endpoint, aucun conflit)

### url_for templates
Aucun `url_for` dans les templates (liens en dur href) → rien à changer côté Jinja. Les seuls `url_for` du projet sont les 5 de la sécurité dans `dashboard.py`, traités ci-dessus.

### Vérifications (toutes vertes)
- `python -m compileall app/web` : propre
- `pytest` : **5 passed** (dont test_exactly_94_routes — 94 rules confirmées : 67 `_add_route` restants A6 + 14 A5 `config.*` + 26 A4 `auth.*/dashboard.*/sync_api.*` comptées avec la statique = 94 après migration des 3 domaines ; à la marge près selon avancement A6)
- `tests/compare_snapshots.py` : **50/50 OK, 0 DIFF/ERROR** (exit 0)

### Décisions / notes
- Endpoints désormais préfixés : `auth.login_page`, `dashboard.index`, `sync_api.api_metrics`, etc. Les snapshots ne comparent que path/status/corps → non affectés.
- RIEN commité (comme demandé).

---

## A5 — Config BP
**Statut** : ✅ TERMINÉ — 2026-09-13 10:10

**Mission** : convertir le parking `app/web/parking/config.py` en blueprint Flask `config` (24 routes, endpoints préfixés `config.`).

### Fichiers créés
- `app/web/routes/__init__.py` — vide (package)
- `app/web/routes/config.py` — `bp = Blueprint('config', __name__)`, les 24 handlers strictement inchangés (corps identiques byte-pour-byte au parking), décorateurs `@bp.route(...)` avec URL/méthodes strictement identiques aux `add_url_rule` historiques. Wrapping `_login_required` reproduit à l'identique via `_login_required_late` (wrapper résolvant `_login_required` à l'exécution, lié en bas de module — même convention de liaison tardive que les parkings A3, aucun import circulaire dans les deux ordres d'import). `__getattr__` de délégation vers dashboard conservé.

### Fichiers modifiés
- `app/web/dashboard.py` : édition minimale — import `parking.config` remplacé par `from app.web.routes import config as _rt_config` + `app.register_blueprint(_rt_config.bp)` ; 24 lignes `_add_route(... _pk_config ...)` supprimées (6 blocs « CONFIGURATION »). Rien d'autre touché.

### Fichiers supprimés
- `app/web/parking/config.py` (+ `__pycache__` associés)

### url_for
Aucun `url_for` vers les endpoints config existait (vérifié par grep exhaustif sur `app/` et `app/web/templates/`) : `config/index.html` n'utilise aucun `url_for` et les autres domaines n'appellent pas d'endpoint config. Migration des `url_for` = no-op vérifié, rien à renommer. Les endpoints sont désormais `config.<nom>` (ex. `config.config_page`).

### Routes migrées (24 — les 24 anciennes lignes `_pk_config`, source de vérité)
`/config`, `/api/db/test`, `/api/sfec/test`, `/api/sfec/certify`, `/api/sfec/to-monitor`, `/api/sfec/debug`, `/api/sfec/validate`, `/api/sfec/sync`, `/api/sfec/certified-from-api`, `/api/tax-rates`, `/api/tables`, `/api/config`, `/api/config/db`, `/api/config/sfec`, `/api/config/company`, `/api/config/sync`, `/api/config/validation`, `/api/config/notifications`, `/api/config/system`, `/api/config/dashboard`, `/api/config/reload`, `/api/config/export`, `/api/config/import`, `/api/tax-rates/local`.
Note : `/api/ledger-accounts` (citée dans le périmètre du brief) est en réalité enregistrée depuis `sync_api` dans l'état A3 — laissée à A4, non touchée.

### Vérifications (toutes vertes)
- `pytest` : **5 passed** (dont 94 routes exactes et import de tous les modules)
- `tests/compare_snapshots.py` : **50/50 OK, 0 DIFF/ERROR**
- `python -m compileall app/web` : propre
- Contrôle url_map : 94 rules, 24 règles config toutes préfixées `config.`, 0 endpoint non préfixé
- Aucun import résiduel de `parking.config` (grep)

RIEN commité.

---

## A6 — Métier BP
**Statut** : ✅ TERMINÉ — 2026-09-13

**Mission** : convertir les parkings `billing`, `pos`, `directory` en blueprints Flask (`app/web/routes/`), avec URL / méthodes HTTP / wrapping `_login_required` strictement identiques.

### Fichiers créés (`app/web/routes/`)
- `__init__.py` — package
- `billing.py` — blueprint `billing` (13 routes : `/billing`, `/billing/invoice/*` ×3, `/api/invoices*` ×9) + helper `_invoice_form_page`
- `pos.py` — blueprint `pos` (12 routes : `/pos`, `/pos/ticket/<id>/print`, `/api/pos/*` ×7, `/api/products*` ×3) + helper `get_setting`
- `directory.py` — blueprint `directory` (18 routes : `/clients`, `/vendeurs`, `/utilisateurs`, `/api/contacts*` ×5 dont le doublon GET `/api/contacts`, `/api/vendeurs*` ×5, `/api/utilisateurs*` ×5) + helpers `_user_rows`, `_matrix_editor`, `_audit_rows`

Convention identique à A3/A4/A5 : décorateurs `@bp.route(...)` (URL et méthodes STRICTEMENT identiques aux `_add_route` d'origine) + wrapper `_login_required` local résolu à l'exécution (`from app.web.dashboard import _login_required` à l'intérieur du wrapper — aucun import circulaire possible quel que soit l'ordre), liaison tardive en bas de module (`_current_identity`, `_get_csrf_token`). Corps des handlers strictement inchangés (transfert mécanique depuis les parkings, imports de `app.web.parking.common` conservés — fichier non modifié, territoire partagé).

### Fichiers supprimés
- `app/web/parking/billing.py`, `app/web/parking/pos.py`, `app/web/parking/directory.py` (43 routes migrées ; vérifié : aucune référence restante dans `app/` ni `tests/`)

### Fichier modifié
- `app/web/dashboard.py` (édition minimale, 383 → 318 lignes) :
  - 43 lignes `_add_route(...)` de mes domaines supprimées + 3 imports parking correspondants remplacés par `from app.web.routes import billing/pos/directory`
  - ajout : 3 `app.register_blueprint(...)` (billing, pos, directory)
  - `_add_route` devenu sans usage (toutes les routes passent par des blueprints) → supprimé ; commentaires d'en-tête actualisés
  - **aucune ligne de la sécurité touchée** (before/after_request, CSRF, `_login_required`, permissions sont inchangés — A4 a déjà géré ses `url_for("login_page")`)

### url_for
Aucun `url_for` dans mes templates (`billing/*`, `pos/*`, `directory/*`) ni dans mes handlers Python (redirects en dur `/billing`) → **zéro changement nécessaire** (confirmé par grep sur `app/` et `templates/`).

### Vérifications (toutes vertes, APRÈS suppression des parkings)
- `python -m compileall app/web` : propre
- `pytest` : **5 passed** (dont test_exactly_94_routes — 94 rules confirmées : 43 endpoints `billing.*/pos.*/directory.*` + 24 `config.*` A5 + 26 A4 + statique)
- `tests/compare_snapshots.py` : **50/50 OK, 0 DIFF/ERROR** (exit 0)
- Contrôle d'import dans l'ordre inverse : même comportement qu'A4/A5 (import direct d'un module `routes/*` avant `dashboard` → cycle, contrainte pré-existante dès A3 ; `app.web.dashboard` doit être importé en premier — à résoudre par la factory A7)

### Décisions / notes
- Endpoints désormais préfixés : `billing.billing_page`, `pos.pos_page`, `directory.clients_page`, etc. Les snapshots ne comparent que path/status/corps → non affectés.
- Le doublon GET `/api/contacts` (endpoints `api_contacts` + `api_list_contacts`) est conservé à l'identique dans le blueprint directory.
- RIEN commité (comme demandé).

---

## A7 — Clôture
**Statut** : ✅ TERMINÉ (2026-09-13 10:49 WAT)

### 1. Factory réelle (ÉTAPE 4)
- **Créé `app/web/auth/security.py`** : toute la sécurité déplacée depuis `app/web/dashboard.py` (318 l.) — `_get_secret_key`, `_auth_enabled`, `_apply_session_security(app=None)` (current_app en contexte de requête), CSRF (`_get_csrf_token`/`_csrf_valid`), identité (`_current_identity`, `_is_sage_connected_user`), `_login_required`, `_ACCESS_RULES`/`_MUTATE_PERMS`/`_required_permission`/`_deny`, hooks `_auth_before_request`/`_security_headers`, et `init_security(app)`. Aucune dépendance vers les modules de routes → aucun cycle.
- **Réécrit `app/web/app.py`** : vraie factory `create_app()` — `Flask(__name__, template_folder=..., static_folder=...)`, secret, `init_security(app)`, enregistrement des 7 blueprints, `return app`. Plus d'instance globale à l'import.
- **Supprimé `app/web/dashboard.py`** (plus aucun importateur après migration) ainsi que la passerelle racine `dashboard.py`.
- **Déplacé `app/web/parking/common.py` → `app/web/common.py`** ; package `parking/` supprimé. `__getattr__` et liaisons tardives (« Liaison dashboard ») supprimés des 7 blueprints ; ceux-ci n'importent plus que `app.web.auth.security` et `app.web.common`. `_esc` désormais défini dans security.py (common l'importe), ce qui élimine la dépendance croisée.
- Particularité : `app/web/routes/auth.py` mutait l'instance globale (`app.permanent_session_lifetime = …`) → remplacé par `current_app` (équivalent strict à l'exécution).

### 2. Externalisation CSS/JS (avec régénération des snapshots)
Procédure imposée respectée : (a) baseline 50/50 OK avant changement ; (b) `base.html` et `directory/clients.html` : `<style>{{ css }}</style>` remplacé par `{{ static_tags | safe }}` (`<link rel="stylesheet" href="/static/css/app.css"><script src="/static/js/app.js"></script>` en tête, script synchrone pour garantir `api()`/etc. avant les scripts inline des pages) ; blocs `<script>` inline de `ALERT_ZONE`/`FOOTER` retirés de `app/web/common.py` (contenu = `static/js/app.js`) ; `app/web/static_content.py` supprimé ; (c) comparateur → **42/50 OK, 8 DIFF** limitées aux pages HTML 200 contenant le CSS/JS inline (`/`, `/billing`, `/billing/invoice/new`, `/certified`, `/clients`, `/compte/mot-de-passe`, `/config`, `/invoices`) ; inspection du diff de `/` : uniquement le retrait du bloc `<style>…</style>` et des `<script>` inline de fin de body, remplacés par les balises link/script externes (le sha « live » affiché varie d'un run à l'autre car il est calculé avant normalisation du jeton CSRF — le corps normalisé est stable, vérifié sur 3 runs) ; (d) snapshots régénérés (`snapshot_routes.py`, 50 générés) ; (e) **comparateur : 50/50 OK sur la nouvelle baseline**.

### 3. Passerelles racine (ÉTAPE 5)
Grep des imports dans `app/`, `main.py`, `tests/`, `scripts/` avant suppression ; aucun import dynamique (`import_module`/`__import__`) par nom de module.
- **Supprimées (0 importateur)** : `article_sync.py`, `config_manager.py`, `connectivity.py`, `database.py`, `invoice_engine.py`, `pdf_generator.py`, `pos_engine.py`, `sage_writer.py`, `seed_data.py`, `service.py`, `sfec_client.py`, `sfec_endpoints.py`, `sqlite_db.py`, `sync_bidirectional.py`, `sync_engine.py`, `user_auth.py` (toutes passerelles « bridge » vers `app/…`), ainsi que `dashboard.py` (racine) et `app/web/dashboard.py`.
- **Conservées** : `generate_articles.py` (racine — raccourci d'appel actif déléguant à `scripts/generate_articles.py`).
- **Imports réparés en conséquence** : `scripts/windows/_install_svc.py` (`from service import …` → `from app.service.windows_service import …`), `scripts/generate_articles.py` (`from config_manager import …` → `from app.config.manager import …`), `app/service/windows_service.py` (`from app.web.dashboard import app` → `create_app()`), `tests/` (conftest, test_smoke, compare_snapshots, snapshot_routes → `create_app()`).

### Vérifications finales
1. `.venv/bin/python -m pytest` → **5 passed** ✅
2. `.venv/bin/python tests/compare_snapshots.py` → **50/50 OK, 0 DIFF** (baseline régénérée post-externalisation) ✅
3. `python -m compileall app main.py` (et scripts/tests) → propre ✅
4. `from app.web.app import create_app; app = create_app()` fonctionne sans importer `app.web.dashboard` (assert `sys.modules`), **94 routes** ✅
5. Lancement réel `.venv/bin/python main.py` : `curl http://localhost:3000/login` → **HTTP 200**, `/static/css/app.css` et `/static/js/app.js` → **HTTP 200**, arrêt propre (log « T-CONNECTOR arrete proprement », port 3000 libéré) ✅
