# Bugs certification SFEC des factures deja certifiees

Date : 2026-10-28
Branche : `update/alu-congo`

## Contexte

Une facture visible sur `/billing` etait presentee comme « A certifier » alors
qu'elle existait deja sur SFEC. Un clic sur « Certifier » renvoyait :

```text
Facture deja certifiee SFEC ou synchronisee Sage
success false
```

Le meme probleme peut se produire pour une facture `A_SURVEILLER` dont les
donnees Sage ont ete corrigees, ou pour une facture certifiee directement sur
SFEC sans passer par le connecteur.

## Cas observes

### Facture FCDFG013

- UUID SFEC fourni : `ed1120d5-0ca3-4207-b80e-92213ed2a690`.
- Le detail SFEC renvoie `invoice_number = FCDFG013`, et non `FCDFG015`.
- Statut SFEC : `invoice_status = certified`,
  `certification_status = certified`.
- Statut local : `CERTIFIE`, avec le numero de certification
  `605C5F022F6A727DF3E6`.
- Aucun correctif de donnees n'est necessaire pour cette facture.

### Facture FCDFG015

- Statut local : `A_SURVEILLER`.
- `synced_sage = 1`, `sage_piece = FCDFG015`.
- Aucun numero de certification local.
- Absente de SFEC sandbox : un parcours complet de la liste paginee
  (587 factures) ne contient pas `FCDFG015`.
- Derniere erreur de validation enregistree :
  - `recipient_niu` ne contient pas 16 ou 17 caracteres ;
  - `items[0].tax_rate = 20.0`, alors que SFEC attend `0`, `5` ou `18` ;
  - `TTC Sage = 510000.0` contre `TTC calcule = 514250.0`
    (ecart de `4250.0` FCFA).

Cette facture ne peut donc pas etre certifiee tant que les donnees Sage ne
sont pas corrigees. Le probleme visible est neanmoins un probleme d'affichage
et de routage interne : le connecteur affiche « A certifier » puis bloque avec
un message trompeur.

## Bugs identifies

### Bug 1 - `A_SURVEILLER` affiche comme « A certifier »

Fichier : `app/web/templates/billing/list.html`

La fonction `sfecCell()` traite `CERTIFIE`, `DEJA_CERTIFIE`, `EN_COURS` et
`ERREUR`, mais pas `A_SURVEILLER`. Ce statut tombe dans le cas par defaut :

```javascript
return '<span class="badge badge-info">A certifier</span>' + cb;
```

La facture affiche donc un bouton « Certifier » alors qu'elle est en anomalie
et ne doit pas etre renvoyee directement.

### Bug 2 - Confusion entre verrou d'edition et verrou de certification

Fichier : `app/web/routes/config.py`, fonction `_certify_local_invoice()`.

Le code utilise `invoice_engine.is_invoice_locked(inv)`, qui retourne vrai des
que la facture est synchronisee avec Sage (`synced_sage`, `sage_piece`), meme
si elle n'est pas certifiee SFEC.

Consequence : une facture Sage non certifiee ne peut jamais passer par le
circuit de certification manuelle et recoit un 409 générique :

```text
Facture deja certifiee SFEC ou synchronisee Sage
```

Le message est faux : la facture n'est pas certifiee SFEC. Le verrou d'edition
(immuabilite de la facture) et l'etat de certification SFEC sont deux notions
differentes.

### Bug 3 - Les factures `A_SURVEILLER` ne sont jamais reconsultees sur SFEC

Fichier : `app/sync/engine.py`, fonction `_auto_certify_invoices_locked()`.

Le statut `A_SURVEILLER` est ignore avant la verification du lookup SFEC :

```python
if sfec_statut in ("EN_COURS", "ERREUR", "A_SURVEILLER"):
    pending_reasons[_id] = "Statut SFEC : {}".format(sfec_statut)
    continue
```

Si la facture a ete certifiee directement sur SFEC, ou si une certification
SFEC existait deja, elle reste indefiniment `A_SURVEILLER` en local.

### Bug 4 - La recuperation SFEC ne met pas a jour le miroir SQLite

Fichier : `app/sync/engine.py`, fonction `_handle_already_certified()`.

La recuperation ecrit dans Sage via `write_sfec_to_invoice()` et met a jour le
cache Sage, mais pas le miroir SQLite lu par `/billing`. Le bouton peut donc
rester visible jusqu'a la prochaine synchronisation Sage, meme apres une
recuperation reussie.

