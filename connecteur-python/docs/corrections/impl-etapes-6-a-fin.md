# Plan d'implémentation — Étapes 6 à 9 (routes web → fin de projet)

> **Date** : 2026-09-14
> **Base analysée** : `connecteur-python/` (working tree courant)
> **Références** : `docs/gap-invoice/a.md` (plan à 9 étapes) · `docs/corrections/rapport-progression-plan-sfec.md` (état des lieux)
> **Portée** : détail opérationnel de ce qui reste à implémenter de l'étape 6 (routes web) jusqu'à la fin du projet, avec ancres de fichiers/lignes vérifiées sur le code actuel.

---

## 0. État de l'art au démarrage (vérifié sur disque)

- `app/domain/invoices.py` — `create_invoice()` insère désormais `payment_method, devise, recipient_rccm, is_recipient_taxable, reference_invoice_id, montant_ht_brut` (l.83-101) ; `_validate_avoir()` appelé pour `type_doc='avoir'` (l.79-80) ; `_save_lines()` insère `subtotal, discount_type, type_article, classification_code` (l.307-325). ✅
- `app/storage/db.py` — seeds `avoir_prefix` / `avoir_format` / `avoir_next_number` ajoutés à `_seed_default_settings()`. ✅
- `app/web/routes/billing.py` — logique serveur du formulaire prête : `_invoice_form_page()` expose `type_doc`, `payment_method`, `devise`, `reference_invoice_id`, `recipient_rccm` et la liste des ventes certifiées éligibles (`certified_sales_json`, l.182-205). ❌ Le template ne les exploite pas.
- `app/web/static/css/app.css` — correctif sidebar (cf. `sidebar-issue.md`) **déjà appliqué** : pas de `.sidebar.collapsed:hover`, pas de `display:none` sur le toggle, `.sidebar-top` en `space-between` (l.13-21). ✅
- BUG en attente : `dashboard.py:print_certified` référence `doc_label` et `inv_type` **jamais définis** (NameError à l'affichage).

### Ordre d'exécution recommandé

1. **Étape 6** : corriger `print_certified` (NameError) + filtre type sur `/api/invoices/list` + passer `meta_rows` au détail.
2. **Étape 7** : templates + JS du formulaire (le gros morceau UX).
3. **Étape 8** : sync Sage (type_doc, avoirs négatifs).
4. **Étape 9** : tests & snapshots.
5. **Fin de projet** : rattrapage `count_invoices` + `generate_avoir_number` + recette + sécurité.

---

## 1. Étape 6 — Routes web

### 1.1 `app/web/routes/dashboard.py` — corriger `print_certified` (l.139-203) — ⚠️ BLOQUANT

Le `render_template` (l.171-203) passe `doc_label` et `invoice_type` mais ces variables ne sont **jamais définies** → `NameError`.

- Définir avant le `render_template` :
  - `inv_type = inv.get("invoice_type", "") or inv.get("invoice_status", "")`
  - `doc_label = "FACTURE D'AVOIR" if inv_type == "creditNote" else "FACTURE"` (déterministe, pas `"FACTURE DE VENTE"` pour ne pas casser le gabarit générique)
- Lire la liste d'items via `inv.get("items") or inv.get("items_json") or []` (l.160) pour fiabiliser.
- TVA 18 % / 5 % : préférer `total_tax_t_amount` / `total_tax_r_amount` quand présents, sinon fallback sur `total_tax18` / `total_tax5` (l.194-195).
- Vérifier que `recipient_type`, `discount_amount`, `total_exempt_amount`, `additional_cent_tax`, `electronic_stamp_duty`, `reference_invoice_id` sont bien populés depuis la réponse SFEC (l.174-179) avant de les faire afficher dans le template (étape 7).

### 1.2 `app/web/routes/billing.py` — filtre `type` sur `/api/invoices/list` (l.80-98)

- Passer `type_doc=request.args.get("type") or None` à `invoice_engine.list_invoices(...)` (miroir du GET `/api/invoices` l.227-240 qui le fait déjà).
- Résultat attendu : la liste factures du dashboard peut filtrer vente / avoir côté API.

### 1.3 `app/web/routes/billing.py` — fiche détail : `meta_rows` jamais rendu (l.107-148)

- `meta_rows` (badge Type, mode paiement, devise, facture d'origine — l.124-129) est construit mais **absent du `render_template`** (l.133-148).
- Ajouter `meta_rows=meta_rows` au `render_template` et afficher dans `billing/detail.html` (étape 7.2).

### 1.4 `app/web/routes/billing.py` — création / mise à jour API (l.243-285)

- Déjà opérationnel grâce au moteur métier : `create_invoice`/`update_invoice` lisent les nouvelles clés et `_validate_avoir()` s'applique côté serveur.
- Vérification : un POST `type_doc='avoir'` sans `reference_invoice_id` → `InvoiceIntegrityError` → code **422** (l.255-256). Un doublon d'avoir → 422. À couvrir par les tests (étape 9).

### 1.5 `app/web/routes/dashboard.py` — `pending_page` (l.91-127)

- Ajouter un badge avoir dans les lignes (`type_doc`/`invoice_type` du cache Sage) et s'assurer que `certifySingle()` (app.js) est utilisable pour les avoirs (le payload `certify_invoice` gère déjà le mapping `creditNote`, étape 3). Non bloquant.

### 1.6 `app/web/routes/sync_api.py` — `api_certified_list` (l.71-91)

- Exposer le type document dans l'output (ex. `"type": "creditNote" | "salesInvoice"`) pour permettre le badge avoir dans `dashboard/certified.html`.

### 1.7 Routes déjà conformes (à ne pas re-toucher)

- `app/web/routes/pos.py` — `create_contact` (doublons NIU + `rccm`/`is_taxable`), ticket → facture avec nouveaux champs. ✅
- `app/web/routes/directory.py` — POST `/api/contacts` doublon NIU → 409 ; PUT accepte `rccm`/`is_taxable` + contrôle doublon. ✅
- `app/web/routes/config.py` — `company.*` exposés (`legal_form`, `rc_number`, `capital`, `tax_regime`, `bank_iban`, `bank_account`) + tolérance validation. ✅

---

## 2. Étape 7 — Templates & JS

### 2.1 `app/web/templates/billing/form.html` — formulaire facture/avoir

L'essentiel des données est déjà injecté côté Python (billing.py:202-224) ; le template doit maintenant les exploiter.

**Bloc en-tête (après l.19-27)** :
- [ ] Sélecteur **type document** : `<select name="type_doc" onchange="onTypeDocChange()">` avec `d_vente` / `d_avoir` (déjà passés, l.206-207).
- [ ] Champ **mode de paiement** : `<select name="payment_method">` (cash, bank_transfer, mobile_money, card, cheque), valeur par défaut `bank_transfer`.
- [ ] Champ **devise** : `<select name="devise">` (XAF, FCFA, EUR, USD), défaut `XAF`.
- [ ] Champ **référence facture d'origine** (`reference_invoice_id`) : liste déroulante alimentée par `certified_sales_json` (l.205) — ventes certifiées **non déjà avoirées**. Visible **uniquement** quand `type_doc === 'avoir'` (géré par `onTypeDocChange()`).
- [ ] Champ **RCCM destinataire** (`recipient_rccm`) lié au contact sélectionné.

**Lignes de facture (`addLine()`, l.120)** :
- [ ] Ajouter colonne **remise %** + **type d'article** (`type_article`: product/service) + `discount_type` (fixed/percentage) par ligne.
- [ ] Ajouter hidden fields `subtotal`, `additional_cent_tax` ou les calculer dans `calcLine()` (l.127).

**Totaux sidebar (l.80-93)** :
- [ ] Afficher HT brut, remises de lignes, remise globale, **TVA 18 %**, **TVA 5 %**, exonéré, centime additionnel, **net à payer** — alignés sur `recalc_invoice_totals()` (db.py).

**JS inline (l.97-142)** :
- [ ] `onTypeDocChange()` : show/hide du champ référence avoir + force `statut` cohérent.
- [ ] `gatherData()` (l.132) : ajouter `type_doc`, `payment_method`, `devise`, `reference_invoice_id`, `recipient_rccm`, et les enrichir par ligne (`type_article`, `discount_type`, `remise_pct`, `subtotal`).
- [ ] Validation avant envoi (l.134/136) : si `type_doc === 'avoir'` → `reference_invoice_id` requis, sinon toast d'erreur.
- [ ] `fillContact()` (l.114) : remplir `recipient_rccm` et pré-renseigner `is_recipient_taxable` depuis le contact.

### 2.2 `app/web/templates/billing/detail.html`

- [ ] Afficher le bloc `meta_rows` (type, mode paiement, devise, facture d'origine) — variable à passer côté route (1.3).
- [ ] Afficher les totaux détaillés (HT brut, remises, TVA 18/5, exonéré, centime, timbre, net à payer) en plus des totaux simples actuels (l.23-27).

### 2.3 `app/web/templates/billing/list.html`

- [ ] Filtre **type document** dans `filters-form` (l.17-26) ; branché dans `buildQ()` (l.49-57) + route `/api/invoices/list` (1.2).
- [ ] Colonne ou badge **Type** (Vente / Avoir) dans `invRow()` (l.69-80) ; afficher `reference_invoice_id` au survol d'un avoir.

### 2.4 `app/web/templates/dashboard/invoices.html`

- [ ] Bouton/filtre **type doc** (Vente / Achat / Avoir) à côté de l'existant (l.3-7) et colonne badge avoir dans le tableau (l.8-9).

### 2.5 `app/web/templates/dashboard/print.html` — impressions certifiées

- [ ] Remplacer le titre fixe `FACTURE` (l.26) par `{{ doc_label }}` (FACTURE / FACTURE D'AVOIR).
- [ ] Bandeau avoir + n° de la facture d'origine : afficher `reference_invoice_id` (variable déjà passée par `print_certified`) quand c'est un avoir.
- [ ] Totaux détaillés : ligne **exonéré** (`total_exempt`), **centime additionnel** (`additional_cent_tax`), **timbre** (`electronic_stamp_duty`), remise globale (`discount_amount`), **Net à payer** (`amount_due`) — entre l'existant (l.51-55).
- [ ] Mentions légales vendeur complètes : RCCM (`seller_rccm`, déjà passé) + forme juridique / régime fiscal / capital si disponibles dans `company.*`.

### 2.6 `app/web/templates/dashboard/certified.html` / `pending.html`

- [ ] Badge avoir (`creditNote`) sur les lignes certifiées / en attente.

### 2.7 `app/web/templates/directory/clients.html`

- [ ] Champs **RCCM** et **assujetti TVA** (checkbox `is_taxable`) dans le formulaire client.
- [ ] Avertissement visuel **doublon NIU** (le serveur renvoie déjà 409 avec la valeur existante).

### 2.8 `app/web/templates/config/index.html`

- [ ] Vérifier libellés section société (`legal_form`, `rc_number`, `capital`, `tax_regime`, `bank_iban`, `bank_account`) — largement présents.

### 2.9 `app/web/static/js/app.js` + CSS

- [ ] Helpers globaux : ajouter la gestion du type avoir (referencing d'une facture certifiée), mode de paiement, devise (fonctions appelées depuis `form.html`).
- [ ] **CSS sidebar** (`app.css`) : correctif de `sidebar-issue.md` **déjà en place** (l.13-21) → à confirmer par un smoke test visuel de non-régression (clic toggle, état mémorisé `localStorage`).

---

## 3. Étape 8 — Sync / Sage

### 3.1 `app/integration/sage/database.py`

- [ ] **Mapping `type_doc` Sage → domaine** : `d.DO_Type AS type_doc` (l.905, l.979) renvoie un entier Sage (6 = vente). Distinguer vente / avoir (type document avoir en Sage, souvent montants négatifs ou `DO_Type` dédié) et l'exposer en clair (`vente`/`avoir`) pour le payload SFEC (étape 3 en dépend pour décider `creditNote`).
- [ ] Exposer `date_echeance` (déjà présent l.896/970) et ajouter `payment_method`, `devise` si les colonnes existent dans la table.
- [ ] `write_sfec_to_invoice` : écrire les totaux détaillés si les colonnes SFEC correspondantes existent.

### 3.2 `app/sync/engine.py`

- [ ] `auto_certify_invoices()` (l.509+) : le filtre `statut_code != 2` (l.542) ne distingue plus vente/avoir — stocker `type_doc` dans le cache Sage et **appliquer les 3 règles d'intégrité avant auto-certification d'un avoir** (référence non vide, facture d'origine certifiée, pas de double avoir) sinon mettre en `ERREUR` avec message dédié.
- [ ] `_certify_single_worker()` (l.451) : s'assurer que `certify_invoice()` reçoit bien `type_doc` (le mapping `creditNote` + `reference_invoice_id` est déjà géré côté payload, cf. étape 3).
- [ ] `_build_sfec_lookup()` / cache : inclure les avoirs dans le lookup pour éviter les auto-certifications à répétition.

### 3.3 `app/integration/sage/writer.py` — écriture des avoirs

- [ ] `write_invoice_to_sage()` (l.24-96) : si `type_doc='avoir'` → écrire en **négatif** (DO_TotalHT / DO_Taxe1 / DO_TotalTTC / DO_NetAPayer négatifs, quantités lignes négatives) et avec le **type document avoir** de la config, pas systématiquement `vente_type=6` (l.46-58). Conserver le comportement positif actuel pour les ventes.

---

## 4. Étape 9 — Tests & snapshots

### 4.1 Snapshots à régénérer

- [ ] `014_GET_api_invoices.json`
- [ ] `042_GET_invoices.json`
- [ ] `036_GET_billing_invoice_new.json`
- [ ] `023_GET_api_products.json`
- [ ] `011_GET_api_contacts.json`
- Commande : `python tests/compare_snapshots.py` (voir `tests/conftest.py` / `snapshot_routes.py`). Vérifier ensuite avec `pytest tests/` (attendu : 94 routes, 51 snapshots, 2 routes 500 whitelistées `/api/contacts` et `/api/sfec/certified-from-api`).

### 4.2 Nouveaux cas de test à ajouter

| Cas | POST/GET | Résultat attendu |
|---|---|---|
| Avoir sans `reference_invoice_id` | POST `/api/invoices` `type_doc='avoir'` | **422** + `InvoiceIntegrityError` |
| Avoir sur facture **non certifiée** | POST `/api/invoices` | **422** ("doit etre certifiee") |
| Avoir sur facture **déjà avoirée** | POST `/api/invoices` | **422** ("Un avoir existe deja") |
| Doublon NIU contact | POST `/api/contacts` | **409** |
| Cohérence totaux payload | `validate_sfec_payload` | tolérance `config.validation.tolerance_amount` |

### 4.3 Non-régression

- [ ] `pytest tests/` vert après chaque étape (filet de sécurité A0 : 5 tests smoke + 51 snapshots).
- [ ] `python -m compileall app main.py` pour détecter toute erreur de syntaxe.

---

## 5. Fin de projet — rattrapage & recette

### 5.1 Rattrapage noyau / schéma (dettes identifiées)

- [ ] `app/storage/db.py` — **désalignement des clés avoirs** : `generate_avoir_number()` (l.490-505) lit `doc_format_avoir` / `doc_next_avoir` alors que les seeds définissent `avoir_format` / `avoir_next_number`. Aligner les clés (et le `_migrate()` de `settings` si nécessaire).
- [ ] `app/domain/invoices.py:count_invoices()` (l.259-278) — ajouter un compteur d'avoirs (`type_doc='avoir'`) pour la refonte du CA net (CA ventes − avoirs).

### 5.2 Questions à trancher

- [ ] `app/domain/pos.py:194` — « 5% TVA 18% seulement ? » : clarifier le périmètre du **centime additionnel** (5 % de la TVA) pour les tickets et factures.
- [ ] **Sécurité** : clé API SFEC réelle présente dans `config.json` → retirer du fichier au profit d'une variable d'environnement / placeholder (comme `config.example.json`), la vraie clé n'étant chargée que via l'env.

### 5.3 Recette finale

- [ ] Créer une vente → la certifier → créer un avoir associé : doit passer les 3 règles d'intégrité.
- [ ] Vérifier le PDF de la vente et de l'avoir (mentions légales + bandeau AVOIR + QR + montant en lettres).
- [ ] Vérifier la liste factures filtrée par type document.
- [ ] Vérifier le repli de la sidebar au clic (pas de ré-expansion au survol) + état mémorisé.
- [ ] Tester hors-ligne : avoirs → file d'attente retry, pas de blocage.
- [ ] Commit final avec message de style repo (français, ex. `feature: ...`).

---

## 6. Récapitulatif (checklist rapide)

- [ ] **6.1** Corriger `print_certified` (NameError `doc_label`/`inv_type`)
- [ ] **6.2** Filtre `type` sur `/api/invoices/list`
- [ ] **6.3** Passer `meta_rows` au `billing/detail.html`
- [ ] **7.1** Formulaire `billing/form.html` : type_doc + paiement + devise + référence avoir + lignes enrichies + JS
- [ ] **7.2** Détail : meta_rows + totaux détaillés
- [ ] **7.3** Liste : filtre + badge type
- [ ] **7.4-7.8** invoices.html, print.html, certified/pending, clients.html, config
- [ ] **8.1** Sage database.py : mapping type_doc + nouveaux champs
- [ ] **8.2** engine.py : 3 règles d'intégrité en auto-certif
- [ ] **8.3** writer.py : avoirs négatifs
- [ ] **9.1** Régénérer 5 snapshots
- [ ] **9.2** Ajouter les cas avoirs / doublon / totaux
- [ ] **5.1** Aligner `generate_avoir_number` + `count_invoices` avoirs
- [ ] **5.2** Clé API SFEC hors `config.json`