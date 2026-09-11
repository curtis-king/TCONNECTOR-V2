# Plan de restructuration — T-CONNECTOR (`connecteur-python`)

> **Dossier** : `docs/new-structure/`
> **Statut** : PLAN VALIDÉ — restructuration exécutée (étapes 0 à 11), voir §8 « Avancement ».
> **Contrainte** : conserver le support **Windows ET Linux**, ne pas réécrire la logique métier.
> **Stratégie** : migration **progressive par étapes**, chaque étape laisse l'application fonctionnelle. Utilisation de packages `app/` + mise en place de **tests** en parallèle pour sécuriser.

---

## 0. Règles de conduite de la restructuration

1. **Chaque étape est fonctionnelle** : on commite après chaque étape validée ; jamais de « big bang ».
2. **Aucune réécriture de logique métier** : on **déplace** et on **renomme** les fonctions, on ne recode pas les requêtes SQL ni les calculs.
3. **Compatibilité des imports** : tant que la migration n'est pas terminée, on maintient des modules « passerelle » à la racine qui ré-exportent les nouveaux symboles, pour ne rien casser.
4. **Univers Windows/Linux** : `pywin32` isolé derrière une garde `sys.platform` ; `requirements-*.txt` séparés ; `main.py` invariant.
5. **Tests avant/après** : on ajoute des tests de smoke puis des tests unitaires sur le domaine/stockage.
6. **Pas de commentaires superflus** : on respecte le style du code existant (docstrings explicites si utiles).

---

## 1. Objectifs

- Découper les monolithes (`dashboard.py` 4419 l., `database.py` 1494 l., `sqlite_db.py` 543 l.).
- Séparer **Web / Domaine / Stockage / Intégration / Sync**.
- Isoler `pywin32` et les scripts Windows.
- Rendre l'application testable (`create_app()`).
- Garder `main.py`, le dashboard et toutes les routes **identiques en comportement**.
- Conserver la compatibilité **Windows + Linux**.

---

## 2. Arborescence cible (rappel)

```
connecteur-python/
├── main.py                        # point d'entrée (comportement inchangé)
├── requirements.txt               # base multiplateforme
├── requirements-windows.txt       # -r requirements.txt + pywin32
├── requirements-linux.txt         # -r requirements.txt (driver ODBC via apt)
│
├── app/
│   ├── __init__.py
│   ├── config/manager.py          # <- config_manager.py
│   ├── core/
│   │   ├── paths.py               # détection frozen / répertoires
│   │   ├── logging_setup.py       # setup_logging()
│   │   └── utils.py               # _safe_float, safe_str, normalize_date...
│   ├── domain/
│   │   ├── invoices.py            # <- invoice_engine.py
│   │   ├── pos.py                 # <- pos_engine.py
│   │   ├── products.py
│   │   ├── contacts.py
│   │   ├── vendeurs.py
│   │   └── tax.py
│   ├── storage/
│   │   ├── db.py                  # connexion + schéma + migrations <- sqlite_db
│   │   ├── session.py             # get_cursor / connexion WAL
│   │   └── repos/
│   │       ├── invoices.py
│   │       ├── products.py
│   │       ├── contacts.py
│   │       ├── vendeurs.py
│   │       └── pos.py
│   ├── integration/
│   │   ├── sage/
│   │   │   ├── connection.py      # pool pyodbc (SEUL import de pyodbc)
│   │   │   ├── schema.py          # discover_tables / _TABLE_MAP
│   │   │   └── queries/
│   │   │       ├── invoices.py    # fetch_sales/purchase_invoices
│   │   │       ├── articles.py
│   │   │       ├── contacts.py
│   │   │       └── references.py  # tax_rates, ledger_accounts
│   │   └── sfec/
│   │       ├── client.py          # <- sfec_client.py
│   │       ├── payload.py         # db_invoice_to_sfec (fusion unique)
│   │       └── certification.py   # <- sfec_endpoints.py
│   ├── sync/
│   │   ├── engine.py              # <- sync_engine.py
│   │   ├── bidirectional.py       # <- sync_bidirectional.py
│   │   ├── connectivity.py        # <- connectivity.py
│   │   └── scheduler.py           # orchestration threads (extrait de main.py)
│   ├── web/
│   │   ├── app.py                 # create_app() + blueprints
│   │   ├── auth/                  # user_auth + security + login
│   │   ├── routes/                # blueprints par domaine
│   │   ├── templates/             # HTML extrait
│   │   └── static/
│   │       ├── css/app.css
│   │       └── js/app.js
│   └── service/
│       ├── cli.py                 # CLI commune
│       └── windows_service.py     # <- service.py (pywin32 isolé)
│
├── scripts/
│   ├── windows/                   # .bat, installer.iss regroupés
│   └── sql/                       # add-sfec-columns.sql + futures migrations
│
├── data/                          # runtime (gitignoré)
├── tests/                         # pytest
├── docs/new-structure/            # ce plan + rapport
└── requirements.txt / -linux.txt / -windows.txt
```

