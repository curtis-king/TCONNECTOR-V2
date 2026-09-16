# AUDIT — Conformité au plan `docs/corrections/impl-etapes-6-a-fin.md`

> **Date de l'audit** : 2026-09-13 (soir) — orchestrateur k3
> **Méthode** : vérification item par item sur le code réel (greps + exécution pytest/comparateur), aucune modification du code métier.
> **Référence auditée** : `docs/corrections/impl-etapes-6-a-fin.md` (étapes 6 → fin)

---

## 1. Verdict global

> **MAJ 2026-09-16 : chantier soldé.** B1 ✅ + B2 ✅, commités. pytest 12/12, comparateur 51/51. Restent connus et documentés : bug pré-existant `api_create_contact` (400 « code unbound » sur succès, non corrigé — hors périmètre) et D1 (helpers JS globaux, optionnel).

Le gros du chantier (étapes 6, 7, 8) est **fait et bien fait**. Il reste : **2 bugs réels**, **3 manques fonctionnels**, **1 dette de sécurité**, et toute l'**étape 9** (tests + snapshots) à finir.

**Filet de sécurité actuel** : après installation de `qrcode` dans le venv (était dans `requirements.txt` mais pas installé) → `pytest` 5/5 vert. Comparateur : **39/51 OK, 12 DIFF** (évolution attendue liée aux avoirs — à inspecter puis régénérer, étape 9.1).

## 2. État par item du plan

### ✅ Conformes (vérifiés)

| Item | Preuve |
|---|---|
| 6.1 `print_certified` NameError | `dashboard.py` l.170-171 : `inv_type` + `doc_label` définis |
| 6.2 Filtre `type` sur `/api/invoices/list` | `billing.py` l.86 : `type_doc=request.args.get("type")` |
| 6.3 `meta_rows` au détail | passé l.143 + rendu dans `billing/detail.html` l.17 |
| 7.1 Formulaire (type_doc, paiement, devise, réf. avoir, RCCM, JS) | `billing/form.html` : 19 occurrences des clés attendues |
| 7.2 Totaux détaillés détail | `detail.html` : TVA 18/5, exonéré, centime, timbre présents |
| 7.3 Liste : filtre + badge type | `list.html` l.23 (select vente/avoir) + badge AVOIR `billing.py` l.122-128 |
| 7.5 print.html : doc_label + totaux + mentions | `{{ doc_label }}` l.28 ; 5 clés de totaux détaillés |
| 7.7 clients.html RCCM + is_taxable | 5 occurrences |
| 8.1 Mapping `type_doc` Sage | `database.py` l.632 : `CASE WHEN d.DO_Type = {at} THEN 'avoir' ELSE 'vente' END` |
| 8.2 Règles d'intégrité en auto-certif | `engine.py` l.548-584 : référence requise + anti double-avoir |
| 8.3 Avoirs négatifs dans Sage | `writer.py` l.36-38 : `avoir_type` + `sign = -1` |
| 7.6 (partiel) badge certifiés | `certified.html` l.82 — **mais cassé, voir ❌ 1.6** |

### ❌ Manquants / cassés

