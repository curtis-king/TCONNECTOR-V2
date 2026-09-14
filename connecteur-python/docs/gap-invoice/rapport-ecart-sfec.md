# Rapport d'écart facture SFEC — connecteur T-CONNECTOR

> Contenu de référence : **SFEC API — certifier une facture** (https://docs.sfec.gouv.cg/api/certify).
> Facture analysée : impression `/certified/<invoice_id>/print` (`app/web/routes/dashboard.py` → `dashboard/print.html`).

## 1. Ce que la SFEC exige (critères obligatoires de certification)

Source : doc officielle « Certifier une facture » (endpoint `POST /api/v1/invoices/certify`).

### 1.1 Identification & statut

| Champ | Type | Obligatoire | Statut connecteur |
|---|---|---|---|
| `invoice_id` | string | ✅ | ✅ envoyé |
| `invoice_type` (`salesInvoice`/`creditNote`) | string | ✅ | ⚠️ envoyé, **non affiché** |
| `taxpayer_niu` (vendeur) | string/null | ❌ | ✅ envoyé |
| `invoice_subject` | string/null | ❌ | ✅ envoyé |
| `invoice_due_date` | string/null | ❌ | ✅ envoyé |
| `reference_invoice_id` (avoir) | string/null | ❌ | ✅ envoyé |
| `sciet` | string/null | ❌ | — |

### 1.2 Informations destinataire

| Champ | Type | Obligatoire | Statut connecteur |
|---|---|---|---|
| `recipient_type` | string | ✅ | ⚠️ envoyé, **non affiché** |
| `recipient_name` | string | ⚠️ selon type | ✅ envoyé + affiché |
| `recipient_niu` | string | ⚠️ selon type | ✅ envoyé + affiché |
| `recipient_rccm` | string | ❌ | ⚠️ envoyé, **non affiché** |
| `recipient_address` | string | ⚠️ selon type | ✅ envoyé + affiché |
| `recipient_phone` | string | ⚠️ selon type | ✅ envoyé + affiché |
| `recipient_email` | string | ⚠️ selon type | ⚠️ envoyé, **non affiché** |
| `is_recipient_taxable` | boolean | ✅ | ⚠️ envoyé, **non affiché** |

> Règle : si `recipient_type = business|government`, `recipient_name` **et** `recipient_niu` sont requis.
> `individual` : champs libres. `government`/`foreign` : adresse, tél, email requis.

### 1.3 Montants & totaux (tous `number`, sans devise, cohérents)

| Champ | Obligatoire | Statut connecteur |
|---|---|---|
| `subtotal` | ✅ | ✅ envoyé + affiché (Total HT) |
| `total_tax_t_amount` (TVA 18%) | ✅ | ✅ envoyé + affiché |
| `total_tax_r_amount` (TVA 5%) | ✅ | ✅ envoyé + affiché |
| `total_exempt_amount` | ✅ | ⚠️ envoyé, **non affiché** |
| `total_tax_amount` | ✅ | ✅ envoyé |
| `discount_amount` (remise globale) | ✅ | ⚠️ envoyé, **non affiché** |
| `total_line_discount_amount` (remises articles) | ✅ | ⚠️ envoyé, **non affiché** |
| `additional_cent_tax` | ✅ | ⚠️ envoyé (=0), non affiché |
| `electronic_stamp_duty` | ✅ **doit = 0** | ✅ envoyé = 0 |
| `total_amount` (TTC global) | ✅ | ✅ envoyé + affiché |
| `amount_due` | ✅ | ✅ envoyé + affiché |

### 1.4 Paiement

| Champ | Obligatoire | Statut connecteur |
|---|---|---|
| `currency` | ✅ | ✅ envoyé + affiché |
| `payment_method` | ✅ | ✅ envoyé + affiché |
| `payment_reference` | ❌ | ⚠️ envoyé, non affiché |
| `payment_date` | ❌ | ⚠️ envoyé, non affiché |

### 1.5 Articles (`items[]`)

| Champ | Obligatoire | Statut connecteur |
|---|---|---|
| `designation` | ✅ | ✅ affiché |
| `classification_code` | ❌ | ⚠️ envoyé, non affiché |
| `type` (`product`/`service`) | ✅ | ⚠️ envoyé, **non affiché** |
| `unit_price` / `quantity` | ✅ | ✅ affichés |
| `subtotal` / `discount_amount` / `discount_type` | ✅ | ⚠️ envoyés, non affichés |
| `net_amount` | ✅ | ✅ affiché |
| `tax_rate` | ✅ | ✅ affiché |
| `tax_amount` | ✅ | ⚠️ envoyé, **non affiché** |
| `total_amount` | ✅ | ⚠️ envoyé, **non affiché** |

### 1.6 Règles de forme & cohérence (doc SFEC)

- **`electronic_stamp_duty` doit être `0`** (timbre électronique plus calculé par le contribuable ; le SFEC le génère). ✅ connecteur conforme.
- **Montants sans devise** dans les valeurs (XAF/francs séparés). ✅
- **Cohérence des totaux** : `total_ttc = subtotal + total_tax_t + total_tax_r + additional_cent_tax`, détaillés. ⚠️ calculé côté connecteur par `round(...)`.
- **Remises affichées** : les remises (globales et par article) doivent être **visibles sur lignes séparées** de la facture (règle de présentation : distinction campagne), pas seulement déduites dans les totaux.
- Les articles requièrent un **`type`** explicite (produit vs service, via `article_nature`) — nécessaire pour la certification.

---

## 2. Ce que la facture du connecteur affiche aujourd'hui

`app/web/routes/dashboard.py` — `print_certified` (lignes 136-194) :

- **Vendeur** : nom, adresse, NIU, RCCM
- **Destinataire** : nom, NIU, adresse, téléphone (NIU + nom)
- **Paiement** : méthode (payment_method), montant dû
- **Articles** : désignation, quantité, prix unitaire, taux TVA, montant net
- **Totaux** : HT, TVA 18%, TVA 5%, TTC, montant dû
- **Certification** : statut, n° certificat, signature courte, signature complète, date, QR code

---

## 3. Écart : ce qui **manque** dans la facture du connecteur

### 3.1 Champs critères présents mais invisibles à l'impression

| Écarts (afficher pour être conforme "papier") | Bloc |
|---|---|
| `invoice_type` (vente / **avoir**) — critique pour les avoirs/crédit-note | En-tête |
| `recipient_type` (business/individual/government/foreign) | Destinataire |
| `is_recipient_taxable` (assujetti TVA) | Destinataire |
| `recipient_email` | Destinataire |
| `recipient_rccm` | Destinataire (si renseigné) |
| `electronic_stamp_duty` (doit afficher 0 ou la valeur SFEC) | Totaux |
| `additional_cent_tax` si > 0 | Totaux |
| `discount_amount` (remise globale) — doit être sur **ligne séparée** | Totaux |
| `total_line_discount_amount` (remises article — ligne séparée) | Totaux |
| `total_exempt_amount` (montant exonéré) — ligne dédiée | Totaux |
| `reference_invoice_id` (si avoir : n° de la facture d'origine) | En-tête |
| **`type` de chaque article** (produit/service) | Articles |
| **`tax_amount` et `total_amount`** par article (TVA ligne + total TTC ligne) | Articles |
| `classification_code` par article | Articles |

### 3.2 Règles TOUTES conforme / bonnes pratiques

| Règle SFEC | État |
|---|---|
| `electronic_stamp_duty = 0` | ✅ |
| Montants sans devise dans les valeurs | ✅ |
| Devise + méthode de paiement affichées | ✅ |
| QR code + signature + n° certification affichés | ✅ |
| Montants arrondis et cohérents côté envoi | ✅ |
| Remises **déduites** des totaux (calcul) | ✅ (mais non affichées en ligne) |

---

## 4. Recommandations

### Priorité 1 — Conformité obligatoire (bloquant)
1. **Afficher `invoice_type`** dans l'en-tête (bandeau « FACTURE / AVOIR / CREDIT NOTE »).
2. **Afficher les remises séparément** :
   - `total_line_discount_amount` → ligne « Remise articles » ;
   - `discount_amount` → ligne « Remise globale ».
3. **Afficher le `type`** (article/service) — colonne ou mention dans la désignation.
4. **Afficher la TVA par article** : colonne `tax_amount` (et éventuellement `total_amount` TTC ligne).

### Priorité 2 — Complétude / lisibilité
5. `recipient_type` + `is_recipient_taxable` dans le bloc destinataire.
6. Ligne dédiée au montant exonéré (`total_exempt_amount`).
7. `recipient_email` et `recipient_rccm` si présents.
8. `electronic_stamp_duty` : afficher la ligne « Timbre » (= 0 conforme, ou valeur si SFEC la renvoie).

### Priorité 3 — Détails
9. `additional_cent_tax` si > 0.
10. `classification_code` par article.
11. Numéro SFEC (`sfec_id` / identifier) sous le numéro de facture.

---

## 5. Points validés (rien à corriger)

- ✅ Occupation correcte des totaux HT / TVA 18% / TVA 5% / TTC / dû.
- ✅ `electronic_stamp_duty = 0`.
- ✅ Signature, QR, n° certification, date certification tous affichés.
- ✅ Escapage (`_esc`) de toutes les valeurs côté rendu.
