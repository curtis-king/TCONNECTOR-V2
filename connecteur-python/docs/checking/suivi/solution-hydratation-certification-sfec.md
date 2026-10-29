# Solution — Hydratation de la certification SFEC après le POST

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/checking/suivi/`
> **Statut** : IMPLÉMENTÉ — 2026-10-29
> **Branche** : `update/alu-congo`
> **Périmètre** : récupération du détail de certification après
> `POST /api/v1/invoices`. Aucun changement de données SQLite ou Sage.

---

## 1. Symptôme observé

Sur le sandbox SFEC, la certification d'une facture comptoir réussissait côté
SFEC, mais le connecteur pouvait laisser la facture en statut local
`EN_COURS` alors que la signature était déjà disponible.

Exemple observé :

```text
Facture de test : TEST-NIU-20261029065021-BFF200
UUID SFEC       : 3b9e0a6d-6808-45bf-a28f-b71bb3cf1fe3
POST réponse    : identifier = 73260A78CEB3CD200E40
GET /api/v1/invoices/73260A78CEB3CD200E40  -> 404
GET /api/v1/invoices/TEST-NIU-20261029065021-BFF200 -> certified
```

Le `POST` retournait donc dans `identifier` la **signature courte de
certification**, et non l'UUID de la facture. Les champs
`certification_short_signature`, `certification_status` et
`certification_signature` n'étaient pas tous présents dans la réponse
immédiate.

Le détail complet n'était récupérable qu'avec le **numéro de facture**.

---

## 2. Cause racine

Deux problèmes se cumulaient dans `app/integration/sfec/endpoints.py` :

1. `certify_invoice()` et `certify_sqlite_invoice()` interrogeaient
   directement `client.get_invoice(identifier)` avec la valeur du POST.
   Quand cette valeur était la signature courte, le GET renvoyait `404`.
2. Le module important à la fois `import time` et
   `from datetime import ... time`. Le second écrasait le premier.
   L'appel `time.sleep(delay)` de la boucle de polling levait donc
   `AttributeError: type object 'datetime.time' has no attribute 'sleep'`.
   Le polling ne pouvait pas fonctionner correctement.

Conséquences :

- la facture restait `EN_COURS` en local ;
- l'UUID SFEC n'était pas conservé dans `sfec_id` ;
- la signature courte pouvait être enregistrée à la place de l'UUID ;
- la certification n'était pas récupérée de façon fiable après un `POST`
  réussi.

---

## 3. Correctif appliqué

### 3.1 Hydratation par le numéro de facture

Ajout de `_hydrate_certification_result()` dans
`app/integration/sfec/endpoints.py`.

Après le `POST`, la fonction :

1. cherche d'abord les données de certification dans la réponse immédiate ;
2. si elles manquent **ou si l'identifiant reçu n'est pas un UUID**, interroge
   `GET /api/v1/invoices/{invoice_number}` ;
3. en cas d'échec, réessaie avec l'`identifier` retourné par le POST ;
4. répète l'opération jusqu'à obtenir la signature ou l'UUID, avec un délai
   configurable ;
5. fusionne la réponse détaillée avec la réponse du POST ;
6. conserve l'**UUID** dans `sfec_id` et `identifier`, pas la signature courte.

Le POST peut donc déjà contenir `certification_short_signature` : le
connecteur fait quand même un GET par numéro pour récupérer l'UUID. Si ce GET
échoue, il conserve la signature du POST sans bloquer la certification.

Le numéro de facture est prioritaire, car c'est la clé qui fonctionne sur le
sandbox SFEC.

### 3.2 Identifiant canonique

Ajout de `_canonical_sfec_id()` :

- priorité à `sfec_id`, puis `id`, puis `identifier` ;
- l'UUID du détail est conservé côté SQLite ;
- la signature courte reste disponible dans `certification_number`.

### 3.3 Correction du masquage de `time`

Le fichier importe désormais :

```python
import time
from datetime import date, datetime, time as datetime_time, timezone
```

et utilise `datetime_time.min` pour les valeurs de type heure.
`time.sleep()` fonctionne donc à nouveau dans la boucle d'hydratation.

### 3.4 Fonctions concernées

- `certify_invoice()` : certification du chemin Sage ;
- `certify_sqlite_invoice()` : certification du miroir SQLite ;
- `_hydrate_certification_result()` : récupération commune du détail ;
- `_canonical_sfec_id()` : choix de l'UUID canonique ;
- `_certification_lookup_keys()` : ordre des clés de recherche.

Le comportement métier n'a pas changé :

- aucun NIU n'est créé ou modifié ;
- aucune donnée Sage ou SQLite n'est migrée ;
- une certification non finalisée reste marquée `EN_COURS` ;
- une certification confirmée est enregistrée avec l'UUID et la signature.

---

## 4. Tests ajoutés

Fichier : `tests/test_sfec_certification_hydration.py`

Tests couverts :

- GET par numéro de facture avant la signature courte ;
- repli sur l'`identifier` du POST si le numéro est indisponible ;
- POST contenant déjà la signature : GET quand même pour obtenir l'UUID ;
- `certify_sqlite_invoice()` conserve l'UUID et enregistre `CERTIFIE` ;
- absence de détail : statut local `EN_COURS`, sans erreur de polling.

Commande exécutée :

```bash
cd connecteur-python
.venv/bin/python -m pytest tests -q --ignore=tests/perf
```

Résultat : **308 tests réussis**, aucune régression.

---

## 5. Validation sandbox

Le test sandbox antérieur avait déjà confirmé :

- facture `TEST-NIU-20261029065021-BFF200` acceptée ;
- payload `individual` sans `recipient_niu` ;
- TTC `1189.0` ;
- certification SFEC `73260A78CEB3CD200E40` ;
- date `2026-10-29T05:50:21Z` ;
- statut détaillé `certified`.

Après ce correctif, le connecteur retrouve ces informations via le numéro de
facture et stocke l'UUID. Un second test sandbox avec
`TEST-HYDRATE-20261029070124` a confirmé :

- POST : signature courte `1598840DE6BA5300F55E` ;
- GET par numéro : UUID `f20c844b-8bf2-479e-be2d-862f24a0cfe2` ;
- statut SFEC : `certified` ;
- date : `2026-10-29T06:01:25.742Z`.

La revalidation du helper sur cette facture a bien retourné :

```text
cert_number = 1598840DE6BA5300F55E
sfec_id     = f20c844b-8bf2-479e-be2d-862f24a0cfe2
invoice_status = certified
```

Même lorsque le POST contient déjà la signature courte, le connecteur
récupère donc l'UUID par le numéro de facture.

---

## 6. Limites et suites

- `FCDFG016` reste bloquée par le NIU invalide `546789` du client
  `Bague's en or`. Ce client n'est pas comptoir : le correctif NIU ne doit pas
  le convertir automatiquement en `individual`.
- Test final exécuté le 2026-10-29 sur `FCDFG016` via le chemin applicatif
  `certify_invoice()` : **échec local 422 avant tout appel HTTP**, avec
  `Validation SFEC echouee: recipient_niu doit contenir 16 ou 17 caracteres`.
  Le payload reste `recipient_type=business`, `recipient_niu=546789`,
  `TTC=699132.0`, une ligne. Aucune certification sandbox n'a donc été créée
  pour cette facture.
- La correction doit être faite côté Sage, ou la facture reclassée en vente
  B2C si elle correspond réellement à un particulier.
- L'acceptation de `recipient_type=individual` sans NIU doit être confirmée
  sur l'environnement de production SFEC ; le sandbox l'accepte.
- Aucun mapping de codes de taxe n'est inclus dans ce correctif.