---

## 3. Étapes d'implémentation (ordre proposé)

> Chaque étape décrit : **quoi faire**, **fichiers concernés**, **critère de validation**.
> L'ordre choisit d'abord les couches « sûres » (config, core, stockage) puis les plus risquées (web).

### ÉTAPE 0 — Préparation & filet de sécurité

**Actions**
1. Créer la structure de dossiers `app/`, `scripts/`, `tests/`, `docs/new-structure/` (déjà partiel).
2. Écrire des **tests de smoke** qui lancent l'app (imports OK, `create_app`, routes répondent) — sur SQLite.
3. Vérifier que l'app démarre avec `python main.py` avant tout changement (état de base sain).

**Fichiers créés**
- `tests/conftest.py`, `tests/test_smoke.py`, `tests/test_config.py`
- `pytest.ini` ou config dans `pyproject.toml` (si on en ajoute un)

**Critère de validation**
- `pytest tests/test_smoke.py` passe sur la version actuelle (avant refactoring).

---

### ÉTAPE 1 — `config` : extraire `config_manager.py` en package

**Actions**
1. Créer `app/__init__.py` et `app/config/__init__.py`.
2. Déplacer `config_manager.py` → `app/config/manager.py`.
3. Créer un **module passerelle** `config_manager.py` à la racine :
   ```python
   # config_manager.py (passerelle)
   from app.config.manager import *   # ré-exporte load_config, get_config, etc.
   ```
   Ainsi **aucun** module existant n'a besoin d'être modifié immédiatement.

**Fichiers**
- Créer : `app/__init__.py`, `app/config/__init__.py`, `app/config/manager.py`
- Modifier : `config_manager.py` → devient passerelle

**Critère**
- `python -c "import config_manager"` et l'app démarrent toujours.

---

### ÉTAPE 2 — `core` : paths + logging + utils

**Actions**
1. Extraire la détection `frozen`/`SCRIPT_DIR`/`CONFIG_DIR` dans `app/core/paths.py`.
2. Extraire `setup_logging()` de `main.py` dans `app/core/logging_setup.py`.
3. Regrouper les helpers partagés (`_safe_float`, `_safe_str`, `_normalize_date`, `_map_recipient_type`) dans `app/core/utils.py`.

**Fichiers**
- Créer : `app/core/{__init__,paths,logging_setup,utils}.py`
- Modifier : `main.py` (import `setup_logging` depuis `core`), `config_manager` (lire les paths depuis `core`).

**Critère**
- L'app démarre ; les logs sont écrits dans `data/`.

---

### ÉTAPE 3 — `storage` : découper `sqlite_db.py`

**Actions**
1. `app/storage/session.py` : connexion, WAL, `get_connection`, `get_cursor`, `close_connection`.
2. `app/storage/db.py` : schéma (SCHEMA), `init_database`, migrations, seeds.
3. `app/storage/repos/*.py` : requêtes par entité (invoices, products, contacts, vendeurs, pos).
   - Extraire de `invoice_engine.py`/`pos_engine.py` les accès SQLite vers les repos.
4. Mettre à jour `invoice_engine.py` et `pos_engine.py` pour utiliser les repos.
5. Passerelle : `sqlite_db.py` ré-exporte `get_cursor`, `get_connection`, `init_database`, etc.

**Fichiers**
- Créer : `app/storage/{__init__,session,db}.py`, `app/storage/repos/{__init__,invoices,products,contacts,vendeurs,pos}.py`
- Modifier : `invoice_engine.py`, `pos_engine.py`, passerelle `sqlite_db.py`.

