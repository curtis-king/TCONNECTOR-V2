# Rapport d'architecture & proposition de refactoring — T-CONNECTOR

> **Projet** : `connecteur-python`
> **Statut** : analyse + proposition — **restructuration exécutée** (voir §6 « Bilan de la migration »), avec écarts documentés dans `plan-restructuration.md` §8.2.
> **Objectif** : diagnostiquer la structure actuelle et proposer une organisation plus propre et intelligente, **en conservant le support Windows ET Linux**.

---

## 1. Diagnostic de la structure actuelle

### 1.1 État des lieux (chiffres)

Tout est à plat à la racine, 32 entrées mélangeant code Python, scripts Windows, migrations SQL et données de test :

| Fichier | Lignes | Problème principal |
|---|---|---|
| `dashboard.py` | **4 419** | Monolithe : HTML/CSS/JS + auth + routes + logique métier |
| `database.py` | **1 494** | Connection ODBC + découverte tables Sage + toutes les requêtes métier |
| `sync_engine.py` | 848 | Polling + cache + retries + métriques |
| `pos_engine.py` | 712 | Logique POS + produits + contacts + vendeurs |
| `sfec_endpoints.py` | 563 | Adaptation + certification SFEC |
| `sqlite_db.py` | 543 | Connexion + schéma + migrations + requêtes (accès direct partout) |
| `user_auth.py` | 461 | Auth utilisateurs + permissions + audit |
| `pdf_generator.py` | 294 | Génération PDF |
| `invoice_engine.py` | 271 | Facturation |
| `sync_bidirectional.py` | 257 | Sync bidirectionnelle |
| `generate_articles.py` | 251 | Utilitaire données de démo |
| `config_manager.py` | 188 | Chargement config |
| `sfec_client.py` | 160 | Client HTTP SFEC |
| `connectivity.py` | 153 | Vérification réseau |
| `service.py` | 137 | Service Windows (pywin32) |
| `seed_data.py` | 182 | Données de démo |
| `main.py` | 186 | Point d'entrée + orchestration threads |
| `article_sync.py` | 106 | Sync articles Sage |
| `sage_writer.py` | 217 | Écriture factures dans Sage |
| `+ 7 fichiers Windows` | — | `.bat`, `installer.iss`, `_install_svc.py`, etc. |

### 1.2 Problèmes majeurs

#### 1.2.1 `dashboard.py` — un monolithe de 4 400 lignes
Le fichier mélange **quatre responsabilités distinctes** dans un seul module :
- **Templates** : centaines de lignes de HTML/CSS/JS inline dans des chaînes Python (`CSS`, `LOGIN_HTML`, `_page`, `ALERT_ZONE`, `FOOTER`).
- **Authentification & sécurité** : `_auth_before_request`, CSRF, rôles, permissions, sessions.
- **Routes HTTP** : toutes les pages ET toute l'API REST.
- **Logique métier** : appelle directement `pos_engine`, `invoice_engine`, `sage_writer`, `sync_engine`, `sfec_*` dans les handlers.

Conséquence : impossible de tester, de maintenir, ou de réutiliser une partie sans importer le tout.

#### 1.2.2 `database.py` — de la connexion brute aux requêtes métier
Trois niveaux cobaye dans un seul fichier :
- **Connexion/pool ODBC** (`get_pool`, `_build_conn_string`, `pyodbc`).
- **Découverte du schéma Sage** (`discover_tables`, `_TABLE_MAP`, `_TABLE_COLUMNS`).
- **Logique métier**: `fetch_sales_invoices`, `fetch_purchase_invoices`, `fetch_articles`, `fetch_contacts`, `fetch_tax_rates`, `fetch_ledger_accounts`.

Le choix du driver ODBC est codé en dur (préférence Windows : "ODBC Driver 17 for SQL Server" puis "SQL Server").

#### 1.2.3 Couplage fragile, dépendances cachées
Des **re-imports locaux au milieu des fonctions** créent des dépendances implicites et cycliques :

```python
# pos_engine.py
from config_manager import get_config
from connectivity import is_online        # import tardif
from sfec_endpoints import certify_sqlite_invoice
from sage_writer import write_invoice_to_sage

# sage_writer.py
def _get_sage_cursor():
    from database import get_cursor as get_sage_cursor   # import tardif
```

Impossible de connaître le graphe de dépendances sans l'exécuter.

