# Rapport de suivi — Validation des tests automatiques via le sandbox SFEC

## Objectif
Tous les tests automatiques de certification SFEC doivent passer par la clé `sandbox_api_key` définie dans `config.json` pour valider l'implémentation contre le sandbox SFEC (`https://sandbox.api.sfec.gouv.cg`).

## Configuration
- Clé sandbox : `sandbox_api_key` (présente dans `config.json`, gitignoré)
- URL sandbox : `https://sandbox.api.sfec.gouv.cg`
- URL production : `https://api.sfec.gouv.cg`

## Règles de validation
Une implémentation est considérée "juste" uniquement si :
1. Les tests unitaires passent (calcul des taxes, centime, ventilation)
2. Les tests d'intégration passent (envoi réel au sandbox SFEC, retour 201 Created)
3. Le code de taxe supplémentaire `TAR` (3 %) est accepté par SFEC

## Progression

| Étape | Statut | Détail |
|-------|--------|--------|
| Clé sandbox valide | ✅ | Testée via GET `/api/v1/invoices/{id}` — retourne 200 |
| Certification salesInvoice + taxe 3% (code TAR) | ✅ | Testée via POST `/api/v1/invoices` — retourne 201 (certification_number `62C9628D61043E33EB9E`) |
| Tests unitaires existants | ✅ | 47 tests passés (test_sfec_centimes + test_sfec_avoirs) |
| Tests d'intégration sandbox | ✅ | Créés : `tests/test_sfec_sandbox_integration.py` (4 tests) |
| Tests avoirs/retours multiples | ✅ | Implémenté dans le fichier d'intégration |
| Tests exonération | ✅ | Implémenté dans le fichier d'intégration |

## Tests d'intégration implémentés
Fichier : `tests/test_sfec_sandbox_integration.py`

- `test_sandbox_connectivity` : vérifie que la clé sandbox est valide (GET facture existante)
- `test_sandbox_certify_sales_invoice_3pct_tar` : facture vente avec taxe 3% (code TAR) → 201
- `test_sandbox_certify_credit_note` : avoir (creditNote) avec référence parent → 201
- `test_sandbox_certify_exoneree` : facture TTC = HT (exonérée) → 201
- `test_sandbox_multiple_returns_same_parent` : parent + 2 retours partiels sur même facture → 201 chacun

## Exécution
Les tests d'intégration ne s'exécutent que si la variable d'environnement
`TCONNECTOR_TEST_SFEC_SANDBOX=1` est définie :

```bash
export TCONNECTOR_TEST_SFEC_SANDBOX=1
.venv/bin/python -m pytest -q tests/test_sfec_sandbox_integration.py -m integration
```

## Résultats d'exécution (2026-10-28)

| Test | Statut | Détail |
|------|--------|--------|
| test_sandbox_connectivity | ✅ | Clé sandbox valide, GET facture existante → 200 |
| test_sandbox_certify_sales_invoice_3pct_tar | ✅ | Vente + taxe 3% (code TAR) → 201 |
| test_sandbox_certify_credit_note | ✅ | Avoir avec référence parent → 201 |
| test_sandbox_certify_exoneree | ✅ | Facture exonérée (TTC=HT) → 201 |
| test_sandbox_multiple_returns_same_parent | ✅ | Parent + 2 retours partiels → 201 chacun |

**Total : 5/5 tests d'intégration passent contre le sandbox SFEC.**

**Tests unitaires : 47/47 passent** (test_sfec_centimes.py + test_sfec_avoirs.py)

## Hydratation de la certification (2026-10-29)

Un test sandbox dédié a vérifié le correctif de récupération de l'UUID après
un `POST /api/v1/invoices`.

| Élément | Valeur |
|---|---|
| Facture de test | `TEST-HYDRATE-20261029070124` |
| Type client | comptoir / `individual` sans NIU |
| POST `identifier` | `1598840DE6BA5300F55E` (signature courte) |
| GET par numéro | UUID `f20c844b-8bf2-479e-be2d-862f24a0cfe2` |
| Statut SFEC | `certified` |
| Date de certification | `2026-10-29T06:01:25.742Z` |

Le helper `_hydrate_certification_result()` a bien conservé l'UUID dans
`sfec_id`, même lorsque le POST contenait déjà la signature courte.

Détail du correctif : `docs/checking/suivi/solution-hydratation-certification-sfec.md`.

## Conclusion

L'implémentation est validée contre le sandbox SFEC :
- Le code de taxe supplémentaire `TAR` (3 %) est accepté par SFEC
- Les avoirs (creditNote) avec référence parent sont certifiés
- Les factures exonérées (TTC = HT) sont certifiées
- Plusieurs retours partiels sur une même facture parent sont autorisés
- Les taxes supplémentaires sont correctement ventilées dans `additional_taxes`

## Date de dernière mise à jour
2026-10-29