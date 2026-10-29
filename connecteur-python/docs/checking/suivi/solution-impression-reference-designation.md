# Solution — Référence article et désignation sur la facture certifiée

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/checking/suivi/`
> **Statut** : IMPLÉMENTÉ — 2026-10-29
> **Périmètre** : impression navigateur `/certified/<uuid>/print`.

---

## 1. Symptôme

Pour la facture `FCDFG016`, le PDF « Facture cpta » affichait une ligne
article sans :

- **Référence** article ;
- **Désignation**.

L’en-tête contenait `autre test` dans le champ Référence, mais les colonnes
`Référence` et `Désignation` de la ligne étaient vides.

Attendu pour cette facture :

```text
Référence  : GRAVURE
Désignation: Gravure sur Or ciselé
```

---

## 2. Cause racine

### 2.1 Lignes Sage non normalisées

`fetch_doc_lines()` renvoie les champs Sage :

```text
description
code_produit
article_design
article_unit
```

La route d’impression lisait :

```text
designation
code_article
unite
```

Les colonnes étaient donc présentes côté Sage, mais ignorées à l’affichage.

### 2.2 Données SFEC dans `items_json`

Le GET SFEC renvoie les lignes dans `items_json`, pas dans `items`.
La route ne lisait que `items`, donc elle ne pouvait pas utiliser la
désignation SFEC en repli.

Pour `FCDFG016` :

```json
{
  "designation": "Gravure sur Or ciselé",
  "subtotal": 588000,
  "net_amount": 693840,
  "tax_amount": 105840,
  "quantity": 24,
  "unit_price": 24500
}
```

`net_amount` est le montant TTC ; il ne doit pas être utilisé comme HT.
Le montant HT de ligne est `subtotal`, ou `amount_after_discount` lorsqu’il
est présent.

---

## 3. Correctif

### 3.1 `app/web/routes/dashboard.py`

Ajout de helpers :

- `_normalize_print_line()` : normalise une ligne Sage ou locale :
  - `code_article` ← `code_article` / `code_produit` / `AR_Ref` ;
  - `designation` ← `designation` / `description` / `article_design` ;
  - `unite` ← `unite` / `article_unit`.
- `_sfec_invoice_items()` : lit `items` ou `items_json` (liste ou chaîne
  JSON).
- `_sfec_item_ht()` : détermine le HT de ligne sans confondre avec le TTC.

La route `/certified/<invoice_id>/print` :

- normalise les lignes Sage avant fusion ;
- fusionne SFEC + Sage par index ;
- affiche en priorité la référence article Sage (`GRAVURE`) ;
- affiche la désignation SFEC ou Sage ;
- utilise le HT réel de ligne (`subtotal` / `amount_after_discount`) ;
- renseigne le champ **Référence** de l’en-tête avec la première référence
  article disponible, avec repli sur la référence de facture locale.

---

## 4. Tests

Fichier ajouté : `tests/test_print_certified_invoice.py`.

Cas couverts :

- normalisation `description` → `designation` et `code_produit` →
  `code_article` ;
- lecture de `items_json` quand `items` est absent ;
- affichage de `GRAVURE` et `Gravure sur Or ciselé` sur la page
  `/certified/<uuid>/print` ;
- total de ligne à `588 000`, et non `693 840` ;
- repli SFEC quand Sage ne fournit pas de ligne.

Commande :

```bash
cd connecteur-python
.venv/bin/python -m pytest tests/test_print_certified_invoice.py -q
```

---

## 5. Validation manuelle

Ouvrir :

```text
http://localhost:3000/certified/93e873b3-186a-4498-9ab5-99842be826a9/print
```

Vérifier :

```text
Référence  : GRAVURE
Désignation: Gravure sur Or ciselé
Total HT   : 588 000
```

Ne pas modifier les données Sage ni SQLite pour corriger l’affichage :
le correctif est uniquement dans la lecture et le rendu.

### Résultat du contrôle réel — 2026-10-29

La route a été exécutée avec les données réelles de `FCDFG016` :

```text
HTTP 200
GRAVURE                 : 2 occurrences (en-tête + ligne)
Gravure sur Or ciselé   : 1 occurrence
588 000                 : 5 occurrences
693 840                 : 0 occurrence
```

Le montant TTC `693 840` n’est donc plus utilisé comme HT de ligne.
