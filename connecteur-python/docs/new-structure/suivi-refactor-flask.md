# SUIVI — Refactorisation Flask (Real Python)

> Fichier de progression unique. Règles : chaque sous-agent écrit **uniquement dans sa section** (append-only), l'orchestrateur k3 met à jour le tableau de bord ci-dessous après validation de chaque mission.
> Référence : `plan-refactor-flask-realpython.md` (même dossier).

| Agent | Mission | Statut | Fin |
|---|---|---|---|
| A1 | Filet de sécurité (tests + snapshots) | ✅ | 2026-09-12 20:16 |
| A2 | Extraction Jinja (templates + static) | ⏳ | — |
| A3 | Découpage parking de dashboard.py | ⏳ | — |
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
**Statut** : ⏳ EN ATTENTE

(à remplir par l'agent)

---

## A3 — Splitter
**Statut** : ⏳ EN ATTENTE

(à remplir par l'agent)

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