**Critère**
- Le POS et la facturation fonctionnent (créer ticket, facture) ; données persistées dans `data/tconnector.db`.

---

### ÉTAPE 4 — `domain` : regrouper la logique métier

**Actions**
1. Déplacer `invoice_engine.py` → `app/domain/invoices.py` (passerelle `invoice_engine.py`).
2. Déplacer `pos_engine.py` → `app/domain/pos.py` + scinder `products.py`, `contacts.py`, `vendeurs.py` (passerelles).
3. Déplacer `seed_data.py` et `generate_articles.py` vers `app/domain/` ou `scripts/` (passerelles si importés).

**Fichiers**
- Créer : `app/domain/{__init__,invoices,pos,products,contacts,vendeurs,tax}.py`
- Modifier : passerelles racine (`invoice_engine.py`, `pos_engine.py`, etc.).

**Critère**
- Facturation, POS, produits, vendeurs, contacts répondent comme avant via le dashboard.

---

### ÉTAPE 5 — `integration/sage` : découper `database.py` (le plus gros de la couche externe)

**Actions**
1. `app/integration/sage/connection.py` : `get_pool`, `close_pool`, `_build_conn_string`, `get_cursor`, `ping_database` (seul import de `pyodbc`).
2. `app/integration/sage/schema.py` : `discover_tables`, `get_table_map`, `ensure_sfec_columns`, constantes tables.
3. `app/integration/sage/queries/*.py` :
   - `invoices.py` : `fetch_sales_invoices`, `fetch_purchase_invoices`, `fetch_doc_lines`, fallbacks.
   - `articles.py` : `fetch_articles`, `_merge_article_stock`.
   - `contacts.py` : `fetch_contacts`.
   - `references.py` : `fetch_tax_rates`, `fetch_ledger_accounts`.
4. Passerelle : `database.py` ré-exporte tous ces symboles (les autres modules ne changent pas).

**Fichiers**
- Créer : `app/integration/{__init__,}.py`, `app/integration/sage/{__init__,connection,schema}.py`, `app/integration/sage/queries/{__init__,invoices,articles,contacts,references}.py`
- Modifier : passerelle `database.py`.

**Critère**
- `from database import fetch_sales_invoices` fonctionne toujours ; `ping_database` se comporte pareil.

---

### ÉTAPE 6 — `integration/sfec` : découper `sfec_client.py` + `sfec_endpoints.py`

**Actions**
1. `app/integration/sfec/client.py` <- `sfec_client.py`.
2. `app/integration/sfec/payload.py` : `db_invoice_to_sfec`, `validate_sfec_payload`, `sqlite_invoice_to_sfec` (fusionner les duplications).
3. `app/integration/sfec/certification.py` <- `sfec_endpoints.py` (certify, check_health, fichiers certifiés).
4. Passerelles racine `sfec_client.py` / `sfec_endpoints.py`.

**Fichiers**
- Créer : `app/integration/sfec/{__init__,client,payload,certification}.py` ; passerelles.

**Critère**
- Certification SFEC d'une facture locale (sandbox) fonctionne.

---

### ÉTAPE 7 — `sync` : découper `sync_engine.py`, `sync_bidirectional.py`, `connectivity.py`

**Actions**
1. `app/sync/engine.py` <- `sync_engine.py` (polling, cache, metrics, queue).
2. `app/sync/bidirectional.py` <- `sync_bidirectional.py`.
3. `app/sync/connectivity.py` <- `connectivity.py`.
4. `app/sync/scheduler.py` : extraire de `main.py` le lancement des threads (`start_polling`, `start_bi_sync`, `start_background_check`, etc.).
5. Passerelles racine pour les trois modules.

**Fichiers**
- Créer : `app/sync/{__init__,engine,bidirectional,connectivity,scheduler}.py` ; passerelles.

**Critère**
- Polling et sync bidirectionnelle démarrent ; `main.py` peut appeler `scheduler`.

---

### ÉTAPE 8 — `web` : découper `dashboard.py` (étape la plus délicate)

**Actions (par sous-étapes)**
1. **Sortir CSS/JS/HTML de Python** :
   - Extraire `CSS` → `web/static/css/app.css`.
   - Extraire le JS (ALERT_ZONE, FOOTER, LOGIN_HTML) → `web/static/js/app.js` + `web/templates/*.html`.
   - Modifier `_page()`, `_sidebar()`, `LOGIN_HTML` pour utiliser les fichiers statiques (`url_for('static', ...)`).
