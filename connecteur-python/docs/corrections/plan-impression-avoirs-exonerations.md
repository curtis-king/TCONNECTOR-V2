# Plan de correction — Impression, avoirs négatifs et exonérations

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/corrections/`
> **Statut** : PROPOSÉ — 2026-10-27

---

## 1. Contexte et constats

### 1.1 Rubriques d'impression

| Rubrique | Route | Bouton impression | Cible |
|----------|-------|-------------------|-------|
| Page `/certified` (liste) | `certified_page()` (`dashboard.py:355`) | Bouton "Imprimer" par facture | `/certified/<id>/print` |
| Impression facture certifiée | `print_certified()` (`dashboard.py:446`) | — | `dashboard/print.html` |
| Impression POS ticket | `/print` | — | `pos/ticket_print.html` |

Le bouton "Imprimer" sur `/certified` pointe déjà vers `/certified/<id>/print` (`certified.html:84`), qui utilise `print_certified`.

Le **Commit 3** (`ab0f74e`) a déjà ajouté un fallback lecture directe Sage (`fetch_doc_lines` via `_print_sage_lines`) dans `print_certified` pour afficher les lignes d'article (désignation, référence, quantité, PU HT, remise, total HT) quand la facture est absente de la base locale SQLite.

### 1.2 Avoirs négatifs

Dans Sage, toute facture de vente avec `montant_ttc < 0` est un avoir (remboursement). Le client utilise fréquemment le **Type 8** (facture de retour de vente, impact inventaire géré par Sage).

Exigences :
- Une facture négative ne doit **pas** être bloquée comme "A_SURVEILLÉ" si elle est un avoir.
- Le payload SFEC doit contenir des **montants positifs** (SFEC ne valide pas les sommes négatives ; le signe est porté par le type de pièce `creditNote`).
- Les avoirs/retours multiples sur une même facture d'origine sont autorisés (déjà implémenté, Phase 1 Commit 1).

### 1.3 Exonérations

Une facture avec `TTC = HT` (aucune TVA facturée) est une exonération.

Exigence :
- Plus besoin de faire valider l'exonération : elle peut être certifiée directement.
- Supprimer le blocage `check_sage_totals` qui renvoie actuellement `"TTC = HT ... exoneration a confirmer"`.

---

## 2. Objectif

1. Confirmer que l'impression des factures certifiées (y compris depuis `/certified`) affiche les lignes d'article (déjà fait, Commit 3).
2. Autoriser la certification des avoirs négatifs avec envoi de montants positifs à SFEC.
3. Valider directement les factures exonérées (TTC = HT) sans blocage "exonération à confirmer".

---

## 3. Périmètre

| Inclus | Exclu |
|--------|-------|
| `app/integration/sfec/endpoints.py` : `check_sage_totals()` et `db_invoice_to_sfec()` | Logique de certification SFEC (API externe) |
| `app/sync/engine.py` : `_should_mark_credit_failure()`, `_prepare_credit_note()`, `_auto_certify_invoices_locked()` | Refonte du POS |
| `app/web/routes/dashboard.py` : `print_certified()` (déjà corrigé, Commit 3) | Modification des données BIJOU |
| `tests/test_sfec_centimes.py`, `tests/test_sfec_avoirs.py` | Changement des tolérances de validation |

---

## 4. Modifications techniques prévues (Commit 4)

### 4.1 `app/integration/sfec/endpoints.py` — `check_sage_totals()`

**Supprimer** le bloc de blocage exonération :

```python
if abs(sage_ttc - sage_ht) <= tolerance:
    return ["TTC = HT ({} FCFA): aucune TVA facturee - exoneration a "
            "confirmer".format(sage_ttc)]
```

Remplacer par un passage direct (aucune erreur renvoyée pour TTC = HT). Les autres garde-fous (HT absent, taxe additionnelle inhabituelle > max_additional_tax_rate, écart TTC) sont conservés.

### 4.2 `app/integration/sfec/endpoints.py` — `db_invoice_to_sfec()`

Vérifier que les montants négatifs d'un avoir sont passés en positif dans le payload :
- `quantity`, `unit_price`, `montant_tva` déjà passés en `abs()` quand `is_credit_note` (déjà impliqué).
- `total_amount`, `subtotal`, `amount_due` doivent être positifs pour un `creditNote`.
- Le type de pièce `creditNote` porte le signe, pas les montants.

### 4.3 `app/sync/engine.py`

#### `_should_mark_credit_failure()`
Élargir à toute facture négative traitée comme avoir :

```python
return invoice.get("type_doc") == "avoir" or _as_float(invoice.get("montant_ttc"), 0) < 0
```

#### `_prepare_credit_note()`
Pour une facture avec `montant_ttc < 0` et **aucune référence parent** :
- Ne pas renvoyer `"no_ref"` → A_SURVEILLÉ.
- Préparer le `creditNote` avec :
  - `external_credit_note_number` = numéro de la facture (Sage)
  - `original_sfec_invoice_number` = numéro de la facture (best effort)
  - `reference_invoice_id` = vide ou numéro de la facture

#### `_auto_certify_invoices_locked()`
Une facture négative traitée comme avoir doit pouvoir être ajoutée à `to_certify` pour certification en positif (pas ignorée).

---

## 5. Critères d'acceptation

| # | Critère | Comment vérifier |
|---|---------|------------------|
| 1 | Facture certifiée depuis `/certified` affiche les lignes d'article | Imprimer via `/certified/<id>/print` : tableau lignes rempli |
| 2 | Facture exonérée (TTC = HT) | `check_sage_totals(inv, p)` renvoie `[]` |
| 3 | Avoir négatif (`montant_ttc < 0`) | Payload `invoice_type == "creditNote"`, montants `total_amount` positifs |
| 4 | Avoir négatif sans référence | Certifié (pas A_SURVEILLÉ) |
| 5 | Non-régression | Suite ciblée passée, `git diff --check` propre |

---

## 6. Tests prévus

### `tests/test_sfec_centimes.py`
- `test_exonere_ttc_egal_ht_valide_directement` : facture TTC = HT, `check_sage_totals` renvoie `[]`, payload accepté.

### `tests/test_sfec_avoirs.py`
- `test_facture_negative_devient_credit_note_sans_reference` : facture `montant_ttc < 0`, pas de référence → `creditNote` positif, pas de marquage A_SURVEILLÉ.
- `test_facture_negative_montants_positifs_payload` : vérifie que `total_amount`, `subtotal`, `amount_due` sont positifs.

---

## 7. Risques et réversibilité

- Risque faible : modifications isolées sur 2 fichiers + tests.
- Suppression du blocage exonération : une facture réellement sans TVA passera en certification sans alerte (comportement demandé par le client).
- Les données BIJOU ne sont pas touchées.
- Si le fallback Sage d'impression échoue, le comportement actuel (totaux uniquement) est conservé.

---

## 8. Phases et estimation

| Phase | Contenu | Estimation |
|-------|---------|------------|
| 1 | Modifications `endpoints.py` + `engine.py` | ~1h |
| 2 | Tests ciblés + `git diff --check` | ~1h |
| 3 | Commit isolé + déploiement chez le client | ~1h |

---

## 9. État actuel (déjà fait)

- **Commit 1** (`874de5a`) : Phase 1 typage Sage 7/8.
- **Commit 2** (`3fd04fc`) : Phase 2 ventilation fiscale.
- **Commit 3** (`ab0f74e`) : Affichage lignes d'article sur factures certifiées Sage (fallback `print_certified`).

Reste à faire : **Commit 4** (ce plan) — avoirs négatifs + exonérations.
