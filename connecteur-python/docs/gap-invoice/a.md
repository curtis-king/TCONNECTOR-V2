# PLAN D'ALIGNEMENT SFEC — Structure de la base SQLite et flux de facturation

> **Portée** : Mise en conformité de T-CONNECTOR avec l'API SFEC (Système de Facturation
> Électronique Certifiée, République du Congo) au regard du cahier de recette
> `SFEC_Cahier_de_Tests_API_REMPLI.xlsx` (39 cas de test, bugs BUG-001 → BUG-005).
> **Base analysée** : `connecteur-python/data/tconnector.db`
> **Base de référence schéma** : `connecteur-python/app/storage/db.py`

---

## 1. Résumé des écarts constatés

Le cahier de tests a relevé 5 anomalies (4 bloquantes, 1 majeure) qui sont soit des
manques de la base SQLite, soit des manques de l'API interne :

| Bug | Cas | Criticité | Cause racine DB/API |
|---|---|---|---|
| BUG-001 | TC-035 | Bloquant | Trop de champs SFEC sont **hardcodés** dans `endpoints.py` (`discount_amount=0`, `additional_cent_tax=0`, `is_recipient_taxable=True`…) → totaux incohérents impossibles à valider côté ERP |
| BUG-002 | TC-033 | Bloquant | **Aucun champ `reference_invoice_id`** → impossible de lier un avoir à sa facture de vente |
| BUG-003 | TC-034 | Bloquant | **Aucune contrainte d'unicité** facture ⇄ avoir → double avoir possible |
| BUG-004 | TC-031 | Bloquant | Le **PDF** (`app/domain/pdf.py`) ne génère pas les mentions obligatoires (QR, signature, centime additionnel, net à payer, montant en lettres, RCCM, régime fiscal…) car les données ne sont pas stockées |
| BUG-005 | TC-018/020/021 | Majeur | **`contacts` sans contrainte d'unicité** sur `niu`/`nom`/`rccm` → doublons clients acceptés |

---

## 2. Cible : structure SQLite alignée SFEC

### 2.1 Table `invoices` — colonnes à AJOUTER (migration)

Schéma actuel : `id, numero, date_facture, date_echeance, reference, contact_id,
tiers_code, tiers_nom, tiers_niu, tiers_email, tiers_telephone, tiers_adresse,
tiers_type, montant_ht, montant_tva, montant_ttc, montant_restant, statut, valide,
type_doc, source, sfec_statut, sfec_num_certif, sfec_signature, sfec_qr_code,
sfec_date_certif, sfec_id, synced_sage, sage_domaine, sage_type, sage_piece, notes,
vendeur_id, created_at, updated_at`

**Colonnes à ajouter** (dans `app/storage/db.py` `SCHEMA` + liste `_migrate()`) :