### Bug 5 - Pas de verification SFEC avant la validation du payload

Fichiers : `app/sync/engine.py` (`certify_single()`) et
`app/integration/sfec/endpoints.py` (`certify_invoice()`).

Une facture deja certifiee sur SFEC peut avoir des donnees Sage devenues
invalides pour la validation locale (NIU, taux de TVA, totaux). Le code valide
le payload avant de verifier l'existence de la facture sur SFEC, et renvoie
donc une erreur 422 au lieu de recuperer la certification existante.

## Route SFEC

Le client SFEC utilisait deja les bonnes routes externes :

- `POST /api/v1/invoices` pour certifier ;
- `GET /api/v1/invoices/{invoice_number}` pour lire une facture ;
- `GET /api/v1/invoices?page=...&pageSize=...` pour lister.

Le endpoint local `POST /api/sfec/certify` est la route interne du connecteur
appelee par le navigateur. Il ne remplace pas l'appel SFEC : il doit ensuite
utiliser `SfecClient`. Le probleme n'etait donc pas l'URL SFEC, mais le blocage
intervenu avant l'appel.

## Correctif attendu

1. Traiter `A_SURVEILLER` comme une anomalie dans `/billing`, avec un lien vers
   `/pending` et sans bouton « Certifier ».
2. Separer le verrou d'edition du verrou de certification SFEC.
3. Deleguer la certification des factures Sage non certifiees au circuit Sage
   (`certify_single()`), et non au seul miroir SQLite.
4. Verifier SFEC avant la validation du payload et recuperer une certification
   existante.
5. Mettre a jour immediatement SQLite apres une recuperation ou une
   certification reussie, et supprimer l'anomalie associee.
6. Ne plus renvoyer le message ambigu « Facture deja certifiee SFEC ou
   synchronisee Sage ».

## Tests de non-regression prevus

- Facture deja certifiee sur SFEC avec payload local invalide : recuperation
  sans erreur 422.
- Facture `A_SURVEILLER` presente dans le lookup SFEC : statut local corrige.
- Recuperation SFEC : SQLite mis a jour et anomalie supprimee.
- Facture Sage non certifiee : `/api/sfec/certify` delegue a `certify_single`.
- `A_SURVEILLER` ne produit plus de bouton « Certifier » dans `/billing`.

## Donnees a corriger dans Sage

`FCDFG015` doit etre corrigee dans Sage avant toute nouvelle tentative :

- NIU client sur 16 ou 17 caracteres ;
- taux de TVA ramene a une valeur acceptee (`0`, `5` ou `18`) ;
- coherence du TTC avec les lignes et la TVA.

Sans ces corrections, la facture restera en anomalie, mais l'interface devra
l'indiquer clairement au lieu de proposer une certification impossible.

## Suivi de correction

Correction appliquee le 2026-10-28 :

- `_certify_local_invoice()` distingue desormais preuve SFEC, facture Sage et
  facture locale ; une facture deja certifiee renvoie un succes, pas un 409.
- Les factures Sage non certifiees sont deleguees a `certify_single()` avec
  l'identifiant `domaine-type-piece`.
- `certify_single()` consulte SFEC par numero avant la validation du payload.
- `_auto_certify_invoices_locked()` consulte le lookup SFEC avant d'ignorer un
  statut `A_SURVEILLER`.
- `_handle_already_certified()` met a jour SQLite et supprime l'anomalie meme
  si l'ecriture Sage est differee.
- `/billing` affiche `A_SURVEILLER` comme « A surveiller » avec un lien vers
  `/pending`, sans bouton de certification directe.

Tests de non-regression ajoutes :

- `tests/test_sfec_already_certified.py` ;
- `tests/test_sfec_certify_number.py` (delegation Sage, deja certifiee, UI
  `A_SURVEILLER`).

Verification locale du 2026-10-28 :

- `FCDFG013` : la route interne renvoie `success=true`,
  `already_certified=true`, certification `605C5F022F6A727DF3E6`.
- `FCDFG015` : l'identifiant Sage reconstruit est `0-6-FCDFG015` ; la facture
  sera envoyee au circuit Sage puis a la validation SFEC, sans 409 trompeur.
- Donnees Sage de `FCDFG015` toujours a corriger avant certification reelle.
