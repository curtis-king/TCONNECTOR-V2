# Rapport de synthèse — Fiabilisation T-Connector (facturation, SFEC, Sage)

> **Période** : 14 → 24 septembre 2026. **Périmètre** : `connecteur-python` (copie Windows, celle qui tourne).
> **Objet** : remise en état complète du flux facturation → certification SFEC → écriture Sage BIJOU,
> puis inspection générale des pages avant présentation.

---

## 1. Fiabilisation du flux de facturation et des avoirs

| Problème | Cause | Résultat |
|---|---|---|
| Création d'avoir impossible (`NOT NULL`) | `reference_invoice_id` passé à `None` | Corrigé (`""` par défaut) |
| `19 values for 20 columns` à l'enregistrement | `INSERT invoice_lines` : 20 colonnes / 19 `?` | Requêtes alignées |
| `classification_code` NULL rejeté | valeur `None` dans colonne `NOT NULL` | Valeurs par défaut (`""`, `"fixed"`, `"product"`, `0.0`) |
| Page détail en erreur | 8 variables utilisées par le template mais jamais passées | Variables ajoutées au rendu |
| Écarts création/édition (type_doc, référence d'origine) | `update_invoice` trop restrictif, formulaire ne restaurant pas la référence | Plans A (domaine) + B (formulaire + requête éligible) appliqués |
| Centime additionnel hors TTC | `recalc_invoice_totals()` l'ignorait | Intégré : `TTC = HT + TVA + centime (5 % de la TVA 18 %)` + rapport + script backfill idempotent |

---

## 2. Conformité SFEC (Congo)

- **TVA** : SFEC n'accepte que `0/5/18 %` alors que Sage BIJOU n'avait que `0/20 %`. Décision : **migration propre côté Sage** (création `C18/C05/D18/D05` dans `F_TAXE` par copie de `C20/D20`, `TA_No` séquentiel), historique `20 %` intact, code figé sur `18`. Les avoirs `FA00001-03` restent lisibles.
- **`avoir_type = 7`** : `KeyError: 'avoir_type'` qui figeait le sync complet → clé ajoutée à `_DOMAIN_CONFIG`, **vérifiée en live** (`DO_Domaine=0` : 4 ventes type 6, 3 avoirs type 7).
- **NIU** : règle SFEC (16-17 caractères pour entreprise/État) appliquée en 3 étapes dans le formulaire — placeholder + hint par type de tiers, validation live avec compteur et bordures vert/rouge, normalisation auto (majuscules, sans espaces) + filet au submit. Garde-fous backend `422`/`409` conservés.
- **Connexion SQL Server** : accès[" depuis WSL diagnostiqué puis établi (`172.31.16.1,1433` + login `sageUser`), base `BIJOU` ONLINE vérifiée.

---

## 3. Intégration Sage BIJOU — le morceau le plus dur

**Symptôme** : `Push Sage: 0 ecrites, 11 erreurs` (`42000/50000`, erreur `82019`).

**Diagnostic en 2 temps** (triggers Sage lus en base) :
1. Le trigger `TG_INS_F_DOCENTETE` exige les **codes taxe** (`DO_CodeTaxe1/DL_CodeTaxe1` : `C18/C05/C00`), pas seulement les taux → `writer.py` complété (`_code_taxe()` branché sur les 2 `INSERT`).
2. Vraie cause racine : le trigger `TG_INS_CPTAF_DOCENTETE` (erreur `82019`) exige que **tout tiers d'une pièce de vente existe en `F_COMPTET` avec `CT_Type=0`** — or les factures portaient des codes locaux (`CLI-001`…) inexistants côté Sage, et **aucun push contacts n'existait**.

**Réalisé** :
- `write_contact_to_sage()` + `ensure_contact_in_sage()` (`writer.py`) : création idempotente en `F_COMPTET` (colonnes minimales sûres déduites des triggers, `CT_Type` jamais `2`, jamais d'écrasement silencieux).
- `push_contacts_to_sage()` + ordre `full_sync()` : **contacts avant factures** (`bidirectional.py`).
- Corrections associées : devise `smallint` → ISO (`0/3/5` → `XAF`), NIU → fallback `CT_Identifiant` (3 requêtes), `DO_ModeReglement` absent géré.

**Résultat mesuré** : `Push Sage: N ecrites, 0 erreur`, `FA000026` écrite avec `C18`, plus de `82019`.

---

## 4. Inspection générale des pages (avant présentation)

Audit croisé de **20 templates × 7 fichiers de routes**, puis corrections :
- `POST /api/contacts` → 500 à chaque succès (`code` non défini) : **corrigé** (`201` + fix du doublon `GET /api/contacts` qui tuait le handler Sage).
- Dropdown "facture d'origine" toujours vide (double `fetchall()`, `billing.py:208`) : **corrigé**.
- Page Utilisateurs vide (`{rows}` littéral + variable jamais passée) : **corrigée**.
- Impression certifiée : ligne d'origine jamais affichée (comparaison sur valeur échappée) : **corrigée** (`invoice_type == "creditNote"`).
- PDF : QR ré-encodé au lieu d'affiché (scan = `data:image…` illisible) : **corrigé** (`_decode_qr_image` : image SFEC exacte d'abord, repli texte sinon) ; ticket de caisse : plan de QR prêt.
- Page `/print` (captures) : `Remise globale: -0` → `0`, `Montant HT` sans décimales parasites, méthode en clair (`Virement`), ligne `Capital` vide masquée.

**Reste documenté** (procédures prêtes dans `docs/corrections/rapport-correctifs-pages-restants.md`, R1→R12) :
failles XSS (`clients`, `sales`), gate admin (aucun contrôle de rôle), export config exposant les secrets + import destructif (**à traiter en dernier**), notifications jamais sauvegardées, édition qui réinitialise paiement/devise, `?limit=abc` → 500, `fetch()` POS sans CSRF.

---

## 5. Livrables documentaires

| Fichier | Contenu |
|---|---|
| `docs/corrections/rapport-correctifs-3-blocs-restants.md` | Devise, NIU→`CT_Identifiant`, TVA (blocs copier-coller) |
| `docs/corrections/rapport-correctifs-pages-restants.md` | R1→R12 : procédures de tous les correctifs pages restants |
| `docs/integration/sage/plan-correction-push-sage.md` | Plan du chantier push Sage (phases + gates + rollback) |
| `docs/integration/progress/phase-1 à 5.md` | Suivi d'exécution du chantier push |
| `docs/checking/audit-plan-6-a-fin.md` | Audit de conformité (soldé : B1+B2, pytest 12/12, snapshots 51/51) |

---

## 6. Résultats mesurés

- Sync complet sans `KeyError` ni `tp_cols` ; `Push Sage: 0 erreur` sur cycles consécutifs.
- `F_TAXE` : `C18/C05/D18/D05` créés ; `F_DOCENTETE`/`F_DOCLIGNE` : pièces et lignes écrites avec codes taxe.
- Facture `FA000027` certifiée SFEC (signature `64A51F…`), QR scannables (PDF + page `/print` identiques).
- 5 bugs "qui plantent en démo" éliminés avant la présentation (contact, dropdown avoir, utilisateurs, print, QR).

## 7. Suites prévues

1. R11 export/import (dernier), XSS + gate admin, notifications, formulaire édition.
2. Étape 4 NIU : tolérance legacy à la lecture + cohérence fiche contact.
3. Régénération snapshots (dérive depuis les derniers correctifs) + enrichissement `tests/test_avoirs.py` (cas push contacts/tiers).