| Colonne | Type SQL | Défaut | Rôle SFEC |
|---|---|---|---|
| `payment_method` | TEXT | `'bank_transfer'` | `payment_method` de la payload (aujourd'hui hardcodé) |
| `devise` | TEXT | `'XAF'` | `currency` de la payload (aujourd'hui config-only) |
| `montant_ht_brut` | REAL | `0.0` | `subtotal` = Σ unit_price×qty AVANT remises |
| `total_tax_t_amount` | REAL | `0.0` | TVA 18% séparée (`total_tax_t_amount`) |
| `total_tax_r_amount` | REAL | `0.0` | TVA 5% séparée (`total_tax_r_amount`) |
| `total_exempt_amount` | REAL | `0.0` | Montant exonéré TVA 0% (`total_exempt_amount`) |
| `discount_amount` | REAL | `0.0` | Escompte/remise globale (`discount_amount`) |
| `total_line_discount_amount` | REAL | `0.0` | Total remises articles (`total_line_discount_amount`) |
| `additional_cent_tax` | REAL | `0.0` | Centime additionnel (5% de la TVA) |
| `electronic_stamp_duty` | REAL | `0.0` | Timbre électronique — DOIT rester `0` |
| `is_recipient_taxable` | INTEGER | `1` | `is_recipient_taxable` (assujetti TVA) |
| `recipient_rccm` | TEXT | `''` | `recipient_rccm` (requis B2B / gouvernement) |
| `reference_invoice_id` | TEXT | `''` | `reference_invoice_id` — **obligatoire pour les avoirs** |
| `payment_date` | TEXT | `''` | `payment_date` |

### 2.2 Table `invoice_lines` — colonnes à AJOUTER

| Colonne | Type SQL | Défaut | Rôle SFEC |
|---|---|---|---|
| `subtotal` | REAL | `0.0` | Montant brut ligne (unit_price×qty) avant remise |
| `discount_type` | TEXT | `'fixed'` | `fixed` / `percentage` (`discount_type` item) |
| `type_article` | TEXT | `'product'` | `product` / `service` (`type` item) — injecté depuis `products.nature` |
| `classification_code` | TEXT | `''` | `classification_code` item (famille) |

### 2.3 Table `contacts` — colonnes + contrainte à AJOUTER

| Colonne | Type SQL | Défaut | Rôle |
|---|---|---|---|
| `rccm` | TEXT | `''` | `recipient_rccm` (source fiable côté annuaire) |
| `is_taxable` | INTEGER | `1` | Assujetti TVA du client |

**Contrainte d'unicité (BUG-005)** : index unique partiel sur `niu` (si renseigné) :

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_niu_unique
    ON contacts(niu) WHERE niu <> '';
```

> ⚠️ La création d'un contact avec un `niu` déjà existant doit être **refusée** avec un
> message explicitant le doublon (champ retourné : `niu`, valeur existante).
> Algorithme : vérifier le doublon en amont dans `create_contact()`
> (`app/domain/pos.py:545`) et gérer l'exception `IntegrityError` comme fallback.

### 2.4 Table `settings` — clés à AJOUTER (séquences avoir)

| Clé | Défaut | Rôle |
|---|---|---|
| `avoir_prefix` | `'AV'` | Préfixe des factures d'avoir |
| `avoir_format` | `'AV{:06d}'` | Format de numérotation des avoirs |
| `avoir_next_number` | `'1'` | Prochain n° d'avoir |

### 2.5 Mapping `type_doc` → `invoice_type` SFEC

`type_doc` existe déjà mais n'est jamais exploité par le payload :

| `invoices.type_doc` | `invoice_type` SFEC |
|---|---|
| `vente` | `salesInvoice` |
| `avoir` | `creditNote` (+ `reference_invoice_id` obligatoire) |

**Règles d'intégrité (TC-032/033/034) côté ERP avant envoi** :
1. Un avoir **ne peut pas** être certifié si `reference_invoice_id` est vide.
2. La facture de vente référencée doit **exister ET être certifiée** (`sfec_statut` IN
   `('CERTIFIE','DEJA_CERTIFIE')`) sinon erreur 422 côté ERP.
3. Une facture de vente certifiée **ne peut pas** avoir plus d'un avoir : vérifier
   l'existence d'une facture `type_doc='avoir'` avec `reference_invoice_id = numero` de
   la facture visée avant la création de l'avoir.

---

## 3. Procédure de mise en œuvre

### Étape 1 — Schéma SQLite (`app/storage/db.py`)

1. Ajouter les colonnes `invoice_*` dans la définition `CREATE TABLE IF NOT EXISTS invoices`
   du bloc `SCHEMA`.
2. Ajouter les colonnes `invoice_lines` / `contacts` de la même façon.
3. Créer l'index unique `idx_contacts_niu_unique`.
4. Alimenter la liste `_migrate()` avec toutes les nouvelles colonnes pour les bases
   existantes (mécanisme déjà en place, `db.py:304-324`) **sortis du `CREATE TABLE`**.
5. Ajouter les seeds `avoir_prefix`, `avoir_format`, `avoir_next_number` dans
   `_seed_default_settings()`.
6. Ajouter `generate_avoir_number()` (miroir de `generate_invoice_number()`, `db.py:402`).

> ⚠️ Règle : toute colonne ajoutée en `CREATE TABLE IF NOT EXISTS` doit **aussi** figurer
> dans `_migrate()` sinon les bases existantes ne seront pas mises à jour.

### Étape 2 — Noyau métier (`app/domain/invoices.py`)

| Fonction | À faire |
|---|---|
| `create_invoice` | Enregistrer `payment_method`, `devise`, `recipient_rccm`, `is_recipient_taxable`, `reference_invoice_id`, `montant_ht_brut` ; si `type_doc='avoir'` → appliquer les 3 règles d'intégrité §2.5 |
| `update_invoice` | Étendre la liste `updatable` aux nouvelles colonnes (`invoices.py:62-68`) |
| `_save_lines` | Enregistrer `subtotal`, `discount_type`, `type_article`, `classification_code` |
| `list_invoices` / `count_invoices` | (optionnel) exporter les nouvelles colonnes ; ajouter un compteur d'avoirs |
| `recalc_invoice_totals` (`db.py:461`) | Calculer et écrire `montant_ht_brut`, `total_tax_t_amount`, `total_tax_r_amount`, `total_exempt_amount`, `discount_amount`, `total_line_discount_amount`, `additional_cent_tax` |

**Fonctions à créer :**
- `avoir_existe_pour(facture_numero)` → bool (règle anti-double-avoir).

### Étape 3 — Payload SFEC (`app/integration/sfec/endpoints.py`)

Dans `db_invoice_to_sfec()` :

| Ligne actuelle | Remplacement |
|---|---|
| `232` `"invoice_type": "salesInvoice"` | mapper `type_doc` (`vente`→`salesInvoice`, `avoir`→`creditNote`) |
| `235` `"is_recipient_taxable": True` | lire `invoice.get("is_recipient_taxable", True)` |
| `241` `"discount_amount": 0` | `invoice.get("discount_amount", 0)` |
| `243` `"additional_cent_tax": 0` | `invoice.get("additional_cent_tax", 0)` |
| `247` `"currency": ...` | `invoice.get("devise", ...)` |
| `248` `"payment_method": "bank_transfer"` | `invoice.get("payment_method", "bank_transfer")` |
| `126` `discount_amount = 0` (items) | `discount_amount = line.get("discount_amount", line.get("remise_montant", 0))` et `discount_type = line.get("discount_type", "fixed")` |
| `137-141` (designation) | propager aussi `designation` (SQLite) |
| `146-154` (item_type) | préférer `line.get("type_article")` si présent |
| — | ajouter `sfec_req["reference_invoice_id"]` si `type` = `creditNote` |
| — | ajouter `sfec_req["recipient_rccm"]` si renseigné |
| — | ajouter `sfec_req["payment_date"]` si renseigné |

Dans `sqlite_invoice_to_sfec()` (`endpoints.py:440-473`) : propager
`payment_method`, `devise`, `type_doc`, `reference_invoice_id`, `recipient_rccm`,
`is_recipient_taxable`, `discount_amount`, `additional_cent_tax`, les totaux
taxe T/R/exonéré, et la nouvelle structure d'items (`subtotal`, `discount_type`,
`type_article`).

Renforcer `validate_sfec_payload()` (`endpoints.py:282`) :
- si `invoice_type == "creditNote"` → `reference_invoice_id` obligatoire ;
- vérifier la cohérence `total_amount = subtotal + tax + centime` (tolérance issue de
  `config.validation.tolerance_amount`) — corrige BUG-001.

### Étape 4 — POS → facture (`app/domain/pos.py`)

| Fonction | À faire |
|---|---|
| `_create_invoice_for_ticket` (l.80) | Insérer les nouvelles colonnes (`payment_method` = `mode_paiement` du ticket, `devise`, `montant_ht_brut`, `total_*`) ; propager `remise_pct`/`remise_montant` dans les `invoice_lines` (aujourd'hui elles sont calculées dans `calc_line_ticket_totals` mais écrasées : `net_ht` stocké, `remise` perdue) |
| `create_contact` (l.545) | Contrôle d'unicité §2.3 ; accepter `rccm`, `is_taxable` dans l'INSERT |

### Étape 5 — PDF mentions légales (`app/domain/pdf.py`) — BUG-004

Gabarit `generate_invoice_pdf` à enrichir conformément à la feuille
« Obligations Juridiques et fiscales » du cahier :

**Socle commun (toujours)**
1. En-tête vendeur : nom, forme juridique, **NIU**, **RCCM**, **régime fiscal**,
   **capital social**, adresse, téléphone, email, **RIB/IBAN** → champs
   `company.*` déjà présents dans `config.json` : `tax_number`, `address`, `phone`,
   `email`, `bank_account`, `bank_iban`, `tax_regime` ; **ajouter** `rc_number`/`capital`
   si absents de la config.
2. **Nature du document** : `FACTURE DE VENTE` / `FACTURE D'AVOIR` selon `type_doc`
   (le gabarit affiche toujours « FACTURE DE VENTE », cf. `pdf.py:80`).
3. Numéro, **date + heure**, mode de paiement, devise.
4. Lignes articles : quantité, PU HT, brut, remise, après-remise, TVA %, TVA, TTC.
5. Totaux : HT, **TVA 18%**, **TVA 5%**, **centime additionnel**, **Total TVA**,
   exonéré, remises (lignes séparées), **TTC**, **Net à payer**, **montant en lettres**.
6. Certification : mention « SFEC – Système de Facturation Électronique Certifié »,
   date, **signature électronique**, **QR code** (`sfec_qr_code`).
7. Pour les avoirs : afficher le bandeau « FACTURE D'AVOIR » + n° de la facture
   d'origine (`reference_invoice_id`).

**Fonctions utilitaires à ajouter :**
- `_montant_en_lettres(amount)` → conversion chiffres→lettres (français) ;
- génération QR : dépend de la lib (ajouter `qrcode` à `requirements.txt`).

### Étape 6 — Routes web

| Route / Fichier | Impact |
|---|---|
| `app/web/routes/billing.py` | API POST/PUT `/api/invoices*` : accepter `payment_method`, `devise`, `type_doc` (`avoir`), `reference_invoice_id`, `recipient_rccm`, lignes enrichies ; page formulaire : sélecteur type document + mode de paiement + devise + référence facture d'origine pour avoirs |
| `app/web/routes/pos.py` | `create_contact` : doublons + `rccm`/`is_taxable` ; ticket → facture : nouveaux champs |
| `app/web/routes/dashboard.py` | `print_certified` : afficher `invoice_type` (bandeau FACTURE/AVOIR), type destinataire, remises, exonéré, timbre, centime, `reference_invoice_id` |
| `app/web/routes/config.py` | (config) : exposés `company.*` (forme juridique, capital…) & validation cohérence montants |
| `app/web/routes/directory.py` | Formulaire client (annuaire) : champs `rccm`, `is_taxable` ; bloquer doublon NIU côté UI |
| `app/web/routes/sync_api.py` | si découpage prévu pour la certification des avoirs dossier par dossier |

### Étape 7 — Templates impactés

`app/web/templates/` :
- `billing/form.html` — sélecteur `type_doc`, `payment_method`, `devise`, champ
  `reference_invoice_id` (facture d'origine), lignes avec `type_article`,
  `discount_type`, remises.
- `billing/detail.html` — affichage des nouveaux champs (avoir, mode paiement, devise).
- `billing/list.html` — filtre / colonne type de document (vente / avoir).
- `dashboard/invoices.html` — filtre `type_doc`.
- `dashboard/print.html` — mentions légales complètes + gabarit AVOIR.
- `dashboard/certified.html` / `pending.html` — éventuel badge avoir.
- `directory/clients.html` — champs RCCM, assujetti TVA + avertissement doublon NIU.
- `directory/sales.html` — (champs vendeur, non bloquant).
- `pos/index.html` / `pos/ticket_print.html` — pas de changement requis (documenté).
- `config/index.html` — section société : forme juridique, capital social, régime
  fiscal, RIB/IBAN (déjà largement présents, vérifier libellés).

JS : `app/web/static/js/app.js` — ajouter la gestion du type avoir (referencing d'une
facture certifiée), du mode de paiement et de la devise dans le formulaire de
facturation.

### Étape 8 — Sync / Sage

| Fichier | Impact |
|---|---|
| `app/sync/engine.py` | `certify_invoice` / `_certify_single_worker` : passer les nouveaux champs du cache Sage (mapping depuis `F_DOCENTETE`) — la clause `certify_status` (statut 2 = A COMPTABILISER) ne suffit plus pour distinguer vente/avoir ; stocker `type_doc` (DO_Type) |
| `app/integration/sage/database.py` | Lire et exposer `payment_method`, `type_doc` (avoir = type document), `date_echeance`, devise depuis Sage ; `write_sfec_to_invoice` : écrire les totaux taxe T/R/exonéré si colonnes SFEC ajoutées |
| `app/integration/sage/writer.py` | `write_invoice_to_sage` : écrire un **avoir** dans Sage en quantité/montants négatifs si `type_doc='avoir'` |

### Étape 9 — Tests & snapshots

- `tests/snapshots/014_GET_api_invoices.json`, `042_GET_invoices.json`,
  `036_GET_billing_invoice_new.json`, `023_GET_api_products.json`,
  `011_GET_api_contacts.json` — régénérer (nouvelles colonnes dans les réponses).
- Ajouter des cas de test : création d'avoir sans référence → 400 ; avoir sur facture
  non certifiée → blocage ; avoir sur facture déjà avvoirée → blocage ;
  doublon NIU contact → 409 ; cohérence totaux payload validée avant envoi.
- Commande de régénération : `tests/compare_snapshots.py` (voir `tests/conftest.py`).

---

## 4. Ordre de priorité (bloquant d'abord)

| # | Livrable | Résout |
|---|---|---|
| 1 | Migration SQLite : colonnes + index unique contacts + seeds avoirs | base de tout le reste |
| 2 | Mapping `type_doc` → `invoice_type` + `reference_invoice_id` + 3 règles d'intégrité avoir | BUG-002, BUG-003 |
| 3 | Dé-hardcoder la payload (`is_recipient_taxable`, `discount_amount`, `additional_cent_tax`, `payment_method`, `currency`) + calcul/stockage des totaux détaillés | BUG-001 |
| 4 | Contrainte d'unicité NIU sur `create_contact` | BUG-005 |
| 5 | PDF mentions légales + gabarit AVOIR | BUG-004 |
| 6 | Routes / templates / JS (formulaire facture + annuaire) | UX |
| 7 | Sync Sage (lecture type_doc, écriture avoirs négatifs) | cohérence Sage |
| 8 | Tests & snapshots | non-régression |

---

## 5. Règles SFEC à ne jamais violer (rappel)

- `electronic_stamp_duty` DOIT valoir `0`.
- Montants **sans devise** dans les valeurs numériques de la payload.
- `total_amount = subtotal + total_tax_t + total_tax_r + additional_cent_tax`
- Un avoir DOIT référencer une facture de vente **certifiée** et est unique par facture.
- Pour `recipient_type = business|government` : `recipient_name` + `recipient_niu` requis.
- `foreign` : email, téléphone, adresse requis.
- Item `type` requis (`product` / `service`).

La base SQLite cible est résolue par app.core.paths.data_dir() → connecteur-python/data/tconnector.db (≥ connecteur-python, paths.py:19-35). Donc n'importe quel script qui importe app.storage.db tombe toujours sur la bonne base, même lancé depuis ailleurs.
Où le mettre
Dans scripts/backfill_centime.py (même pattern que scripts/generate_articles.py), puis le lancer côté Windows. Contenu à coller :
"""Backfill : integre le centime additionnel dans le TTC/restant des
brouillons et factures non certifiees (re-run recalc_invoice_totals).

Usage (depuis connecteur-python, cote Windows):
    python scripts/backfill_centime.py [--dry-run]
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import argparse

from app.storage.db import get_cursor, recalc_invoice_totals, close_connection


def main():
    parser = argparse.ArgumentParser(description="Backfill centime additionnel")
    parser.add_argument("--dry-run", action="store_true",
                        help="affiche les factures concernees sans modifier")
    args = parser.parse_args()

    with get_cursor() as cur:
        cur.execute("""
            SELECT id, numero, statut, sfec_statut
            FROM invoices
            WHERE statut = 'brouillon'
               OR sfec_statut IS NULL
               OR sfec_statut NOT IN ('CERTIFIE', 'DEJA_CERTIFIE')
        """)
        rows = cur.fetchall()

    if not rows:
        print("Aucune facture a traiter.")
        return

    print("{} facture(s) a recalculer :".format(len(rows)))
    if args.dry_run:
        for r in rows:
            print("  - {} (id={}, statut={}, sfec={})".format(
                r["numero"], r["id"], r["statut"], r["sfec_statut"]))
        return

    for r in rows:
        recalc_invoice_totals(r["id"])

    close_connection()
    print("{} facture(s) recalculee(s).".format(len(rows)))


if __name__ == "__main__":
    main()
Comment le lancer (Windows)
cd C:\Users\tp_m2\Documents\projects\TCONNECTOR-V2\connecteur-python
python scripts/backfill_centime.py --dry-run     # verifier la liste d'abord
python scripts/backfill_centime.py               # execution
Points de sécurité :
- Le script importe le code corrigé de l'Étape 1 (db.py) → il n'a besoin que des étapes 1-3 appliquées, pas de redémarrage du serveur pour ça (processus séparé).
- Idempotent : relançable sans risque (recalc recompute toujours la même chose).
- Copie de sûreté avant : copie de data\tconnector.db + -wal + -shm (la base est en WAL — arrêter le serveur avant la copie propre, ou copier avec le serveur arrêté).
- Préférable : serveur arrêté pendant le backfill (busy_timeout=5s sinon, WAL le tolère).
Vérification après coup
python -c "import os,sys; sys.path.insert(0,'.'); from app.storage.db import get_cursor;
for r in get_cursor().__enter__().execute('SELECT numero,statut,sfec_statut,montant_ht,montant_tva,montant_ttc,additional_cent_tax,montant_restant FROM invoices ORDER BY id DESC LIMIT 10').fetchall(): print(dict(r))"
Normalement : montant_ttc = montant_ht + montant_tva + additional_cent_tax et montant_restant = montant_ttc pour les factures recalculées ; montant_ttc inchangé (hors centime) pour les certifiées/exportées.