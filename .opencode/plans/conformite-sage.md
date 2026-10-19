# Plan de mise en conformité Sage — T-CONNECTOR

> Référence : https://documentation.eu.erp.sage.com/fr-FR/Sales/Sales_credit_memo.htm (Avoir de vente)
> Base cible : Sage 100 / BIJOU (`F_DOCENTETE`, `F_DOCLIGNE`), `DO_Domaine=0`, vente `DO_Type=6`, avoir `DO_Type=7`
> Statut : **à valider** — l'implémentation démarre après validation de ce plan.

## 0. État des lieux (constats vérifiés, sans modification)

| # | Constat | Preuve |
|---|---------|--------|
| 0.1 | **Aucun push Sage n'a jamais abouti** (0 `Facture ecrite dans Sage` en logs) | `data/output.log`, `app/integration/sage/writer.py:189-207` |
| 0.2 | `F_DOCLIGNE.PF_Num` et `cbMarq` sont `NOT NULL` sans défaut et absents de l'`INSERT` → erreur `515` systématique | `INFORMATION_SCHEMA.COLUMNS`, `output.log:1684,2494,3966` |
| 0.3 | Connexion `autocommit=True` → en-têtes `F_DOCENTETE` orphelines (sans lignes) pour `FA000001/2/3`, puis le pull les marque `synced_sage=1` → plus de retry | `app/integration/sage/database.py:61-63`, `app/sync/bidirectional.py:150-179` |
| 0.4 | `reference` 18 car. > `DO_Ref` 17 → erreur `8152` (cas `FA000002`, contourné manuellement avec `teste`) ; max Sage observé = 17 (`FA00007/08`) | `output.log:2259+`, tailles colonnes relevées |
| 0.5 | Champ `reference` du formulaire **sans `maxlength`** (le `maxlength=17` de `form.html:144` concerne le NIU) | `app/web/templates/billing/form.html:82` |
| 0.6 | `reference_invoice_id` (lien avoir→facture) **jamais écrit en Sage**, forcé à `''` à la lecture (`I1`) → avoirs orphelins côté Sage | `writer.py:178,250-261` |
| 0.7 | **Aucun motif d'avoir** nulle part (ni SQLite, ni formulaire, ni Sage) alors que la doc l'exige en prérequis | `grep motif|reason_code` → 0 résultat |
| 0.8 | Lignes d'avoir en **saisie libre**, non reprises de la facture d'origine, sans contrôle qté/prix (doc : lignes issues de la facture, pas d'ajout direct) | `form.html:309-314`, `invoices.py:327-371` |
| 0.9 | **1 seul avoir par facture** (index unique `db.py:331-333` + `_validate_avoir`) ; Sage autorise avoirs partiels/multiples | `db.py:331-333`, `invoices.py:53-56` |
| 0.10 | Numérotation `AV{:06d}` unique côté connecteur, conservée en Sage (pas de n° brouillon → n° final à la comptabilisation comme FR/ZA) | `db.py:462-476`, `writer.py:176` |
| 0.11 | Avoirs envoyés à SFEC en montants **positifs** (`total_amount > 0` requis) mais écrits en Sage en **négatif** (`sign=-1`) | `endpoints.py:322`, `writer.py:151,179-183` |

Tailles colonnes Sage relevées (BIJOU) : `DO_Piece 13`, `DO_Tiers 17`, `DO_Ref 17`, `DL_Design 69`, `AR_Ref 19`, `CO_No` nullable. `PF_Num` attendu `''`, `cbMarq` = compteur `1,2,3…`.

---

## Phase 0 — Réparer le push Sage (bloquant, préalable à tout)

**Objectif :** `POST /api/invoices/<id>/push-sage` → `{"success":true}`, `synced_sage=1`, lignes présentes en Sage.

- [ ] 0.1 `writer.py` — `INSERT F_DOCLIGNE` : ajouter `PF_Num=''` et `cbMarq` (= n° de ligne, `numero_ligne`).
- [ ] 0.2 `writer.py` — appliquer `_trunc()` à tous les champs texte facture/lignes avec les longueurs §0 (`DO_Ref 17`, `DO_Piece 13`, `DO_Tiers 17`, `DL_Design 69`, `AR_Ref 19`, `CO_No 17`) + `logger.warning` à chaque troncature effective.
- [ ] 0.3 `writer.py` — passer l'écriture entête+lignes en **transaction explicite** (rollback si une ligne échoue → plus d'en-tête orpheline). Ne pas changer le pool `autocommit` global : encadrer par `BEGIN/COMMIT/ROLLBACK` ou basculer `autocommit=False` sur ce curseur.
- [ ] 0.4 Vérifier qu'aucune autre colonne `NOT NULL` sans défaut de `F_DOCENTETE` n'est omise (l'entête passe aujourd'hui, à re-valider après 0.3).
- [ ] 0.5 Recette : pousser `FA000002` (ref `teste`) → `synced_sage=1`, `sage_piece` renseigné, lignes visibles en Sage ; re-tester `FA000001` (TVA 0 réparée) et `FA000003`.

**Critère d'acceptation :** 3 factures poussées de bout en bout, `sync_log` statut `ok`, montants Sage = montants SQLite (signe `-1` pour avoirs).

## Phase 1 — Garde-fous longueurs (éviter le retour du 8152)

- [ ] 1.1 `form.html:82` — `maxlength="17"` sur le champ `reference` (+ `title` explicatif).
- [ ] 1.2 `invoices.py` `create/update_invoice` — validation backend : `reference ≤ 17`, `designation ≤ 69`, `code_compte ≤ 17`, `code_article ≤ 19`, `tiers_code ≤ 17`, `numero ≤ 13` → `ValueError` claire (400) au lieu de l'erreur ODBC brute.
- [ ] 1.3 Appliquer la même validation aux tickets POS (`pos.py`) qui alimentent `invoices`.
- [ ] 1.4 Recette : tentative de création avec ref 18 car. → rejet propre côté UI.