#### 1.2.4 Couche HTTP ↔ logique métier non séparée
`dashboard.py` sert de contrôleur ET de « glue » : les routes orchestrent directement les moteurs. Aucun **pattern contrôleur / service / repository**.

#### 1.2.5 Pas de package, artefacts Windows mélangés
- Tout est à plat à la racine : pas de `__init__.py`, pas d'organisation par domaine.
- `installer.bat`, `lancer.bat`, `start.bat`, `installer.iss`, `desinstaller.bat`, `install_service.bat`, `uninstall_service.bat` côtoient le code Python.
- `service.py` / `_install_svc.py` (pywin32, Windows-only) partagés au même niveau.

#### 1.2.6 `requirements.txt` pas multiplateforme
Contient `pywin32>=305` (Windows-only) → varie selon l'OS (déjà adressé avec `requirements-linux.txt`, mais ce n'est pas une vraie solution « officielle »).

#### 1.2.7 Duplication ET doubles implémentations
- Adaptation facture → SFEC en double : `db_invoice_to_sfec` vs `sqlite_invoice_to_sfec` (qui fait presque la même chose).
- Calculs de totaux repris entre `sqlite_db`, `pos_engine`, `invoice_engine`.
- `sqlite_db` fait à la fois du **stockage brut** et de la **logique métier** (numérotation, calculs).

#### 1.2.8 Absence de tests
Aucun test unitaire ni d'intégration. Le refactoring serait risqué sans filet de sécurité.

---

## 2. Architecture cible proposée

### 2.1 Principes directeurs

1. **Séparation des responsabilités** : Web / Domaine / Intégration / Stockage séparés.
2. **Packages au lieu d'un dossier à plat**.
3. **Découpage de `dashboard.py`** en blueprints Flask + `static/` + templates séparés.
4. **Pattern repository** pour l'accès aux données (SQLite et Sage).
5. **Préserver Windows ET Linux** : pywin32 derrière garde `sys.platform`, requirements séparés par OS, `main.py` invariant.
6. **Fabrique `create_app()`** pour rendre l'app testable.
7. **Migration progressive** : réorganisation possible sans réécrire la logique.

### 2.2 Arborescence cible

```
connecteur-python/
├── main.py                        # point d'entrée minimal (inchangé)
├── requirements.txt               # base multiplateforme
├── requirements-windows.txt       # + pywin32, waitress
├── requirements-linux.txt         # + driver ODBC / waitress
│
├── app/
│   ├── __init__.py
│   ├── config/
│   │   ├── __init__.py
│   │   └── manager.py             # ex-config_manager : chargement/validation/schéma
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── paths.py               # détection frozen / répertoires data
│   │   ├── logging_setup.py       # RotatingFileHandler, niveaux
│   │   └── utils.py               # _safe_float, safe_str, normalization
│   │
│   ├── domain/                    # logique métier PURE (aucun HTTP, pas de pyodbc)
│   │   ├── __init__.py
│   │   ├── invoices.py            # ex-invoice_engine
│   │   ├── pos.py                 # ex-pos_engine (tickets, produits, vendeurs)
│   │   ├── products.py
│   │   ├── contacts.py
│   │   ├── vendeurs.py
│   │   └── tax.py
│   │
│   ├── storage/                   # PERSISTANCE (repositories)
│   │   ├── __init__.py
│   │   ├── db.py                  # connexion + schéma + migrations (ex-sqlite_db)
│   │   ├── repo_invoices.py
│   │   ├── repo_products.py
│   │   ├── repo_contacts.py
│   │   ├── repo_vendeurs.py
│   │   └── repo_pos.py
│   │
│   ├── integration/               # SOURCES EXTERNES
│   │   ├── __init__.py
│   │   ├── sage/
│   │   │   ├── __init__.py
│   │   │   ├── connection.py      # pool ODBC (le SEUL à importer pyodbc)
│   │   │   ├── schema.py          # discover_tables / _TABLE_MAP
│   │   │   └── queries/
│   │   │       ├── __init__.py
│   │   │       ├── invoices.py    # fetch_sales/purchase_invoices
│   │   │       ├── articles.py
│   │   │       ├── contacts.py
│   │   │       └── references.py  # tax rates, chart of accounts
│   │   └── sfec/
│   │       ├── __init__.py
│   │       ├── client.py          # ex-sfec_client (HTTP natif)
│   │       ├── payload.py         # adaptation facture → payload SFEC (unique)
│   │       └── certification.py   # ex-sfec_endpoints
│   │
│   ├── sync/
│   │   ├── __init__.py
│   │   ├── engine.py              # ex-sync_engine (polling + cache + retries)
│   │   ├── bidirectional.py       # ex-sync_bidirectional
│   │   ├── scheduler.py           # orchestration threads (extrait de main.py)
│   │   └── connectivity.py        # ex-connectivity
│   │
│   ├── web/                       # COUCHE HTTP (Flask)
│   │   ├── __init__.py
│   │   ├── app.py                 # fabrique create_app() + enregistrement blueprints
│   │   ├── auth/
│   │   │   ├── __init__.py
│   │   │   ├── routes.py          # login/logout
│   │   │   ├── security.py        # CSRF, before_request, permissions, headers
│   │   │   └── user_auth.py       # ex-user_auth (logique utilisateurs)
│   │   ├── routes/                # blueprints par domaine
│   │   │   ├── __init__.py
│   │   │   ├── invoices.py
│   │   │   ├── pos.py
│   │   │   ├── clients.py
│   │   │   ├── config.py
│   │   │   ├── certified.py
│   │   │   └── ...
│   │   ├── templates/             # sortis du Python
│   │   │   ├── base.html
│   │   │   ├── dashboard.html
│   │   │   └── login.html
│   │   └── static/
│   │       ├── css/app.css
│   │       └── js/app.js
│   │
│   └── service/                   # points d'entrée
│       ├── __init__.py
│       ├── cli.py                 # CLI commune Linux/Windows
│       └── windows_service.py     # ex-service.py (+ _install_svc) isolé Windows
│
├── scripts/
│   ├── windows/                   # .bat, installer.iss regroupés ici
│   │   ├── start.bat
│   │   ├── install_service.bat
│   │   └── ...
│   └── sql/
│       └── add-sfec-columns.sql   # migrations (ex-sql/)
│
├── data/                          # runtime (gitignoré)
│   └── output.log / tconnector.db
│
└── docs/
    └── new-structure/             # ce rapport
```

