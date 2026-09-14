# PLAN — Refactorisation Flask selon le standard Real Python

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/new-structure/`
> **Référence** : https://realpython.com/flask-project/ (application factory, blueprints, templates Jinja, static files)
> **Statut** : PLAN PROPOSÉ — en attente de validation
> **Pré-requis** : phase 1 de restructuration déjà exécutée (voir `plan-restructuration.md` §8)

---

## 1. Constat de départ (mesures réelles, 12/09/2026)

La phase 1 a découpé le projet en packages `app/` (`core`, `config`, `storage`, `domain`, `sync`, `integration`, `web`, `service`). Mais la couche web est restée monolithique :

| Constats mesurés | Valeur |
|---|---|
| `app/web/dashboard.py` | **4 217 lignes** — un seul fichier |
| Routes Flask définies | **94** (dont ~50 API JSON) |
| Lignes de HTML inline dans le Python | **~442** (chaînes `<!DOCTYPE`, `<div`, `<table`, `<form>…`) |
| Blueprints Flask | **0** — tout en `@app.route` sur une instance globale |
| Templates Jinja (`templates/`) | **0** — HTML généré par concaténation de chaînes + `render_template_string` |
| `create_app()` | **factice** — retourne l'instance globale construite à l'import (`app/web/app.py` = 8 lignes) |
| Fichiers statiques | CSS extrait dans `static/css/app.css` mais **réinjecté inline** via le hack `static_content.py` (cache + lecture fichier) au lieu d'être servi par Flask |
| Auth/sécurité | mélangée dans `dashboard.py` (`before_request`, CSRF, headers, permissions, login) |
| Tests | **aucun** (`tests/` n'existe pas) |
| Passerelles racine | 17 modules ponts (`sync_engine.py`, `database.py`, `dashboard.py`…) encore présents |

**Vérifié** : `from app.web.dashboard import app` fonctionne dans le venv (`.venv`, Python 3.11, deps installées) → l'app importe et expose ses 94 routes. On part d'un état sain.

## 2. Objectif : la structure Real Python adaptée à T-CONNECTOR

Le tutoriel Real Python pose 4 piliers qu'on applique ici :

1. **Application factory réelle** — `create_app()` construit et configure l'app (config, sécurité, blueprints) au lieu de retourner une instance globale.
2. **Blueprints** — un blueprint par domaine fonctionnel, routes déplacées de `@app.route` vers `@bp.route`.
3. **Templates Jinja** — extraction du HTML inline vers `templates/` avec `base.html` + héritage (`{% extends %}`, `{% block %}`), includes `_navigation.html` / `_sidebar.html`.
4. **Static files** — CSS/JS servis par Flask via `url_for('static', …)`, suppression de `static_content.py`.

### Arborescence cible (couche web uniquement)

```
app/web/
├── __init__.py
├── app.py                     # create_app() RÉELLE : config, secret, sécurité, blueprints
├── auth/
│   ├── __init__.py
│   ├── user_auth.py           # (existant, inchangé) gestion utilisateurs SQLite
│   ├── security.py            # before_request, CSRF, headers, permissions, login_required
│   └── routes.py              # blueprint auth : /login /logout /compte/mot-de-passe
├── routes/
│   ├── __init__.py
│   ├── dashboard.py           # bp : / /invoices /pending /certified /certified/<id>/print /sales /ready /health
│   ├── config.py              # bp : /config + /api/config* /api/db/test /api/tables /api/sfec/*
│   ├── billing.py             # bp : /billing* + /api/invoices*
│   ├── pos.py                 # bp : /pos* + /api/products /api/tax-rates*
│   ├── directory.py           # bp : /clients /vendeurs /utilisateurs + /api/contacts /api/vendeurs /api/utilisateurs
│   └── sync_api.py            # bp : /api/sync* /api/articles/sync /api/connectivity /api/metrics
├── templates/
│   ├── base.html              # <!DOCTYPE>, <head>, blocks header/content, include _sidebar
│   ├── _sidebar.html          # <- _sidebar() actuel
│   ├── auth/login.html
│   ├── dashboard/{index,invoices,pending,certified,print}.html
│   ├── config/index.html      # 7 onglets (le plus gros morceau)
│   ├── billing/{list,form,detail}.html
│   ├── pos/{index,ticket_print}.html
│   └── directory/{clients,vendeurs,utilisateurs}.html
└── static/
    ├── css/app.css            # (existant) servi via url_for, plus d'injection inline
    └── js/app.js              # <- JS inline actuel (ALERT_ZONE, FOOTER, handlers)