| # | Item | Détail vérifié |
|---|---|---|
| B1 | **BUG `app/domain/pdf.py` l.10** | `logger.warning()` dans `except ImportError` **avant** la définition de `logger` (l.12) → `NameError` qui masque l'absence de `qrcode` et fait planter tout import de `app.web.app` (cassait pytest + comparateur avant install de qrcode) |
| B2 | **BUG `generate_avoir_number()`** (`storage/db.py` l.495-505) | lit `doc_format_avoir` / `doc_next_avoir` alors que les seeds (l.423-424) écrivent `avoir_format` / `avoir_next_number` → **la numérotation des avoirs ne fonctionnera jamais** (dette signalée dans le plan §5.1, toujours présente) |
| M1 | **1.6 + 7.6 certifiés** : `api_certified_list` (`sync_api.py` l.73-86) n'expose **pas** `invoice_type` → le badge avoir de `certified.html` (`v.invoice_type==="creditNote"`) reçoit `undefined` et affiche toujours « Vente » |
| M2 | **1.5 + 7.6 pending** : aucun badge avoir sur `/pending` (0 occurrence dans `pending.html` et dans la route `pending_page`) — le plan le marquait « non bloquant » mais il reste à faire |
| M3 | **5.1b** : `count_invoices()` (`invoices.py` l.276-284) sans compteur d'avoirs (pas de CA net ventes − avoirs) |
| S1 | **5.2 Sécurité** : clé API SFEC **réelle** présente dans `config.json` (`def1e8…`). Le fichier est bien gitignoré (pas de fuite git ✅) mais le plan demande le passage par variable d'environnement + placeholder |
| T1 | **9.1 Snapshots** : 12 DIFF à inspecter (dont `/api/sfec/certified-from-api` passé de 500 → 200 — vérifier si c'est un vrai fix ou un effet de bord) puis régénérer |
| T2 | **9.2 Tests avoirs** : aucun test des 5 cas (avoir sans référence → 422, avoir sur non-certifiée → 422, double avoir → 422, doublon NIU → 409, tolérance totaux) |
| D1 | **7.9 Helpers JS globaux** : rien dans `app.js` (0 occurrence) — tout est inline dans `form.html`. Fonctionnel, non conforme au plan. **Cosmétique, optionnel** |

## 3. Plan de clôture — délégation

> Principe inchangé : orchestrateur k3 = briefs + vérification + commits ; exécution déléguée aux sous-agents **kimi k2.7 code** (`delegation.provider/model` déjà épinglés dans `~/.hermes/config.yaml`).
> Suivi : les agents écrivent dans **ce fichier**, sections réservées ci-dessous (append-only, même règle que pour le refactoring).

### Vague 1 — **B1 « Correctifs »** (1 agent, séquentiel)

Périmètre : `app/domain/pdf.py`, `app/storage/db.py`, `app/domain/invoices.py`, `app/web/routes/dashboard.py`, `app/web/routes/sync_api.py`, `app/web/templates/dashboard/pending.html`.

1. **B1** : déplacer `logger = logging.getLogger(...)` avant le `try: import qrcode` dans `pdf.py`.
2. **B2** : aligner `generate_avoir_number()` sur les clés seedées (`avoir_format` / `avoir_next_number`) — ou migrer les seeds vers les clés lues ; choisir l'option la moins risquée et tester la génération d'un numéro d'avoir.
3. **M1** : ajouter `invoice_type` (ou `type`) dans l'output d'`api_certified_list` ; vérifier que le badge de `certified.html` affiche « Avoir » sur un avoir réel/mock.
4. **M2** : badge avoir dans `pending_page` + `pending.html` (miroir du badge certifiés).
5. **M3** : compteur `avoirs` dans `count_invoices()` (+ le cas échéant `ca_net = ventes − avoirs` si déjà exposé ailleurs).

Critères : `pytest` 5/5 vert, comparateur sans nouveau DIFF non expliqué (les DIFF avoirs des templates pending sont attendus → documentés pour la vague 2).

### Vague 2 — **B2 « Sécurité + Tests + Snapshots »** (1 agent, après B1)

Périmètre : `app/config/manager.py`, `config.example.json`, `config.json`, `tests/`, `docs/`.

1. **S1** : clé SFEC lue depuis `SFEC_API_KEY` (env) avec fallback `config.json` ; placeholder dans `config.example.json` ; documenter dans le README/fiche technique. **Ne pas supprimer la clé réelle sans backup** — la déplacer vers `.env`/env et laisser `config.json` fonctionnel.
2. **T2** : ajouter `tests/test_avoirs.py` — les 5 cas du plan §4.2 (422 ×3, 409 NIU, tolérance totaux via `validate_sfec_payload`).
3. **T1** : inspecter les 12 DIFF un par un (s'assurer que ce sont bien les évolutions avoirs/UI et le fix `certified-from-api`), régénérer `tests/snapshots/`, vérifier 51/51 (ou plus) OK.
4. Vérification finale : `pytest` vert, `compileall` propre, `python main.py` + curl `/login` → 200.

Critères : pytest vert (anciens + nouveaux tests), comparateur 100 % OK sur baseline régénérée, clé API hors du fichier commité-able.

### Hors délégation (orchestrateur)

- Vérification croisée à la fin de chaque vague (pytest + comparateur relus par moi-même).
- Commits après validation de chaque vague.
- **D1** (helpers JS globaux) : laissé de côté sauf demande — cosmétique, aucun impact fonctionnel.

### Charge estimée

| Agent | Volume | Difficulté | Durée estimée |
|---|---|---|---|
| B1 Correctifs | 5 fixes ciblés, ~6 fichiers | faible-moyenne (B2 = le seul délicat) | ~20-30 min |
| B2 Sécu+Tests+Snapshots | 1 refacto config + 1 fichier de tests + inspection 12 diffs | moyenne | ~30-45 min |

**Total : 2 sous-agents k2.7 code, 2 vagues séquentielles.**

---

## 4. Suivi d'exécution (rempli par les agents)

| Agent | Mission | Statut | Fin |
|---|---|---|---|
| B1 | Correctifs (pdf, avoir_number, badges, count) | ✅ | 2026-09-16 18:35 |
| B2 | Sécurité clé SFEC + tests avoirs + snapshots | ✅ | 2026-09-16 19:04 |

---

## B1 — Correctifs
**Statut** : ✅ TERMINÉ — mer. 16 sept. 2026 18:35 WAT

**Fichiers modifiés** (aucun commit) :
- `app/domain/pdf.py` — B1 : `logger = logging.getLogger("t-connector.pdf")` déplacé avant le `try: import qrcode` ; le second bloc `try/except` (reportlab) était déjà après la définition → aucun autre cas.
- `app/storage/db.py` — B2 : `generate_avoir_number()` alignée sur les clés seedées `avoir_format` / `avoir_next_number` (miroir de `generate_invoice_number`) ; seeds `_seed_default_settings()` passées de `""` à `AV` / `AV{:06d}` / `1` (conforme au plan §gap-invoice) ; **décision** : option « fonction lit les clés seedées » (cohérente avec `generate_invoice_number`) + **migration `_migrate()`** : copie `doc_format_avoir`→`avoir_format` et `doc_next_avoir`→`avoir_next_number` si valeur existante non vide, sinon défaut, puis suppression des anciennes clés (gère les bases existantes dont les seeds étaient des chaînes vides — UPDATE explicite, pas INSERT OR IGNORE).
- `app/web/routes/sync_api.py` — M1 : `"invoice_type": inv.get("invoice_type", "")` ajouté dans `api_certified_list` (champ présent dans les réponses API SFEC, déjà utilisé dans `print_certified`) → le badge de `certified.html` (`v.invoice_type==="creditNote"`) peut afficher « Avoir ».
- `app/web/routes/dashboard.py` — M2 : badge `<span class="badge badge-warn">Avoir</span>` dans `pending_page`, rendu côté serveur après le numéro quand `inv.get("type_doc") == "avoir"` (champ exposé par `fetch_sales_invoices`, `CASE WHEN DO_Type=avoir_type THEN 'avoir'`).
- `app/domain/invoices.py` — M3 : `count_invoices()` gagne `stats["avoirs"]` (COUNT type_doc='avoir') et `stats["ventes"]` (type_doc='vente') ; aucune clé existante modifiée.

**Vérifications** :
1. `.venv/bin/python -m pytest` → **5/5 VERT** (4,28 s).
2. `tests/compare_snapshots.py` → **12 DIFF d'origine inchangés** (aucun disparu) + **1 nouveau DIFF : `/api/invoices/stats`** (attendu : M3 ajoute `avoirs`/`ventes` au JSON). `/pending` et `/certified` étaient déjà en DIFF avant mes changements et le restent (le badge M2 n'apparaît que si un avoir est en attente — les données de test n'en ont pas). Snapshots NON régénérés (mission B2).
3. Test réel `generate_avoir_number()` sur la base dev : `AV000001` puis `AV000002`, compteur `avoir_next_number` → `'3'`, format `AV{:06d}` vérifié par regex.
4. `python -m compileall app main.py` → propre.
5. Bonus : `count_invoices()` exécuté — clés `avoirs`/`ventes` présentes, clés existantes intactes.

**Décisions** :
- B2 : option choisie = « la fonction lit les clés seedées » (`avoir_format`/`avoir_next_number`), avec valeurs de seed réelles (`AV`, `AV{:06d}`, `1`) au lieu de `""`, et migration des anciennes clés dans `_migrate()` (les seeds `INSERT OR IGNORE` ne mettent pas à jour les bases existantes dont les clés existent vides).
- M2 : badge rendu côté serveur dans la route (le template `pending.html` reçoit `rows` en HTML sûr) plutôt que JS, faute de colonne Type dans le tableau pending ; style strictement identique au badge certifiés (`badge badge-warn`).

---

## B2 — Sécurité + Tests + Snapshots
**Statut** : ✅ TERMINÉ — mer. 16 sept. 2026 19:02 WAT — **aucun commit** (remise à l'orchestrateur)

**Fichiers modifiés / créés** :
- `app/config/manager.py` — **S1** : overlay de la variable d'environnement `SFEC_API_KEY` appliqué dans `load_config()` (PRIORITÉ sur `config.json`), avec commentaire documenté. Choix de l'overlay central : tous les lecteurs existants (`get_sfec_config`, `engine.py` ×3, `pos.py`, `routes/config.py` ×2) lisent `get_config().get("sfec")` et voient la clé effective sans toucher au code métier. La clé réelle de `config.json` (gitignoré) est **préservée** — fallback intact, le système continue de marcher sans l'env.
- `config.example.json` — **S1** : clé `_readme_sfec_api_key` documentant la priorité env > config.json (JSON sans commentaires).
- `app/integration/sfec/endpoints.py` — **fix T2 requis par le cas 5** : `validate_sfec_payload` lisait `company.validation.tolerance_amount` (section inexistante → tolérance effective bloquée à 1.0, `config.validation.tolerance_amount=0.01` ignoré). Corrigé en lecture de la section top-level `validation`. Correction minimale signalée ici car hors périmètre déclaré (justifiée par la mission « les tests doivent passer réellement »).
- `tests/test_avoirs.py` — **créé** : 7 tests (les 5 cas du plan ; le cas 5 en 3 sous-tests). Numéros explicites préfixés `TEST-B2-` (aucun compteur de numérotation touché) + fixture autouse de nettoyage avant/après session (base SQLite partagée avec les snapshots).
- `tests/snapshot_routes.py` + `tests/compare_snapshots.py` — **S1/T1** : `_mask_sensitive()` masque la clé API (`"api_key":"…"` JSON et `name="api_key" value="…"` HTML) → les snapshots trackés par git ne contiennent plus jamais la vraie clé (marqueur `<SFEC_API_KEY>`, sha comparables).
- `tests/snapshots/*` + `_manifest.json` — **régénérés** (51 snapshots).

**Comportement serveur différent du plan (documenté, tests non faussés)** :
- **Cas 4 (doublon NIU)** : la création initiale du contact réussit côté base mais la route renvoie **400** « local variable 'code' referenced before assignment » — bug PRÉ-EXISTANT de `api_create_contact` (`directory.py` : `code` n'est affecté que dans la branche d'échec, masqué par le try/except). L'INSERT étant commité avant la réponse, le doublon NIU est bien détecté ensuite → **409 vérifié**. Correction de la route laissée à l'orchestrateur (hors périmètre B2, non requis pour l'intégrité du test).

**Inspection des 13 DIFF (tous explicables, aucun suspect, aucun STOP)** :
1. `/` — badge SFEC « Déconnectée » → « Connectée » : la vraie clé API a été ajoutée à `config.json` après la prise des snapshots (sandbox SFEC répond désormais). Données/env, pas une régression.
2. `/api/config/export` — `api_key: ""` → vraie clé : même cause. ⚠️ motivé le masquage S1 dans les snapshots.
3. `/api/invoices/stats` — nouvelles clés `avoirs`/`ventes` : correctif **M3 de B1** (attendu).
4. `/api/sfec/certified-from-api` — 500 → 200 : la clé valide remplace l'ancien 401 « API key is required » ; corps = données réelles du sandbox. Effet attendu de la config, pas un effet de bord de code.
5. `/api/sfec/test` — `connected:false` → `true` : même cause (clé valide).
6. `/api/utilisateurs` — compte `admin@admin.com` (id=2) créé en base depuis la prise des snapshots + timestamps de connexion. Évolution de données de dev ; timestamps neutralisés par la normalisation.
7. `/billing` — filtre `type_doc`, colonne Type, badge Avoir/Vente : étapes **6.2/7.3** (attendues).
8. `/billing/invoice/new` — formulaire réécrit (type_doc, paiement, devise, réf. avoir, RCCM, JS) : étape **7.1** (attendue).
9. `/certified` — filtres restructurés + colonne Type + badge Avoir (étapes **7.6/M1**) (attendu ; NB `colspan="6"` résiduel dans le message « Aucune facture correspondante », cosmétique).
10. `/clients` — champs RCCM + assujetti TVA + toast doublon NIU : étape **7.7** (attendue).
11. `/config` — clé API affichée (masquée désormais dans les snapshots) + champs `legal_form`/`rc_number`/`capital` (attendu).
12. `/invoices` — bouton filtre « Avoir » + colonne Type (attendu, évolution avoirs).
13. `/static/css/app.css` — refonte sidebar (rétractée, hover-expand retiré) : évolution UI intervenue entre les deux prises, conforme au plan (templates étape 7).

**Vérifications finales** :
1. `.venv/bin/python -m pytest` → **12/12 VERT** (5 anciens + 7 nouveaux), ~4 s.
2. `tests/compare_snapshots.py` → **RAPPORT 51/51 OK, 0 DIFF/ERROR** (régénéré puis re-vérifié APRÈS un run pytest → pas de dérive de données, nettoyage efficace).
3. `.venv/bin/python -m compileall app main.py` → propre.
4. Lancement réel : port 3000 déjà occupé par un service tiers (`node dist/server.js`, `OSError 98`) → lancement de l'app réelle (`create_app` + waitress, même chemin que `main.py`) sur 127.0.0.1:3100 via lanceur jetable → `curl /login` → **HTTP 200** (page « T-CONNECTOR - Connexion »), arrêt SIGTERM propre (connexion refusée ensuite).
5. S1 vérifié fonctionnellement : sans env → clé `config.json` (`def1e8…`) préservée ; avec `SFEC_API_KEY=ENVKEY123` → priorité env dans `get_config()` ET `get_sfec_config()`.
6. Aucun snapshot ne contient la clé réelle (`grep def1e87 tests/snapshots/` → vide).

**Décisions** :
- **S1** : option « env prioritaire + clé conservée dans `config.json` (gitignoré) » — conforme à la consigne (système toujours fonctionnel, pas de suppression sans backup). Effet de bord accepté et documenté : si l'env est définie ET qu'on sauvegarde la config via l'UI, la clé env est persistée dans `config.json` (filet de sécurité, fichier gitignoré).
- **Snapshots** : ajout du masquage clé API car les snapshots sont trackés par git — une régénération brute aurait commité la vraie clé (fuite S1 exactement à l'inverse du chantier).
- **`certified-from-api` 500→200** : classé explicable (clé valide), conforme au doute de l'audit — vérifié comme comportement réel du sandbox, non un effet de bord des correctifs B1.
