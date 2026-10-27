# Plan de correction — Alignement des taxes sur le modèle API SFEC (openapi.yaml)

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/corrections/`
> **Statut** : PROPOSÉ — 2026-10-27

---

## 1. Contexte et constat

L'OpenAPI officielle SFEC (`openapi.yaml`) définit le modèle de payload de certification `POST /api/v1/invoices`. L'implémentation actuelle (Commits 1 à 5) ne correspond pas à ce modèle :

| Champ | Modèle API officiel | Implémentation actuelle (faux) |
|-------|---------------------|--------------------------------|
| TVA 18 % | `total_tax_t_amount`, pas de code | `tax_code_profile` "1" → rôle "vat" |
| TVA 5 % | `total_tax_r_amount`, pas de code | (idem) |
| Centime additionnel | `additional_cent_tax` (champ numérique, pas de code) | `tax_code_profile` "202" → rôle "centime" |
| Taxes supplémentaires | `additional_taxes[]` : `tax_code` (chaîne), `tax_label`, `tax_amount`, `tax_rate` (pourcentage) | `tax_code_profile` "203" → rôle "additional" |
| `electronic_stamp_duty` | doit être `0` (exigence doc) | non forcé |
| `tax_rate` des items | enum `"0"`, `"5"`, `"18"` (chaîne) | — |

La doc ne donne **aucune liste de valeurs autorisées** pour `tax_code`. Le seul exemple est `"TAR"` (Taxe sur les activités rémunérées, 2,5 %). `"203"` est une supposition non confirmée — il faut un code alphanumérique accepté par SFEC, à confirmer avec le client ou SFEC.

---

## 2. Objectif

Aligner la ventilation fiscale et le payload SFEC sur le modèle officiel de l'API, sans modifier les données BIJOU ni la logique de certification.

---

## 3. Périmètre

| Inclus | Exclu |
|--------|-------|
| `config.example.json` et `config.json` (local) | Logique de certification SFEC (API externe) |
| `app/domain/taxes.py` | Refonte du POS |
| `app/integration/sfec/endpoints.py` | Modification des données BIJOU |
| `app/integration/sage/database.py` et `writer.py` (lecture des codes taxe) | Changement des tolérances de validation |
| `tests/test_sfec_centimes.py` | — |

---

## 4. Modifications techniques prévues

### 4.1 `config.example.json` et `config.json`

**Supprimer** `validation.tax_code_profile` (clés "1", "202", "203"). TVA et centime passent par leurs champs dédiés, pas par un profil de code.

**Garder** `validation.additional_tax_codes` et `validation.additional_tax_labels` pour mapper un taux → code SFEC, mais avec un **code alphanumérique à définir** (pas "203" par défaut, sauf si SFEC le confirme).

Exemple de structure attendue :

```json
"validation": {
  "strict_mode": true,
  "tolerance_amount": 0.01,
  "ttc_sage_tolerance": 1.0,
  "max_additional_tax_rate": 10,
  "cent_tax_rate": 5,
  "additional_tax_codes": {
    "3": "<CODE_SFEC_A_CONFIRMER>"
  },
  "additional_tax_labels": {
    "3": "Taxe supplémentaire 3 %"
  }
}
```

### 4.2 `app/domain/taxes.py`

- **Retirer** `DEFAULT_TAX_CODE_PROFILE` (ou le vider) : TVA et centime n'ont pas de code Sage à résoudre.
- **`resolve_additional_tax()`** : ne plus essayer de résoudre via `tax_code_profile`. Utiliser uniquement `additional_tax_codes` (taux → code SFEC) + `additional_tax_labels`, avec un fallback `"AUTRE"` si aucun code n'est configuré pour le taux.
- Conserver la logique de calcul : HT net → TVA 18/5 % → centime = 5 % de la TVA → résidu `TTC - HT - TVA - centime` → taxes supplémentaires dans `additional_taxes`.

### 4.3 `app/integration/sfec/endpoints.py`

- **`electronic_stamp_duty`** : forcer à `0` dans le payload (exigence doc).
- **`db_invoice_to_sfec()`** : s'assurer que `additional_cent_tax` est un champ numérique séparé (jamais absorbé dans le 3 %), et que les taxes supplémentaires vont uniquement dans `additional_taxes[]` (pas dans `total_tax_amount`, pas dans le centime).
- Vérifier que `total_tax_t_amount` (TVA 18 %) et `total_tax_r_amount` (TVA 5 %) sont bien renseignés, sans code associé.

### 4.4 `app/integration/sage/database.py` et `writer.py`

- Ne plus utiliser les codes de taxe Sage (`DL_CodeTaxe1/2/3`, `DO_CodeTaxe1/2/3`) pour déterminer le rôle fiscal d'une taxe. Ces colonnes peuvent rester lues à titre informatif mais ne doivent plus alimenter `tax_code_profile`.
- La ventilation fiscale repose uniquement sur : HT des lignes, taux de ligne (0/5/18), TTC Sage.

---

## 5. Critères d'acceptation

| # | Critère | Comment vérifier |
|---|---------|------------------|
| 1 | `tax_code_profile` retiré de la config | `config.example.json` et `config.json` ne contiennent plus la clé |
| 2 | TVA 18 % → `total_tax_t_amount`, pas de code | Payload de la facture de référence |
| 3 | TVA 5 % → `total_tax_r_amount`, pas de idem | Idem |
| 4 | Centime → `additional_cent_tax` (champ numérique, pas de code) | Idem |
| 5 | Taxe de 3 % → `additional_taxes[]` avec `tax_code`, `tax_label`, `tax_amount`, `tax_rate: 3` | Idem |
| 6 | `electronic_stamp_duty = 0` | Dans le payload |
| 7 | `tax_rate` des items : `"0"`, `"5"`, `"18"` (chaîne) | Dans le payload |
| 8 | `validate_sfec_payload()` et `check_sage_totals()` vides pour la référence `26FAZ07297` | Test unitaire |
| 9 | `git diff --check` propre, tests ciblés passés | — |

---

## 6. Tests prévus

### `tests/test_sfec_centimes.py`
- Mettre à jour `test_reference_26faz07297()` : le `tax_code` de la taxe de 3 % doit être le code SFEC validé (pas `"203"` par défaut si non confirmé).
- Ajouter un test : `electronic_stamp_duty == 0` dans le payload.
- Ajouter un test : `tax_rate` des items vaut `"18"` (chaîne) pour une ligne à 18 %.

---

## 7. Risques et réversibilité

- Risque moyen : changement du `tax_code` de la taxe de 3 % (de `"203"` à un code à confirmer). Si SFEC rejette `"203"`, il faudra ajuster.
- La suppression de `tax_code_profile` n'impacte pas le calcul des montants, seulement la résolution des codes.
- Les données BIJOU ne sont pas touchées.
- Si le code SFEC n'est pas confirmé, utiliser `"AUTRE"` comme fallback et documenter qu'il faut le remplacer.

---

## 8. Phases et estimation

| Phase | Contenu | Estimation |
|-------|---------|------------|
| 1 | Corrections `config.example.json` / `config.json` + `taxes.py` | ~1h |
| 2 | Corrections `endpoints.py` (`electronic_stamp_duty = 0`, centime séparé) | ~1h |
| 3 | Corrections `database.py` / `writer.py` (retirer rôle fiscal des codes taxe) | ~1h |
| 4 | Mise à jour des tests + `git diff --check` | ~1h |
| 5 | Commit isolé + déploiement chez le client | ~1h |

---

## 9. État actuel (déjà fait)

- **Commit 1** (`874de5a`) : Phase 1 typage Sage 7/8.
- **Commit 2** (`3fd04fc`) : Phase 2 ventilation fiscale (avec `tax_code_profile` "203" — à corriger).
- **Commit 3** (`ab0f74e`) : Affichage lignes d'article sur factures certifiées Sage.
- **Commit 4** (`f8069d1`) : Avoirs négatifs + exonérations valides directement.
- **Commit 5** (`642ccda`) : Colonne Motif sur la page `/pending`.

Reste à faire : **Commit 6** (ce plan) — alignement des taxes sur le modèle API SFEC.

---

**En attente de votre validation.** Si vous validez, je passe à l'implémentation et au commit isolé.