```

**Ce qui ne change PAS** : `main.py`, la logique métier (`domain/`, `sync/`, `integration/`, `storage/`), les URL, les réponses HTML/JSON (rendu visuel identique), le comportement Windows/Linux.

## 3. Règles de conduite (héritées de la phase 1)

1. **Chaque étape laisse l'app fonctionnelle** → commit après validation.
2. **Aucune réécriture de logique** : on déplace le HTML tel quel dans des templates, on remplace `render_template_string(x)` par `render_template("…")`, on ne redesigne rien.
3. **Parité des réponses** : le filet de sécurité (étape 0) capture les réponses actuelles ; après chaque étape, on compare.
4. **`url_for()` partout** dans les templates (y compris `url_for('static', …)`) — fini les chemins en dur.
5. Les passerelles racine ne sont supprimées qu'à la toute fin.

## 4. Étapes d'implémentation

### ÉTAPE 0 — Filet de sécurité (bloquant, avant tout découpage)

**Actions**
1. Créer `tests/` + `pytest.ini`.
2. Script de **snapshot HTTP** : avec `app.test_client()`, appeler les ~30 routes GET publiques + routes API, sauvegarder statut + corps dans `tests/snapshots/*.json`.
3. Tests smoke : import de tous les modules `app/*`, `create_app()` retourne une app, 94 routes enregistrées.

**Critère** : snapshots capturés sur le code actuel ; `pytest` vert.

### ÉTAPE 1 — Templates & statiques (extraction mécanique)

**Actions**
1. Créer `templates/base.html` à partir de `_page()` ; `_sidebar.html` à partir de `_sidebar()` ; `static/js/app.js` à partir du JS inline.
2. Pour chaque page : copier le HTML inline tel quel dans `templates/<domaine>/<page>.html`, remplacer les `f-string`/`format` par des variables Jinja `{{ }}` (attention à l'échappement — le code actuel échappe via `_esc()`, Jinja auto-échappe : ne **pas** double-échapper).
3. Remplacer les `url` en dur par `url_for('pages.home')` après création des blueprints (ou temporairement chemins relatifs identiques).
4. Brancher `url_for('static', filename='css/app.css')` et supprimer l'injection inline.

**Critère** : comparaison snapshot par snapshot — HTML identique (modulo espaces).

### ÉTAPE 2 — Sécurité & auth extraites

**Actions**
1. `auth/security.py` : déplacer `_auth_before_request`, `_apply_session_security`, CSRF, `_security_headers`, `_login_required`, `_required_permission`.
2. `auth/routes.py` : blueprint `auth` (`/login`, `/logout`, `/compte/mot-de-passe`).

**Critère** : login/logout/403/CSRF fonctionnent ; snapshots auth identiques.

### ÉTAPE 3 — Découpage en blueprints (le cœur du chantier)

**Préalable (mission déléguée, agent dédié)** : découpage mécanique de `dashboard.py` en fichiers « parking » par domaine (copie de fonctions, aucune modification de logique), afin que les agents parallèles travaillent sur des fichiers disjoints sans conflit. Mission purement mécanique = idéale pour un sous-agent k2.7 code.

**Actions** (par blueprint : `dashboard`, `config`, `billing`, `pos`, `directory`, `sync_api`)
1. Créer `routes/<domaine>.py` avec `bp = Blueprint("<domaine>", __name__)`.
2. Convertir `@app.route` → `@bp.route`, déplacer les handlers + helpers privés associés.
3. Enregistrer dans `create_app()`.

**Critère** : 94 routes toujours enregistrées (`len(app.url_map._rules)`), snapshots identiques.

### ÉTAPE 4 — Application factory réelle

**Actions**
1. `app/web/app.py` : `create_app()` construit `Flask(__name__)`, charge config/secret, enregistre sécurité + blueprints, configure `session`/timedelta.
2. `main.py` inchangé (appelle déjà `create_app()`).
3. Supprimer l'instance globale de `app/web/dashboard.py` → le module devient un simple agrégat ou est vidé.

**Critère** : `from app.web.app import create_app; app = create_app()` autonome ; `python main.py` démarre sur `:3000`.

### ÉTAPE 5 — Nettoyage final

**Actions**
1. Supprimer `static_content.py` et le cache CSS.
2. Supprimer les 17 passerelles racine (vérifier d'abord qu'aucun import ne les référence : `grep -r "from dashboard import\|import sync_engine$" …`).
3. Compléter `requirements*.txt` (pytest en dev), README, fiche technique.

**Critère** : `pytest` vert, app démarre, `git grep` ne trouve plus de passerelle importée.

## 5. Orchestration multi-agents — combien de sous-agents ?

> **Principe directeur (règle n°2 du boss)** : l'orchestrateur k3 **n'intervient que quand c'est vraiment nécessaire** — arbitrage de conflits, validation croisée finale, décision de go/no-go entre vagues. **Tout le travail d'exécution est délégué** aux sous-agents **kimi k2.7 code** (contexte isolé, une mission bornée chacun), y compris les tâches mécaniques.
>
> **Rôle résiduel de l'orchestrateur k3** : rédiger les briefs de mission, lancer les vagues, vérifier les critères de validation (il *vérifie*, il n'*exécute* pas), committer les étapes validées, trancher en cas de conflit.

### Réponse : **7 sous-agents**, répartis en **5 vagues**

| Vague | Agent | Mission | Dépend de |
|---|---|---|---|
| 1 | **A1 — Sentinel** | Étape 0 : `tests/`, snapshot HTTP des 94 routes, smoke tests | — |
| 2 | **A2 — Templater** | Étape 1 : extraction Jinja complète (base, sidebar, login, ~20 templates, JS→static) | A1 |
| 3 | **A3 — Splitter** | Étape 3 préalable : découpage mécanique de `dashboard.py` en parkings par domaine (copie stricte, zéro logique) | A2 |
| 4 | **A4 — Core BP** | Blueprints `auth` + `dashboard` + `sync_api` (~25 routes) | A3 |
| 4 | **A5 — Config BP** | Blueprint `config` (7 onglets + API SFEC/db, ~1 300 lignes à lui seul) | A3 |
| 4 | **A6 — Métier BP** | Blueprints `billing` + `pos` + `directory` (~35 routes) | A3 |
| 5 | **A7 — Clôture** | Étape 4 (factory réelle) + étape 5 (nettoyage, passerelles, tests finaux) | A4–A6 |

Entre chaque vague, l'orchestrateur ne fait que : lire le rapport du sous-agent, lancer la vérification (snapshots / pytest / comptage des 94 routes), décider go/no-go. Il ne touche pas au code.

### Pourquoi 7 et pas moins

- **Tout est délégable** : même le découpage « parking », purement mécanique, part chez un sous-agent (A3). L'orchestrateur garde uniquement la coordination et la validation — les seules choses qu'un sous-agent à contexte isolé ne peut pas faire.
- **Contexte limité des sous-agents** : `dashboard.py` fait 4 217 lignes (~150 Ko). Un k2.7 code ne tient pas fiable ce fichier en entier + les templates extraits + les instructions. D'où le parking préalable (A3) et des missions bornées à un domaine (A4–A6).
- **Conflits d'édition** : la vague 4 est parallélisable **uniquement** parce que chaque agent touche des fichiers disjoints (son parking + son blueprint). Sans ça, 3 agents éditant `dashboard.py` en parallèle = conflits garantis.
- **A2 est forcément seul et séquentiel** : l'extraction des templates traverse tout le fichier ; la paralléliser casserait la cohérence de `base.html`.
- **A1 en premier, non négociable** : sans snapshots, aucune étape n'a de critère de non-régression objectif.

### Charge estimée par agent

| Agent | Volume manipulé | Difficulté |
|---|---|---|
| A1 | faible (nouveau code) | faible |
| A2 | **très élevé** (~2 500 lignes de HTML/JS à extraire) | élevée — prévoir 2 passes (pages simples puis `/config`) |
| A3 | élevé mais **mécanique** (copie de blocs, aucune décision) | faible |
| A4 | moyen | moyenne |
| A5 | **élevé** (config = plus gros formulaire) | élevée |
| A6 | moyen | moyenne |
| A7 | faible-moyen | moyenne (suppressions = vérifications) |

Si le débit est un problème, A2 peut être scindé en A2a (pages simples) / A2b (`/config`), portant le total à **8**.

## 6. Suivi de progression — fichier unique

Tous les sous-agents consignent leur progression dans **un seul fichier** :

```
docs/new-structure/suivi-refactor-flask.md
```

**Règles d'écriture (incluses dans chaque brief de mission)** :

1. **Append-only** : chaque agent écrit **uniquement dans sa propre section réservée** (`## A1 — Sentinel`, `## A2 — Templater`, etc.), pré-créée par l'orchestrateur avec le statut `⏳ EN ATTENTE`. Jamais de modification des sections des autres agents.
2. **Format imposé** par section : statut (`⏳ / 🔄 / ✅ / ❌`), date-heure de fin, fichiers créés/modifiés, résultats des vérifications (snapshots, pytest, comptage routes), problèmes rencontrés, décisions prises.
3. **Vague 4 (parallèle)** : A4/A5/A6 écrivent dans des sections disjointes → pas de conflit de fichier. Si un conflit survient malgré tout, l'agent ré-écrit sa section après relecture du fichier.
4. L'orchestrateur met à jour uniquement le **tableau de bord global** en tête de fichier (statut de chaque vague) après validation de chaque mission.

**Gabarit du fichier de suivi** :

```markdown
# SUIVI — Refactorisation Flask (Real Python)

| Agent | Mission | Statut | Fin |
|---|---|---|---|
| A1 | Filet de sécurité | ⏳ | — |
| A2 | Extraction Jinja | ⏳ | — |
| A3 | Découpage parking | ⏳ | — |
| A4 | Blueprints core | ⏳ | — |
| A5 | Blueprint config | ⏳ | — |
| A6 | Blueprints métier | ⏳ | — |
| A7 | Factory + clôture | ⏳ | — |

---

## A1 — Sentinel
**Statut** : ⏳ EN ATTENTE
(à remplir par l'agent)

## A2 — Templater
**Statut** : ⏳ EN ATTENTE
(à remplir par l'agent)

…
```

---

## 7. Risques & atténuations

| Risque | Atténuation |
|---|---|
| Échappement HTML double (Jinja auto-escape vs `_esc()`) | Règle explicite dans chaque mission d'agent : supprimer `_esc()` dans les templates, le garder côté Python |
| f-strings imbriquées dans le HTML inline | Extraction en copier-coller strict, conversion `f"…{x}…"` → `{{ x }}` mécanique, vérifiée par snapshots |
| `url_for` après split blueprints (noms d'endpoints changent) | Conserver les noms de fonctions → endpoints `<bp>.<fonction>` ; passe de remplacement global dans les templates à l'étape 4 |
| Ordre des `before_request`/middlewares | Un seul endroit : `security.py`, enregistré dans `create_app()` avant les blueprints |
| Régression silencieuse sur les 50 API JSON | Snapshots JSON + test de présence des 94 routes |

## 8. Critères de « terminé »

- [ ] `create_app()` réelle, autonome, testable (`app.test_client()` sans effet de bord global)
- [ ] 0 `@app.route` hors blueprints ; 6 blueprints enregistrés ; 94 routes présentes
- [ ] 0 HTML inline dans le Python (hors petits fragments d'erreur) ; héritage `base.html` partout
- [ ] CSS/JS servis via `url_for('static', …)` ; `static_content.py` supprimé
- [ ] Snapshots étape 0 == réponses finales (statut + corps)
- [ ] `pytest` vert ; `python main.py` démarre sur `:3000` (Linux) ; passerelles racine supprimées
- [ ] Rendu visuel du dashboard **strictement identique** (vérification manuelle navigateur)

## 9. Points à valider avant lancement

1. Découpage en 6 blueprints (`auth / dashboard / config / billing / pos / directory / sync_api`) — OK ?
2. On garde le rendu HTML **à l'identique** (extraction, pas de refonte visuelle) — OK ?
3. 7 sous-agents k2.7 code en 5 vagues, orchestrateur k3 limité à la coordination/validation (exécution 100 % déléguée) — OK ? (ou 8 si A2 est scindé)
4. Je committe après chaque étape validée — OK ?