## Phase 2 — Lien avoir ↔ facture d'origine visible dans Sage ⭐ (structurant)

Contexte : `DO_Ref` est déjà pris par la réf. commande, et la doc montre un lien cliquable facture↔avoir.

- [ ] 2.1 **Décision à valider** — option A : écrire `reference_invoice_id` dans une zone libre Sage (ex. `DO_Ref` si inutilisée chez toi, ou champ libre `DO_Coord01-04` / commentaire) ; option B : table de correspondance côté connecteur + affichage du lien dans le dashboard uniquement ; option C : les deux.
- [ ] 2.2 Implémenter l'option retenue en écriture (`writer.py`) **et** en lecture (`read_invoices_from_sage`, lever la contrainte `I1` `reference_invoice_id=''`).
- [ ] 2.3 Afficher le lien dans `/billing/invoice/<id>` (déjà fait côté SQLite, `billing.py:97-99`) et vérifier l'aller-retour pull → lien conservé.
- [ ] 2.4 Recette : avoir créé → origine visible/cliquable des deux côtés.

## Phase 3 — Motif d'avoir (prérequis doc Sage)

- [ ] 3.1 Ajouter colonne `motif_avoir TEXT DEFAULT ''` (migration `db.py:_migrate`) + champ formulaire (liste : retour, erreur prix, remise, autre) + validation si `type_doc='avoir'`.
- [ ] 3.2 Pousser le motif en Sage (champ à choisir avec 2.1 si zone libre dispo, sinon `notes`/commentaire ligne) et l'inclure dans le payload SFEC si supporté.
- [ ] 3.3 Recette : avoir sans motif → rejet 422 ; avec motif → motif visible en Sage et sur le PDF.

## Phase 4 — Lignes d'avoir reprises de la facture d'origine

- [ ] 4.1 À la sélection de la facture d'origine (`form.html`), **pré-remplir** les lignes (désignation, qté, prix, TVA) depuis `GET /api/invoices/<id>` ; quantités modifiables à la baisse uniquement par défaut.
- [ ] 4.2 Contrôles backend : qté créditée ≤ qté facturée par ligne, prix ≤ prix facturé (tolérance paramétrable), au moins 1 ligne, `total_amount > 0` (contrainte SFEC existante).
- [ ] 4.3 Cumul des avoirs existants sur la même origine dans le contrôle (prépare la phase 5).
- [ ] 4.4 Recette : avoir à qté supérieure → rejet ; avoir partiel → accepté.

## Phase 5 — Politique multi-avoirs (décision produit)

- [ ] 5.1 **Décision à valider** — option A : garder « 1 avoir max » (documenter l'écart vs Sage, coût nul) ; option B : avoirs partiels multiples avec contrôle du cumul ≤ facture (lever l'index unique `db.py:331-333` → index non-unique + contrôle cumul dans `_validate_avoir` + adaptation `engine.py:591-594`).
- [ ] 5.2 Implémenter l'option retenue + recette (2ᵉ avoir : rejet en A, accepté si cumul OK en B).

## Phase 6 — Numérotation (documentation, pas de code sauf demande)

- [ ] 6.1 Acter par écrit : n° `AV{:06d}` généré à la création, définitif, repris tel quel en Sage (`DO_Piece`), pas de renumérotation à la comptabilisation. Vérifier l'acceptation côté comptable (chrono/continuité).
- [ ] 6.2 Si exigence légale CG similaire FR/ZA : prévoir double compteur (brouillon `AV-TMP` → final à validation) — chiffré séparément.

## Phase 7 — Assainissement données existantes (avec validation explicite)

- [ ] 7.1 Lister les en-têtes Sage orphelines (sans lignes) : comparer `F_DOCENTETE` vs `F_DOCLIGNE` pour `FA000001/2/3` (+ autres `synced_sage=1, sage_piece=''`).
- [ ] 7.2 **Après ton feu vert écrit** : `DELETE` les orphelines en Sage, `reset synced_sage=0, sage_piece=''` côté SQLite (la certif SFEC de `FA000002` est conservée en SQLite et réécrite après re-push), re-push phase 0.
- [ ] 7.3 Vérifier `FA000001` (TVA 0 réparée : `HT=5000 TVA=0 TTC=5000`) après re-push.

## Phase 8 — Recette globale & non-régression

- [ ] 8.1 Matrice : vente 18 % / 5 % / 0 % × avoir total/partiel × push Sage × certif SFEC × PDF.
- [ ] 8.2 `pytest -q` vert + `GET /api/connectivity` `online:true` pendant un push (pas de régression réseau).
- [ ] 8.3 Test de charge léger : push de 5 factures en rafale (détecte les `8152`/`515` résiduels et les timeouts).
- [ ] 8.4 Vérification TVA 0 (à ta charge, phase déjà livrée) : création `5000 × 0 %` → détail `TVA=0 TTC=5000 Exonéré=5000`.

---

## Ordre d'implémentation proposé

1. Phase 0 (bloquant) → 2. Phase 1 (garde-fous) → 3. Phase 7 (cleanup, avec ton feu vert) → 4. Phase 2 (lien origine) → 5. Phase 3 (motif) → 6. Phase 4 (lignes) → 7. Phase 5 (décision multi-avoirs) → 8. Phase 6 (doc) → 9. Phase 8 (recette).

**Décisions attendues de toi avant démarrage :** choix 2.1 (zone Sage pour le lien), choix 5.1 (1 avoir max vs partiels multiples), feu vert 7.2 (DELETE Sage), confirmation 6.1 (numérotation définitive OK).
