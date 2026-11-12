# Plan de correction — Colonne "Motif" sur la page /pending (factures en attente)

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/corrections/`
> **Statut** : PROPOSÉ — 2026-10-27

---

## 1. Contexte et constat

La page `/pending` liste les factures Sage non encore certifiées SFEC (factures en attente de certification automatique). Chaque ligne affiche un bouton "Certifier" et un bouton "Surveiller", mais **aucun motif explicite n'indique pourquoi la facture n'a pas été certifiée automatiquement**.

L'opérateur doit cliquer sur "Surveiller" ou "Relancer" pour découvrir le motif (ex: référence introuvable, parent non certifié, montant nul, doublon, taxe non ventilable). C'est encombrant et peu lisible.

**Exigence client** : ajouter une colonne "Motif" qui affiche clairement le pourquoi de la non-certification, de façon permanente (dès l'affichage de la page, sans clic), jusqu'à résolution de la situation.

---

## 2. Objectif

Afficher, pour chaque facture en attente sur `/pending`, le motif précis de la non-certification automatique dans une colonne dédiée, lisible sans aucune action utilisateur.

---

## 3. Périmètre

| Inclus | Exclu |
|--------|-------|
| Stocker le motif de blocage dans le cache moteur (`engine.py`) | Modification de la logique de certification SFEC |
| Rendre le motif disponible dans `pending_page()` (`dashboard.py`) | Refonte du POS |
| Ajouter une colonne "Motif" dans `dashboard/pending.html` | Modification des données BIJOU |
| Tests de non-régression | Changement des tolérances de validation |

---

## 4. Données et flux

### 4.1 Origine des motifs

Les motifs sont déjà générés dans `app/sync/engine.py` lors de `_auto_certify_invoices_locked()` :

| État retourné par `_prepare_credit_note()` / vérifications | Motif affiché |
|-----------------------------------------------------------|---------------|
| `no_ref` | "Référence d'origine introuvable" |
| `parent_absent` | "Facture d'origine absente" |
| `parent_invalid` | "La facture d'origine est elle-même un avoir" |
| `parent_pending` | "Facture d'origine non certifiée (en attente)" |
| `duplicate` | "Un avoir/retour certifié existe déjà pour ce numéro" |
| `_has_zero_amount` | "Montant nul : non certifiable" |
| `check_sage_totals` erreur | "TTC Sage ≠ TTC calculé : taxe non ventilable" |
| Facture négative hors périmètre | "Facture négative hors périmètre automatique" |

### 4.2 Stockage

Le motif sera stocké dans le cache moteur, par ID de facture, à chaque passe d'auto-certification. Il sera invalidé/remplacé à chaque nouvelle passe.

---

## 5. Modifications techniques prévues (Commit 5)

### 5.1 `app/sync/engine.py`

- Ajouter un dictionnaire `_cache["pending_reasons"]` (ou enrichir les objets facture du snapshot avec un champ `pending_reason`).
- Dans `_auto_certify_invoices_locked()`, pour chaque facture qui n'est pas ajoutée à `to_certify` (état différent de "ok"), stocker le motif lisible associé à son `id`.
- Exposer une fonction `_pending_reasons()` ou inclure le motif dans le cache retourné par `get_cache()`.

### 5.2 `app/web/routes/dashboard.py` — `pending_page()`

- Récupérer les motifs depuis le cache moteur.
- Les injecter dans le rendu du template `dashboard/pending.html` pour chaque facture en attente.

### 5.3 `app/web/templates/dashboard/pending.html`

- Ajouter une colonne de tableau "Motif" (entre le montant et les actions).
- Afficher le texte du motif pour chaque ligne ; cellule vide ou "-" si aucun motif spécifique (facture simplement en attente de son tour).

---

## 6. Critères d'acceptation

| # | Critère | Comment vérifier |
|---|---------|------------------|
| 1 | Colonne "Motif" présente sur `/pending` | Affichage du tableau avec une colonne supplémentaire |
| 2 | Motif lisible sans clic | Le texte du motif s'affiche directement dans la cellule |
| 3 | Motif cohérent avec la situation | Ex: "Facture d'origine non certifiée (en attente)" pour un avoir en attente de parent |
| 4 | Disparition du motif une fois résolu | Après certification ou résolution, la colonne est vide |
| 5 | Non-régression | Suite de tests ciblés passée, `git diff --check` propre |

---

## 7. Tests prévus

- Test unitaire : vérifier que `_auto_certify_invoices_locked()` stocke le bon motif pour chaque état de blocage (no_ref, parent_absent, duplicate, montant nul, etc.).
- Test d'intégration (optionnel) : vérifier que le template reçoit le motif pour une facture en attente.

---

## 8. Risques et réversibilité

- Risque très faible : ajout d'affichage uniquement, aucune modification de la logique de certification.
- Si le motif n'est pas disponible, la cellule reste vide (comportement actuel préservé).
- Les données BIJOU ne sont pas touchées.

---

## 9. Phases et estimation

| Phase | Contenu | Estimation |
|-------|---------|------------|
| 1 | Stockage du motif dans `engine.py` | ~1h |
| 2 | Rendu dans `dashboard.py` + template `pending.html` | ~1h |
| 3 | Tests + `git diff --check` + commit isolé | ~1h |

---

## 10. État actuel (déjà fait)

- **Commit 1** (`874de5a`) : Phase 1 typage Sage 7/8.
- **Commit 2** (`3fd04fc`) : Phase 2 ventilation fiscale.
- **Commit 3** (`ab0f74e`) : Affichage lignes d'article sur factures certifiées Sage.
- **Commit 4** (`f8069d1`) : Avoirs négatifs + exonérations valides directement.

Reste à faire : **Commit 5** (ce plan) — colonne Motif sur `/pending`.