### 2.3 Ce que chaque couche fait / ne fait pas

| Couche | Contient | N'interdit/s'appuie pas sur |
|---|---|---|
| `web/` (Flask) | Routes, auth, templates | Aucune logique métier, aucun accès direct aux bases |
| `domain/` | Règles métier pures | Flask, pyodbc, HTTP |
| `storage/` | Requêtes SQLite | Logique métier |
| `integration/sage` | **Seul** à importer `pyodbc` | — |
| `integration/sfec` | Communication HTTP SFEC | — |
| `sync/` | Orchestration & threads | — |

### 2.4 Gestion Windows / Linux

- **`requirements`** :
  - `requirements.txt` : base multiplateforme (`pyodbc`, `requests`, `flask`, `cryptography`, `reportlab`, `waitress`).
  - `requirements-windows.txt` : `-r requirements.txt` + `pywin32`.
  - `requirements-linux.txt` : `-r requirements.txt` (le driver ODBC se gère via `apt`, pas pip).
- **`pywin32` / `servicemanager`** : importés **uniquement** dans `app/service/windows_service.py`, chargé à la volée avec garde `sys.platform == "win32"`. `main.py` n'importe jamais pywin32.
- **`main.py`** : comportement identique sur les deux OS ; appelle `scheduler` pour les threads.

### 2.5 Stratégie de migration progressive (sans réécrire la logique)

1. **Découper `database.py`** → déplacer fonctions dans `integration/sage/*` sans changer les signatures. Les autres modules continuent d'importer les mêmes noms.
2. **Découper `dashboard.py`** → extraire le CSS/JS dans `static/`, puis créer des blueprints en remplaçant les `@app.route` par des `@bp.route`, garder `_page`/`_sidebar` comme helpers d'un module `layout`.
3. **Découper `sqlite_db.py`** → connexion + schéma dans `storage/db.py`, requêtes dans les repositories.
4. **Créer la fabrique `create_app()`** et un `main.py` qui ne fait qu'appeler `create_app()` + `scheduler.start()`.
5. **Regrouper les scripts** Windows dans `scripts/windows/` et les SQL dans `scripts/sql/`.
6. **Ajouter des tests** au fur et à mesure (pytest) sur le domaine et les repositories (SQLite en mémoire), pour sécuriser la refonte.

---

## 3. Bénéfices attendus

