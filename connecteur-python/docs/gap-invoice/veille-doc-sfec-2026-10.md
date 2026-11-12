# Veille doc SFEC — mises à jour détectées (recherche 2026-10-12)

> Sources : https://sfec.gouv.cg/api-sfec (vitrine, en vigueur 1er août 2026) +
> https://docs.sfec.gouv.cg/api + /api/faq + /api/authentication.
> Référence antérieure : pages `/api/certify` et `/api/invoices` lues en septembre 2026.

---

## 1. 🔴 Potentiellement critique — pagination `pageSize`

| | Septembre 2026 | Octobre 2026 (FAQ) |
|---|---|---|
| `pageSize > 20` | ramenée à 20 **silencieusement**, "pas de `400`" | **erreur `400`** |
| Plage valide | 1-20 (implicite) | **1-20**, défaut 10 (explicite) |

**Notre exposition** — `page_size=500` envoyé par :
- `app/sync/engine.py:250-260` (`sync_sfec_invoices`)
- `app/sync/engine.py:332-345` (`_build_sfec_lookup`)
- `app/integration/sfec/client.py:129-145` (`verify_by_invoice_number`)
- `app/web/routes/dashboard.py:99` (`/pending`, `list_invoices(page=1, page_size=500)`)

**Si le serveur applique le 400** : pulls SFEC en échec → lookup vide → auto-certif aveugle + `/pending` cassée.
**Action** : probe live sandbox `GET /api/v1/invoices?pageSize=500` (200 ou 400 ?) puis fix `500→20`
sur les 4 spots + pagination jusqu'à `totalPages` (déjà gérée par les boucles `while`).

## 2. 🟠 Changements confirmés (impact modéré)

- **Erreur `422` possible en validation** (FAQ : "sous peine d'une erreur `400`/`422`").
  Septembre disait "422 jamais utilisé". Déjà géré côté code (`ERREUR` sur 400/422) — à savoir pour la lecture des logs.
- **Erreur `500` avec champ `details`** (avant : `{message}` seul). Parser `details` dans
  `app/integration/sfec/client.py:58-83` pour des logs exploitables.
- **Clés via portail e-Facture + validation DGID** + consigne "ne jamais logger la clé".
  Audit à faire : `grep -rn "api_key" app/ --include="*.py"` hors `config/manager.py` et masquages snapshots.
- **En vigueur 1er août 2026** : nouveau comportement déjà actif, pas de période de grâce.

## 3. 🟢 Nouveau levier — spec téléchargeable `/openapi.yaml`

La FAQ pointe la spécification OpenAPI en téléchargement direct ( + explorateur `/api/reference`).
Action : télécharger + **diff automatique** du schéma (champs requis, enums `invoice_type`, formats)
contre notre payload (`app/integration/sfec/endpoints.py:120-314`).

## 4. Plan d'action proposé

1. Probe live sandbox (lecture seule) : `pageSize=500` → 200 ou 400 ? + `GET /openapi.yaml` → diff schéma.
2. Selon résultat : fix `page_size` 500→20 (4 spots) + parser `details` + audit `grep api_key` logs.
3. Re-run suite perf `pytest tests/perf -m perf -v` (les mocks paginent à 500 — aligner sur 20 si confirmé).