2. **Séparer l'authentification** :
   - `web/auth/user_auth.py` <- `user_auth.py` (passerelle racine).
   - `web/auth/security.py` : `_auth_before_request`, CSRF, `_security_headers`, permissions.
   - `web/auth/routes.py` : login/logout.
3. **Découper en blueprints Flask** :
   - `web/routes/invoices.py`, `pos.py`, `clients.py`, `config.py`, `certified.py`, etc.
   - Remplacer les `@app.route(...)` par `@bp.route(...)` puis `app.register_blueprint(bp)`.
4. **Fabrique `create_app()`** :
   - `web/app.py` : `create_app(config)` qui monte l'app, les blueprints, les filtres sécurité.
   - `main.py` appelle `create_app()`.

**Fichiers**
- Créer : `web/{__init__,app}.py`, `web/auth/*`, `web/routes/*`, `web/templates/*`, `web/static/*`.
- Modifier : `dashboard.py` (passerelle conservée temporairement), `main.py`.

**Critère**
- **Toutes les pages et API renvoient exactement les mêmes réponses** ; `app` estimateur `factory` :
  ```python
  from app.web.app import create_app
  app = create_app()
  ```

---

### ÉTAPE 9 — `service` & entrées + scripts Windows

**Actions**
1. `app/service/cli.py` : regroupe `main()` et les commandes `--install-service`... en séparant la partie Windows par garde `sys.platform`.
2. `app/service/windows_service.py` <- `service.py` (imports `pywin32` **ici uniquement** dans un bloc `if sys.platform == "win32"`).
3. Déplacer tous les `.bat` + `installer.iss` dans `scripts/windows/`.
4. Déplacer `sql/add-sfec-columns.sql` → `scripts/sql/`.
5. Nettoyer à la racine : supprimer les passerelles **uniquement une fois** que plus aucun module ne les importe.

**Fichiers**
- Créer : `app/service/{__init__,cli,windows_service}.py`, `scripts/windows/`, `scripts/sql/`.
- Déplacer : `service.py`, `_install_svc.py`, `*.bat`, `installer.iss`, `sql/*.sql`.
- Modifier : `main.py` final.

**Critère**
- `python main.py` marche sur Linux ; le service Windows (`python service.py`) reste exploitable sur Windows.

---

### ÉTAPE 10 — `requirements` & packaging

**Actions**
1. `requirements.txt` : base multiplateforme `pyodbc, requests, flask, cryptography, reportlab, waitress`.
2. `requirements-windows.txt` : `-r requirements.txt` + `pywin32`.
3. `requirements-linux.txt` : `-r requirements.txt` (+ commentaire driver ODBC via apt).
4. Optionnel : ajouter `pyproject.toml` avec config pytest et métadonnées.

**Fichiers**
- Créer/modifier : `requirements*.txt`, évent. `pyproject.toml`.

**Critère**
- `pip install -r requirements-linux.txt` passe sans erreur sous Ubuntu ; `-windows.txt` idem sous Windows.

---

### ÉTAPE 11 — Tests & nettoyage final

**Actions**
1. Compléter les tests unitaires : domaine (totaux, numérotation), repos SQLite (en mémoire), payload SFEC.
2. Lancer l'ensemble : `pytest`, `python -c "import main"`, démarrage manuel du dashboard.
3. Supprimer les derniers fichiers orphelins / passerelles obsolètes.
4. Mettre à jour `README`/docs si nécessaire.

**Fichiers**
- `tests/*`, nettoyage racine.

**Critère**
- `pytest` vert ; app démarre sur Linux ; aucun import cassé.

---

## 4. Tableau synoptique : ancien → nouveau