- **Maintenabilité** : chaque fichier a un rôle unique, taille raisonnable.
- **Testabilité** : domaine + stockage testables sans Flask ni SQL Server.
- **Portabilité** : `pywin32` isolé → `pip install -r requirements.txt` fonctionne partout.
- **Lisibilité** : fini le monolithe de 4 400 lignes.
- **Évolutivité** : ajouter une route, un fournisseur de données ou une intégration ne touche qu'une couche.

---

## 4. Risques & recommandations

- **Risque principal** : le refactoring de `dashboard.py` (4 400 lignes) est le plus délicat. Il faut le faire **par petits commits** et vérifier chaque page après découpage.
- **Prérequis** : écrire quelques tests de smoke (l'app démarre, les routes répondent) avant de toucher à quoi que ce soit.
- **Ne pas tout faire d'un coup** : la réorganisation est possible en conservant chaque étape fonctionnelle (pas de « big bang » ).

---

## 5. Conclusion

L'application fonctionne, mais sa structure ne reflète pas sa complexité : un monolithe HTTP omniprésent (`dashboard.py`), une couche d'accès aux données mélangée à l'intégration Sage (`database.py`), des dépendances cachées et une absence totale de séparation des responsabilités et de tests.

La cible proposée — **Web / Domaine / Stockage / Intégration / Sync** — ramène de la clarté, améliore la testabilité et la portabilité, **tout en gardant le support Windows et Linux**. La migration peut se faire progressivement, par couches, sans réécrire la logique métier ni casser l'existant.

---

## 6. Bilan de la migration (session de restructuration)

### 6.1 Ce qui a été fait

Tous les monolithes ont été déplacés vers `app/`, avec modules « passerelle » à la racine conservés pour ne rien casser :

| Couche | Emplacement cible | Fichiers créés |
|---|---|---|
| config | `app/config/manager.py` | ✅ + passerelle racine |
| core | `app/core/{paths,logging_setup,utils}.py` | ✅ |
| storage | `app/storage/db.py` | ✅ + passerelle racine |
| domain | `app/domain/{invoices,pos,pdf,seed}.py` | ✅ + passerelles racines |
| integration/sage | `app/integration/sage/{database,writer,connection,schema}.py` + `queries/` | ✅ + passerelle racine |
| integration/sfec | `app/integration/sfec/{client,endpoints,payload,certification}.py` | ✅ + passerelles racines |
| sync | `app/sync/{engine,bidirectional,connectivity,article_sync,scheduler}.py` | ✅ + passerelles racines |
| web | `app/web/{dashboard,app,static_content}.py`, `app/web/auth/user_auth.py`, `static/css/app.css` | ✅ + passerelle racine `dashboard.py` |
| service | `app/service/windows_service.py` | ✅ + passerelle racine |
| scripts | `scripts/windows/` (6 .bat + installer.iss + _install_svc.py), `scripts/sql/` | ✅ |
| requirements | `requirements.txt`, `requirements-windows.txt`, `requirements-linux.txt` | ✅ |

### 6.2 Points notables

- **Bug latente corrigé** : deadlock dans `app/config/manager.py` (le `load_config()` appelait `save_config()` sous un `threading.Lock()` non réentrant) → changé en `RLock()`.
- **Découpage des imports internes `app/`** : les modules du package pointent désormais les uns vers les autres via chemins package (`app.config.manager`, `app.storage.db`, `app.integration.sage.*`, `app.sync.*`, `app.domain.*`, `app.web.auth.*`), et plus via les passerelles racine.
- **Paths unifiés** : la détection `sys.frozen`/`__file__` a été centralisée dans `app/core/paths.py` (config, sqlite, sfec_endpoints, service, main).
- **`main.py`** réécrit pour s'appuyer sur `create_app()` et `app/sync/scheduler.py`.

### 6.3 Limitations / non validé (environnement sans pip/venv)

- `python3.14` n'a pas de venv/pip installables sur ce poste (sudo bloqué) → **aucune exécution réelle** (pas de Flask/pyodbc/reportlab).
- Validation effectuée : `py_compile`/`compileall` sur tout le projet (vert) + imports des modules stdlib-only (config, storage, connectivity, static_content).
- `dashboard.py` reste un gros module (~4 220 lignes) : le CSS a été extrait, mais **pas encore découpé en blueprints** (cf. plan §8.2 écarts 1 et 2).
- Tests pytest non écrits (nécessitent un environnement installable).

### 6.4 Pour reprendre

```bash
sudo apt install python3.14-venv
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-linux.txt
python main.py            # dashboard sur :3000
pytest                    # une fois les tests écrits
```
