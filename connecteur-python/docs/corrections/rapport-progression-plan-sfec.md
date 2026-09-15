# Rapport de progression — Alignement SFEC vs plan `docs/gap-invoice/a.md`

> **Date** : 2026-09-14
> **Base analysée** : `connecteur-python/`
> **Référence** : `docs/gap-invoice/a.md` (plan d'alignement, étapes 1 → 9)

---

## Vue d'ensemble

| Étape | Livrable | Progression | Statut |
|---|---|---|---|
| **1** | Schéma SQLite | ~90% | ⚠️ quasiment terminé |
| **2** | Noyau métier | ~50% | ⚠️ 2 trous bloquants |
| **3** | Payload SFEC | ~95% | ✅ essentiellement terminé |
| **4** | POS → facture | ~95% | ✅ essentiellement terminé |
| **5** | PDF mentions légales | ~95% | ✅ essentiellement terminé |
| **6** | Routes web | ~40% | 🔧 en cours (position actuelle) |
| **7** | Templates | ~20% | ⏳ à faire |
| **8** | Sync / Sage | ~30% | ⏳ à faire |
| **9** | Tests & snapshots | ~5% | ⏳ à faire |

---

## Détail par étape

### Étape 1 — Schéma SQLite (`app/storage/db.py`) — ~90%

**Fait :**
- Colonnes SFEC dans `invoices` (l.179-192)
- Colonnes `invoice_lines` (`subtotal`, `discount_type`, `type_article`, `classification_code`, l.217-220)
- Colonnes `contacts` (`rccm`, `is_taxable`, l.82-83)
- Index unique avoir `idx_invoices_avoir_unique` (l.304-306)
- Index unique NIU `idx_contacts_niu_unique` (l.307-308)
- `_migrate()` alimenté avec toutes les colonnes (l.330-395)
- `generate_avoir_number()` (l.490-505)

**Restant :**
- ❌ Seeds `avoir_prefix` / `avoir_format` / `avoir_next_number` **absents** de `_seed_default_settings()` (l.411-422)
- ⚠️ `generate_avoir_number()` lit `doc_format_avoir` / `doc_next_avoir` — **noms de clés désalignés** avec le plan (`avoir_format` / `avoir_next_number`)

### Étape 2 — Noyau métier (`app/domain/invoices.py`) — ~50%

**Fait :**
- `avoir_existe_pour()` créé (l.13-21)
- `_validate_avoir()` implémenté (l.24-50)
- `update_invoice()` étendu : `payment_method`, `devise`, `recipient_rccm`, `is_recipient_taxable`, `payment_date` (l.116-124)
- `recalc_invoice_totals()` calcule les totaux détaillés (`db.py:547-586`)

**Restant — 2 trous bloquants :**
- ❌ `create_invoice()` **n'insère pas** les nouvelles colonnes dans le INSERT (l.83-96) : `payment_method`, `devise`, `recipient_rccm`, `is_recipient_taxable`, `reference_invoice_id`, `montant_ht_brut` sont lues (l.72-77) mais jamais écrites
- ❌ `_validate_avoir()` **n'est pas appelé** (l.79-80 : simple `print("c'est un autre avoir")`)
- ❌ `_save_lines()` n'insère pas `subtotal`, `discount_type`, `type_article`, `classification_code` (l.276-310)
- ❌ `count_invoices()` sans compteur d'avoirs

### Étape 3 — Payload SFEC (`app/integration/sfec/endpoints.py`) — ~95% ✅

**Fait :**
- Mapping `type_doc` → SFEC (`_TYPE_DOC_TO_SFEC`, `_map_invoice_type`)
- `is_recipient_taxable` lu de l'invoice (plus hardcodé)
- `discount_amount`, `additional_cent_tax` lus de l'invoice (plus hardcodés)
- `currency` via `devise`, `payment_method` via l'invoice
- Items : `discount_amount`/`discount_type`, `designation` propagée, `item_type` via `type_article`
- `reference_invoice_id` ajouté si `creditNote`
- `recipient_rccm`, `payment_date` propagés si renseignés
- `sqlite_invoice_to_sfec()` propage tous les champs + structure d'items
- `validate_sfec_payload()` : `creditNote` → `reference_invoice_id` obligatoire ; cohérence totaux avec tolérance configurable (corrige BUG-001)

### Étape 4 — POS → facture (`app/domain/pos.py`) — ~95% ✅

**Fait :**
- `_create_invoice_for_ticket()` (l.92-310) : `payment_method` mappé depuis `mode_paiement`, `devise`, `montant_ht_brut`, totaux `total_*`, remises propagées dans les `invoice_lines`, `subtotal`/`discount_type`/`type_article`/`classification_code` insérés, `additional_cent_tax`
- `create_contact()` (l.617-667) : contrôle d'unicité NIU (refus + `IntegrityError` fallback), accepte `rccm`, `is_taxable` (corrige BUG-005)

### Étape 5 — PDF mentions légales (`app/domain/pdf.py`) — ~95% ✅

**Fait :**
- En-tête vendeur complet : nom, forme juridique (`legal_form`), NIU, **RCCM**, **régime fiscal**, **capital social**, adresse, tel/email, **RIB/IBAN** (l.158-185)
- **Nature du document** : `FACTURE D'AVOIR` / `FACTURE DE VENTE` selon `type_doc` (l.191-193)
- Bandeau avoir + n° facture d'origine (`reference_invoice_id`) (l.195-199)
- Date + heure (`created_at`), mode de paiement, devise (l.201-208)
- Lignes articles : Qte, PU HT, **Brut**, **Remise**, **Après remise**, TVA %, TVA, TTC (l.239-256)
- Totaux détaillés : HT brut, remises lignes, remise globale, HT net, **TVA 18%**, **TVA 5%**, **exonéré**, **centime additionnel**, total TVA, TTC, **net à payer** (l.277-289)
- **Montant en lettres** (`_montant_en_lettres`, l.77-110) (« Arrêtée à la somme de… », l.302-304)
- Certification SFEC : mention complète, numéro, date, **signature électronique**, **QR code image** (`_generate_qr_image`, l.113-128, 327-338)

**À vérifier :**
- `qrcode` ajouté à `requirements.txt` ?

### Étape 6 — Routes web — ~40% 🔧 (position actuelle)

**Fait :**
- `app/web/routes/directory.py` : POST `/api/contacts` (doublon NIU → 409), PUT accepte `rccm`/`is_taxable` + contrôle doublon NIU (l.118-145)
- `app/web/routes/pos.py` : `create_contact` (doublons + `rccm`/`is_taxable`), ticket → facture (nouveaux champs) ; fiche client POS expose `niu`/`rccm`/`is_taxable` (l.106-113)
- `app/web/routes/config.py` : `company.*` exposés (`legal_form`, `rc_number`, `capital`, `tax_regime`, `bank_iban`, `bank_account`…), tolérance validation configurable

**Restant :**
- ❌ `app/web/routes/billing.py` : formulaire **sans** sélecteur `type_doc` / `payment_method` / `devise` / `reference_invoice_id` (pas de création d'avoir côté UI)
- ❌ `app/web/routes/dashboard.py` `print_certified` : n'affiche pas `invoice_type` (bandeau FACTURE/AVOIR), type destinataire, remises, exonéré, timbre, centime, `reference_invoice_id`
- ❌ `app/web/routes/sync_api.py` : pas de découpage certification des avoirs

### Étape 7 — Templates — ~20%

**Restant :**
- ❌ `billing/form.html` : pas de `type_doc`, `payment_method`, `devise`, `reference_invoice_id`, remises lignes, `type_article`
- ❌ `billing/detail.html` : pas d'affichage avoir / mode paiement / devise
- ❌ `billing/list.html` : pas de colonne/filtre type document (vente / avoir)
- ❌ `dashboard/invoices.html` : pas de filtre `type_doc`
- ❌ `dashboard/print.html` : pas de mentions légales complètes + gabarit AVOIR
- ⚠️ `dashboard/certified.html` / `pending.html` : pas de badge avoir
- ❌ `directory/clients.html` : champ NIU seul — pas de **RCCM**, **assujetti TVA**, ni avertissement doublon NIU
- ⚠️ `config/index.html` : section société présente (`legal_form`, `rc_number`, `capital`, `tax_regime`, `bank_*`) — vérifier libellés
- ❌ `app/web/static/js/app.js` : pas de gestion du type avoir (referencing facture certifiée), du mode de paiement, de la devise

### Étape 8 — Sync / Sage — ~30%

**Fait :**
- ⚠️ `app/integration/sage/database.py` expose déjà `d.DO_Type AS type_doc` (l.905, 979)

**Restant :**
- ❌ `app/sync/engine.py` : `certify_invoice` / `_certify_single_worker` ne stockent pas `type_doc` depuis le cache Sage (la clause `certify_status` = A COMPTABILISER ne distingue plus vente/avoir)
- ❌ `database.py` : `payment_method`, `date_echeance`, devise non exposés
- ❌ `app/integration/sage/writer.py` : `write_invoice_to_sage` n'écrit pas d'**avoir négatif** si `type_doc='avoir'`

### Étape 9 — Tests & snapshots — ~5%

**Restant :**
- ❌ Snapshots non régénérés (`014_GET_api_invoices.json`, `042_GET_invoices.json`, `036_GET_billing_invoice_new.json`, `023_GET_api_products.json`, `011_GET_api_contacts.json`)
- ❌ Cas de test manquants : avoir sans référence → 400 ; avoir sur facture non certifiée → blocage ; avoir sur facture déjà avoirée → blocage ; doublon NIU contact → 409 ; cohérence totaux payload

---

## Verdict

Les étapes **3, 4 et 5 sont quasi terminées**. Position actuelle : **étape 6 (Routes web)**.

⚠️ **2 blocages en arrière-plan (étape 2)** à lever avant de terminer l'étape 6/7 — sans eux, créer un avoir depuis le formulaire web stockera des valeurs vides et ignorera les règles d'intégrité :

1. `create_invoice()` : ajouter `payment_method, devise, recipient_rccm, is_recipient_taxable, reference_invoice_id, montant_ht_brut` au INSERT (`invoices.py:83-96`)
2. Remplacer `print("c'est un autre avoir")` par `_validate_avoir(reference_invoice_id)` (`invoices.py:79-80`)

Rattrapage rapide : seeds avoir + alignement des noms de clés (`db.py`).

## Actions recommandées (ordre)

1. **Étape 1 (rattrapage)** : seeds `avoir_*` + corriger `generate_avoir_number()`
2. **Étape 2 (bloquants)** : INSERT étendu + appel `_validate_avoir` dans `create_invoice` ; `_save_lines` enrichi
3. **Étape 6** : formulaire facture (type_doc + paiement + devise + référence avoir) ; `print_certified` enrichi
4. **Étape 7** : templates + JS
5. **Étape 8** : sync Sage (type_doc, avoirs négatifs)
6. **Étape 9** : tests & snapshots