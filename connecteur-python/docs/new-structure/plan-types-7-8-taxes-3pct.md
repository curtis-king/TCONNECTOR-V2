# Plan de développement — Types Sage 7/8 et ventilation fiscale (centime + taxe 3 %)

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/new-structure/`
> **Statut** : PROPOSÉ — 2026-10-27.
> **Périmètre** : lecture Sage des factures/avoirs/retours, calcul des taxes, payload SFEC.
> **Hors périmètre** : données BIJOU, inventaire Sage, push des retours vers Sage, refonte du POS.

---

## 1. Décisions validées

### 1.1 Typage des documents Sage

La correspondance fournie par le client est confirmée par l’image de la table Sage :

| `DO_Type` | Libellé Sage | Traitement connecteur |
|---:|---|---|
| 6 | Facture de vente | `salesInvoice` |
| 7 | Facture d’avoir de vente | `creditNote` |
| 8 | Facture de retour de vente | `creditNote` + libellé « Retour » |
| 17 | Facture d’avoir d’achat | hors périmètre certification |
| 18 | Facture de retour d’achat | hors périmètre certification |

Le Type 8 a un impact stock dans Sage, mais **le connecteur ne doit pas gérer cet impact** : Sage met à jour l’inventaire. Côté SFEC, le Type 8 reste un `creditNote`, comme le Type 7.

### 1.2 Taxes du client

La facture `26FAZ07297` sert de cas de référence :

```text
Montant HT net : 14 094 340
TVA 18 %       :  2 536 981
Code 202, 0,9 %:    126 849   (centime = 5 % de la TVA)
Code 203, 3 %  :    422 830   (taxe supplémentaire)
Total taxes    :  3 086 660
Net à payer    : 17 181 000
```

Vérifications :

```text
126 849 = 5 % × 2 536 981   (arrondi Sage au franc)
422 830 = 3 % × 14 094 340  (arrondi Sage au franc)
14 094 340 + 2 536 981 + 126 849 + 422 830 = 17 181 000
```

Contraintes :

- Les montants de taxes ne sont **pas stockés** dans Sage : ils sont calculés à partir du HT, des taux/codes et du TTC.
- Le centime est **5 % de la TVA collectée**, jamais 5 % du HT.
- La taxe de 3 % est une **taxe supplémentaire**, pas de la TVA et pas un centime.
- Le payload SFEC doit ventiler séparément :
  - `total_tax_t_amount` : TVA 18 % ;
  - `total_tax_r_amount` : TVA 5 % ;
  - `additional_cent_tax` : centime ;
  - `additional_taxes` : taxe de 3 % (et autres taxes supplémentaires).
- `validation.tolerance_amount = 0.01` reste inchangé. Aucune absorption du 3 % dans le centime.

---

## 2. État actuel du code

### 2.1 Typage

- `app/integration/sage/database.py` : `_DOMAIN_CONFIG` contient `facture_type=6` et `avoir_type=7`, sans `retour_type`.
- Les requêtes de ventes utilisent `DO_Type IN (6, 7)` : le Type 8 est ignoré.
- `app/integration/sage/writer.py` code aussi `6/7` en dur et utilise `vente_type` au lieu de `facture_type`.
- `fetch_certified_invoices()` ne lit que `6/7`.
- `app/web/routes/dashboard.py` ne prévoit pas le Type 8 dans l’impression des lignes.
- `_build_credit_note_index()` interdit actuellement plusieurs avoirs/retours pour une même facture d’origine : incompatible avec des retours partiels multiples.

### 2.2 Taxes

- `fetch_doc_lines()` et `fetch_doc_lines_batch()` ne lisent que `DL_Taxe1` et calculent `montant_tva = DL_MontantHT × DL_Taxe1 / 100`.
- Les codes `DL_CodeTaxe1/2/3`, les taux `DL_Taxe2/3` et les colonnes d’en-tête `DO_CodeTaxe1/2/3`, `DO_Taxe1/2/3` ne sont pas exposés.
- `db_invoice_to_sfec()` calcule déjà :
  - le centime avec `compute_cent_tax(TVA)` ;
  - le 3 % comme résidu `TTC - HT - TVA - centime` ;
  - `additional_taxes` au niveau facture.
- Mais le résidu est la seule source du 3 % : il n’est pas recoupé avec les lignes ou les codes Sage.
- Le code SFEC de la taxe de 3 % vaut `AUTRE` par défaut, car `validation.additional_tax_codes` est vide.
- Le miroir local ne stocke pas les taxes supplémentaires ; ce n’est pas bloquant pour la certification, qui lit Sage, mais le dashboard ne peut pas les afficher.

---

## 3. Phase 1 — Lire et mapper les Types 7 et 8

### 3.1 Objectif

Faire entrer les avoirs (7) et les retours (8) dans le même flux de certification `creditNote`, sans les confondre avec des ventes, et sans gérer l’inventaire côté connecteur.

### 3.2 Modifications

1. **Configuration Sage**
   - Ajouter `retour_type = 8` à `_DOMAIN_CONFIG`.
   - Conserver `facture_type = 6` et `avoir_type = 7`.
   - Utiliser `get_domain_config()` dans `writer.py` au lieu des constantes locales.
   - Ajouter éventuellement une surcharge dans `config.json` pour les clients dont les types diffèrent.

2. **Requêtes de lecture**
   - Remplacer `DO_Type IN (6, 7)` par `DO_Type IN (6, 7, 8)` dans :
     - `fetch_sales_invoices()` ;
     - `_fetch_sales_invoices_fallback()` ;
     - `read_invoices_from_sage()` ;
     - `fetch_certified_invoices()`.
   - Mapper `7` et `8` vers `type_doc = "avoir"`.
   - Conserver `sage_type` dans l’objet facture et dans le miroir SQLite.

3. **Affichage et libellés**
   - `_credit_label()` : « Retour » si `sage_type == 8`, sinon « Avoir ».
   - `dashboard._print_sage_lines()` : inclure `retour_type` dans les types testés.
   - Les compteurs « ventes » excluent les Types 7 et 8.
   - `/certified` et `/pending` affichent les Types 7/8 avec le bon libellé.

4. **Avoirs/retours multiples**
   - Remplacer le contrôle anti-doublon basé sur le parent par un contrôle basé sur le numéro de l’avoir/retour (`external_credit_note_number` / `numero`).
   - Un même parent pourra avoir plusieurs retours partiels.
   - La détection de doublon d’un même avoir reste assurée par `sfec_lookup` et le statut local.

5. **Tests Phase 1**
   - Lecture d’une facture Type 6 → `salesInvoice`.
   - Lecture d’un avoir Type 7 → `creditNote`.
   - Lecture d’un retour Type 8 → `creditNote`, libellé « Retour ».
   - Type 8 présent dans `/certified` et dans le cache moteur.
   - Deux retours partiels sur la même facture parente ne sont pas marqués « doublon ».
   - Les achats Types 17/18 restent hors périmètre.

### 3.3 Critères d’acceptation

- Un document Type 8 est lu, stocké et certifiable comme `creditNote`.
- Aucun Type 8 n’est compté comme vente.
- Le Type 7 conserve son comportement actuel.
- Les tests de non-régression des avoirs passent.

---

## 4. Phase 2 — Moteur de ventilation fiscale

### 4.1 Objectif

Calculer les taxes à partir du HT, des taux/codes Sage et du TTC, puis produire un payload SFEC exact, sans stocker les taxes supplémentaires et sans relâcher la tolérance.

### 4.2 Principe

Le calcul doit être fait dans cet ordre :

1. **HT net** : somme des lignes nettes, ou `DO_TotalHT` en repli.
2. **TVA** : somme des TVA de lignes, répartie entre 18 % et 5 %.
3. **Centime** : `5 % × TVA collectée`, arrondi comme Sage.
4. **Résidu** : `TTC Sage - HT - TVA - centime`.
5. **Taxes supplémentaires** : décomposer le résidu selon les taux/codes Sage connus (3 %, 0,25 %, etc.).
6. **Contrôle final** : `HT + TVA + centime + taxes supplémentaires = TTC Sage`.
7. Si le résidu n’est pas décomposable : facture `A_SURVEILLER`, aucun payload approximatif.

Le TTC Sage est la source de vérité pour le montant final. Les taux/codes Sage servent à qualifier les composants, pas à remplacer le contrôle du TTC.

### 4.3 Lecture des taxes Sage

1. **Lignes**
   - Lire dynamiquement, si les colonnes existent :
     - `DL_CodeTaxe1`, `DL_CodeTaxe2`, `DL_CodeTaxe3` ;
     - `DL_Taxe1`, `DL_Taxe2`, `DL_Taxe3` ;
     - `DL_MontantHT`, `DL_MontantTTC`.
   - Conserver `taux_tva` / `montant_tva` comme champs principaux de TVA pour ne pas casser les appelants.
   - Ajouter `line["taxes"]` : liste de `{code, rate, base, amount, role}`.

2. **En-tête**
   - Lire, si présentes, `DO_CodeTaxe1/2/3` et `DO_Taxe1/2/3`.
   - Utiliser ces champs pour qualifier les taxes quand les lignes ne portent pas l’information.

3. **Référentiel des codes**
   - Résoudre les codes via `F_TAXE` si la table est disponible.
   - Sinon utiliser un profil configurable :
     ```json
     "validation": {
       "tax_code_profile": {
         "1":   {"role": "vat",        "rate": 18},
         "202": {"role": "centime",    "rate": 0.9},
         "203": {"role": "additional", "rate": 3, "sfec_code": "203",
                 "label": "Taxe supplémentaire 3 %"}
       }
     }
     ```
   - `sfec_code` est le code envoyé dans `additional_taxes.tax_code`.
   - Le code `203` est le code Sage imprimé sur la facture de référence. S’il est refusé par SFEC, il sera remplacé par `TAR`, `TAX_MUNI` ou le code officiel via la configuration, sans redéploiement.

4. **Cas du centime**
   - Un code/taux 202 / 0,9 % est un centime, pas une taxe supplémentaire.
   - Un code/taux 0,25 % sur une base à 5 % est également un centime.
   - Le centime n’est **jamais** ajouté à `additional_taxes`.
   - Il est recalculé sur la TVA ; la ligne Sage sert de contrôle.

### 4.4 Calcul et arrondis

- Les montants de la facture client sont en francs entiers.
- Pour les factures XAF, arrondir les taxes au franc entier, comme Sage.
- Exemple de référence :
  ```text
  TVA 18 %       = 2 536 981
  centime        = 126 849   (et non 126 849,05)
  3 %            = 422 830   (et non 422 830,20)
  total          = 17 181 000
  ```
- Si le calcul théorique donne des décimales, le résidu du TTC Sage doit être réparti de façon déterministe :
  1. arrondir la TVA selon les lignes ;
  2. calculer le centime ;
  3. calculer la taxe supplémentaire ;
  4. ajuster le dernier composant dans la limite du tolerance de contrôle, sans jamais dépasser `0.01` sur le total.
- `validation.tolerance_amount` reste à `0.01`. Le payload final doit être exact au centime/franc près.

### 4.5 Payload SFEC

Pour la facture de référence, le payload attendu est :

```json
{
  "subtotal": 14094340,
  "total_tax_t_amount": 2536981,
  "total_tax_r_amount": 0,
  "additional_cent_tax": 126849,
  "additional_taxes": [
    {
      "tax_code": "203",
      "tax_label": "Taxe supplémentaire 3 %",
      "tax_amount": 422830,
      "tax_rate": 3
    }
  ],
  "total_amount": 17181000
}
```

Règles :

- `items[].tax_amount` = TVA de ligne uniquement.
- `items[].net_amount` = HT net + TVA, sans centime ni 3 %.
- Ne pas utiliser `DL_MontantTTC` pour le total de ligne : il contient déjà toutes les taxes.
- `total_tax_amount` = TVA 18 + TVA 5, sans centime ni 3 %.
- `additional_cent_tax` = centime.
- `additional_taxes` = taxe(s) supplémentaire(s), dont le 3 %.

### 4.6 Repli sans lignes

Si les lignes sont absentes ou incomplètes :

- utiliser le ratio `TTC / HT` ;
- reconnaître les ratios connus :
  - `1.000` : exonéré ;
  - `1.0525` : 5 % + centime ;
  - `1.1890` : 18 % + centime ;
  - `1.2190` : 18 % + centime + 3 % ;
- si le ratio n’est pas reconnu ou si le résidu ne correspond à aucun profil : `A_SURVEILLER`.

### 4.7 Tests Phase 2

Créer ou étendre `tests/test_sfec_centimes.py` avec :

- facture `26FAZ07297` : payload exact ci-dessus ;
- facture 18 % + centime seule ;
- facture 5 % + centime ;
- facture 18 % + 3 % + centime ;
- facture mixte 18 %/5 % + 3 % ;
- ligne 202 / 0,9 % correctement classée en centime ;
- code 203 / 3 % correctement classé en `additional_taxes` ;
- arrondi au franc XAF ;
- résidu inconnu → `A_SURVEILLER`, pas de payload ;
- avoir/retour avec montants négatifs → payload positif ;
- `validate_sfec_payload()` et `check_sage_totals()` sans erreur sur le cas de référence.

### 4.8 Critères d’acceptation

- `26FAZ07297` produit un payload dont `total_amount = 17 181 000` et dont les quatre composants sont exacts.
- Le 3 % n’est jamais dans `additional_cent_tax`.
- Le centime n’est jamais dans `additional_taxes`.
- Un écart supérieur à `0.01` bloque la certification.
- Les factures sans ventilation fiable ne sont pas certifiées.

---

## 5. Phase 3 — Validation, déploiement et reprise

### 5.1 Tests d’intégration

- `pytest` ciblé :
  - `tests/test_sfec_centimes.py` ;
  - `tests/test_sfec_avoirs.py` ;
  - `tests/test_mirror_totals.py` ;
  - tests de lecture Sage / dashboard / certified.
- Vérifier `/certified` avec plus de 20 lignes : la pagination SFEC doit être corrigée séparément (déjà identifiée : `page_size=500` tronqué à 20).
- Vérifier `/pending` et les compteurs ventes/avoirs/retours.

### 5.2 Validation sandbox

1. Déployer sur l’environnement sandbox.
2. Configurer `validation.tax_code_profile` avec les codes client (1, 202, 203).
3. Tester `26FAZ07297` en certification.
4. Tester un avoir Type 7 et un retour Type 8.
5. Vérifier que le 3 % apparaît dans `additional_taxes` et que le total est accepté.
6. Vérifier qu’un retour partiel multiple sur une même facture n’est pas bloqué.
7. Contrôler les logs : aucune boucle de rejet, aucun `AUTRE` inattendu, aucun écart de total.

### 5.3 Déploiement client

1. Sauvegarde de la base locale et de `config.json`.
2. Déploiement du build.
3. Pull limité à 7 jours d’abord.
4. Contrôle des volumes Type 7/8 sur 7 jours.
5. Sync complète manuelle uniquement après validation des volumes.
6. Surveillance des premières certifications et des `A_SURVEILLER`.

### 5.4 Reprise / rollback

- Aucun schéma SQLite nouveau n’est indispensable : les taxes supplémentaires sont calculées à la volée.
- En cas de refus du code `203` par SFEC : changer `sfec_code` dans `config.json`.
- En cas de régression : revenir au build précédent et conserver les données locales.
- Ne jamais modifier les données BIJOU sans validation explicite.

---

## 6. Fichiers impactés

| Fichier | Phase | Modification |
|---|---:|---|
| `app/integration/sage/database.py` | 1, 2 | `retour_type`, requêtes `IN (6,7,8)`, lecture des codes/taux de taxe |
| `app/integration/sage/writer.py` | 1, 2 | utiliser `get_domain_config()`, inclure le Type 8, exposer les taxes de ligne |
| `app/sync/engine.py` | 1 | cache, libellés, avoirs/retours multiples |
| `app/sync/bidirectional.py` | 1 | miroir `type_doc` / `sage_type` |
| `app/integration/sfec/endpoints.py` | 2 | moteur de ventilation, payload, arrondis |
| `app/domain/taxes.py` (nouveau) | 2 | fonctions pures de calcul et de contrôle |
| `app/web/routes/dashboard.py` | 1 | lignes Sage Type 8, page `/certified` |
| `app/web/routes/sync_api.py` | 1 | liste `/api/certified/list`, libellé Retour |
| `config.example.json`, `config.json` | 2 | profil des codes de taxe |
| `tests/test_sfec_centimes.py` | 2 | cas 26FAZ07297 et variantes |
| `tests/test_sfec_avoirs.py` | 1, 2 | Types 7/8 et retours multiples |
| `tests/test_sage_pull_window.py` | 1 | types lus |

---

## 7. Découpage en commits

### Commit 1 — Types Sage 7/8

- Ajout de `retour_type`.
- Requêtes `IN (6,7,8)`.
- Mapping `creditNote`, libellé « Retour ».
- Avoirs/retours multiples.
- Tests Phase 1.

### Commit 2 — Ventilation fiscale

- Lecture des codes/taux de taxe.
- Moteur `HT/TTC → TVA + centime + taxe 3 %`.
- Payload SFEC et arrondis XAF.
- Tests `26FAZ07297` et variantes.

### Commit 3 — Validation et documentation

- Tests d’intégration.
- Mise à jour de ce plan / rapport.
- Aucun staging global, uniquement les fichiers du plan.

---

## 8. Risques et parades

| Risque | Parade |
|---|---|
| Le Type 7 est massivement présent (211 057 dans les logs) | Pull 7 jours d’abord, contrôle des volumes avant full sync |
| Le Type 8 est confondu avec une vente | Mapping explicite `7/8 → creditNote`, tests |
| Plusieurs retours sur une même facture | Contrôle d’unicité par numéro d’avoir/retour, pas par parent |
| Le code `203` est refusé par SFEC | Rendre `sfec_code` configurable, tester `TAR` / `TAX_MUNI` |
| Les taxes ne sont pas stockées | Calcul à partir des lignes, des codes/taux et du TTC |
| Arrondis Sage | Arrondi au franc XAF et contrôle du TTC exact |
| Résidu inexpliqué | `A_SURVEILLER`, jamais de payload approximatif |
| Tolérance relâchée | `validation.tolerance_amount = 0.01` inchangé |
| Impact stock du retour | Sage le gère ; le connecteur ne modifie pas l’inventaire |
| Données BIJOU | Aucune modification sans validation explicite |

---

## 9. Points à valider avant implémentation

1. **Type 8** : mapping `creditNote` + libellé « Retour », sans impact inventaire côté connecteur.
2. **Code SFEC du 3 %** : utiliser `203` par défaut (code Sage imprimé) ou `TAR`/`TAX_MUNI` si le client fournit le code officiel.
3. **Arrondi XAF** : arrondir les taxes au franc entier, comme sur la facture `26FAZ07297`.
4. **Retours multiples** : autoriser plusieurs avoirs/retours partiels sur une même facture d’origine.
5. **Périmètre** : le Type 8 est traité pour les ventes ; les Types 17/18 (achats) restent hors périmètre.