| Ancien fichier | Nouvel emplacement | Type |
|---|---|---|
| `config_manager.py` | `app/config/manager.py` (+ passerelle) | déplacement |
| `main.py` (setup_logging, threads) | `app/core/logging_setup.py`, `app/sync/scheduler.py` | découpage |
| `sqlite_db.py` | `app/storage/{db,session}.py` + `repos/` | découpage |
| `invoice_engine.py` | `app/domain/invoices.py` | déplacement |
| `pos_engine.py` | `app/domain/pos.py` (+ products/contacts/vendeurs) | découpage |
| `database.py` | `app/integration/sage/{connection,schema,queries}` | découpage |
| `sage_writer.py` | `app/integration/sage/writer.py` | déplacement |
| `sfec_client.py` | `app/integration/sfec/client.py` | déplacement |
| `sfec_endpoints.py` | `app/integration/sfec/{payload,certification}.py` | découpage |
| `sync_engine.py` | `app/sync/engine.py` | déplacement |
| `sync_bidirectional.py` | `app/sync/bidirectional.py` | déplacement |
| `connectivity.py` | `app/sync/connectivity.py` | déplacement |
| `dashboard.py` | `app/web/{app,routes,auth,templates,static}` | découpage |
| `user_auth.py` | `app/web/auth/user_auth.py` | déplacement |
| `pdf_generator.py` | `app/domain/pdf.py` | déplacement |
| `article_sync.py` | `app/sync/article_sync.py` | déplacement |
| `seed_data.py` / `generate_articles.py` | `app/domain/seed/` | déplacement |
| `service.py` / `_install_svc.py` | `app/service/windows_service.py` | déplacement/isolé |
| `*.bat`, `installer.iss` | `scripts/windows/` | déplacement |
| `sql/*.sql` | `scripts/sql/` | déplacement |
| `requirements.txt` | scindé 3 fichiers | refonte |

---

## 5. Ordre d'exécution recommandé & gestion des risques

| Étape | Risque | Atténuation |
|---|---|---|
| 0 (prépa/tests) | — | filet de sécurité |
| 1 config | faible | passerelle |
| 2 core | faible | IDs déplacés identiques |
| 3 storage | moyen | passerelle + tests repos |
| 4 domain | moyen | passerelles + tests |
| 5 integration/sage | **élevé** (SQL Server) | tests `ping_database`, pas de changement requêtes |
| 6 integration/sfec | moyen | garder payloads identiques |
| 7 sync | moyen | garder signatures |
| 8 web | **très élevé** (4419 l.) | découpage par sous-étapes, vérif chaque page/routes |
| 9 service/scripts | moyen | garde `sys.platform` |
| 10 requirements | faible | — |
| 11 nettoyage/tests | faible | suppression passerelles |

**Recommandation** : exécuter dans cet ordre, en validant + commit après **chaque** étape.

---

## 6. Critères de « terminé »

- `main.py` démarre le dashboard sur `:3000` — identique avant/après.
- Toutes les routes/pages/API répondent de façon identique.
- Le POS, la facturation, la certification SFEC et la sync Sage fonctionnent.
- `pip install -r requirements-linux.txt` passe proprement ; `pywin32` uniquement dans `app/service/windows_service.py`.
- `pytest` passe (tests de smoke + unitaires).
- Plus aucun import cassé ; les passerelles obsolètes supprimées.

---

## 7. Points ouverts à confirmer avant de commencer

1. **Découpage de `dashboard.py` en blueprints** : on garde le rendu exact (CSS/JS extraits mais inchangés) — OK ?
2. **Répertoire** : on pose tout sous `app/` comme ci-dessus — OK ?
3. **Suppression des passerelles racine** : on les garde jusqu'à la fin puis on les supprime — OK ?
4. **Tests** : on ajoute `pytest` + quelques tests de smoke avant de toucher au code — OK ?
5. **Commits** : on commit à chaque étape validée — OK ?
6. **Plan comptable `database`** : on ne touche **pas** aux requêtes SQL Sage (juste déplacement) — OK ?

> La validation de ce plan déclenche le démarrage de la restructuration, **étape par étape**.

---

## 8. Avancement réel (session de travail / restructuration effectuée)

> Cette section consigne ce qui a été réellement fait et les écarts par rapport au plan,
> pour servir de base au commit et aux chantiers restants.

### 8.1 État des étapes

| Étape | Statut |
|---|---|
| 0 — Préparation & filet de sécurité | ✅ structure créée ; **tests pytest non écrits** (environnement sans pip/venv) |
| 1 — config | ✅ `config_manager.py` → `app/config/manager.py` (passerelle + **deadlock corrigé : `threading.Lock` → `RLock`**) |
| 2 — core | ✅ `app/core/{paths,logging_setup,utils}.py` |
| 3 — storage | ✅ `sqlite_db.py` → `app/storage/db.py` (passerelle) ; `repos/` laissé vide (découpage différé) |
| 4 — domain | ✅ `invoice_engine/pos_engine/pdf_generator` → `app/domain/{invoices,pos,pdf}.py` ; `seed_data.py` → `app/domain/seed.py` ; `generate_articles.py` → `scripts/` |
| 5 — integration/sage | ✅ `database.py` → `app/integration/sage/database.py` (déplacement intact) + ré-exporteurs `connection.py`, `schema.py`, `queries/*` ; `sage_writer.py` → `writer.py` |
| 6 — integration/sfec | ✅ `client.py`, `endpoints.py` (chemin `DATA_DIR` → `app.core.paths`), `payload.py`, `certification.py` + passerelle |
| 7 — sync | ✅ `engine.py`, `bidirectional.py`, `connectivity.py`, `article_sync.py` + `scheduler.py` (extrait de `main.py`) |
| 8 — web | ✅ (partiel) `dashboard.py` → `app/web/dashboard.py` (CSS extrait vers `static/css/app.css`) ; `user_auth.py` → `app/web/auth/user_auth.py` ; `app.py` avec `create_app()` |
| 9 — service & scripts | ✅ `service.py` → `app/service/windows_service.py` ; `_install_svc.py` → `scripts/windows/` ; `.bat` + `installer.iss` → `scripts/windows/` ; `sql/*.sql` → `scripts/sql/` |
| 10 — requirements | ✅ scindés : `requirements.txt` (commun), `-windows.txt`, `-linux.txt` |
| 11 — tests & nettoyage | ✅ (partiel) `compileall` vert sur tout ; imports stdlib testés ; découplage des imports internes `app/` vers chemins package ; validation d'exécution différée (pas de deps) |
| 12 — docs & commit | 🔄 docs mises à jour ; commit en cours |

### 8.2 Écarts documentés (délibérés)

1. **Blueprints Flask non créés** : `create_app()` retourne l'instance `app` existante construite par `app/web/dashboard.py` (routes inchangées). Séparation en blueprints = chantier ultérieur (nécessite Flask installé pour tester).
2. **JS/HTML non extraits** : seuls le CSS et `setup_logging` ont été extraits. `ALERT_ZONE`, `FOOTER`, `LOGIN_HTML`, `SIDEBAR_*` restent inline dans `dashboard.py` (déplacements différés, cohérents avec le choix « ne pas réécrire »).
3. **`app/storage/session.py` et `repos/*` non implémentés** : le stockage reste dans `app/storage/db.py` (monolithe déplacé). Découpage fin différé.
4. **`app/service/cli.py` non créé** : les commandes `--install-service` etc. restent dans `main.py`. 
5. **`cryptography` retiré des requirements** (grep : aucun import dans le code). `waitress` ajouté à la base.
6. **Vérification d'exécution impossible sur l'environnement actuel** : pas de `pip`/`python3-venv` installables (sudo nécessite un mot de passe). Validation faite par `py_compile` + imports de modules stdlib-only. Il faut installer `python3.14-venv` (ex. `sudo apt install python3.14-venv`) pour lancer `pip install -r requirements-linux.txt` + `python main.py`.
7. **Passerelles racine conservées** : `config_manager.py`, `sqlite_db.py`, `database.py`, `sfec_client.py`, `sfec_endpoints.py`, `connectivity.py`, `sync_engine.py`, `sync_bidirectional.py`, `article_sync.py`, `invoice_engine.py`, `pos_engine.py`, `pdf_generator.py`, `seed_data.py`, `sage_writer.py`, `user_auth.py`, `service.py`, `dashboard.py`. Suppression prévue seulement à l'étape 11 finale, une fois plus aucun module ne les importe.

### 8.3 Prochains chantiers (une fois l'environnement installable)

- Installer les dépendances (`python3.14-venv`, `pip install -r requirements-linux.txt`).
- Écrire les tests pytest (smoke : `create_app()`, routes ; unitaires : totaux/numérotation, repos SQLite, payload SFEC).
- Découper `dashboard.py` en blueprints (routes, templates, JS) et supprimer les passerelles racine.
- Vérifier `python main.py` sur Linux (waitress) et `python service.py` sur Windows.
