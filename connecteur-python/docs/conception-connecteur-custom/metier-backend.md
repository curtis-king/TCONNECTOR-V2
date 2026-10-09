# Métier backend — Connecteur custom (sans Sage BIJOU, sans la vue)

> Source : inventaire exhaustif du connecteur actuel (`app/domain`, `app/sync`, `app/integration`,
> `app/storage`, `app/config`) — relevé aux lignes près, octobre 2026.
> Périmètre validé : **SFEC gardée, caisse gardée, sync via adaptateur abstrait, tous référentiels gardés**.
> Hors périmètre : tout `app/web` (templates, routes, static) et tout Sage BIJOU (tables `F_*`, triggers).

---

## 1. Entités métier

### 1.1 `invoices` — cœur facturation (factures + avoirs, une seule table)

| Colonne | Type / défaut | Rôle |
|---|---|---|
| `id` | INTEGER PK AI | identifiant interne |
| `numero` | TEXT UNIQUE NOT NULL (`FA{:06d}` / `AV{:06d}`) | numéro d'affaire, anti-collision par boucle |
| `date_facture` / `date_echeance` / `reference` | TEXT | dates ISO, référence libre |
| `contact_id` → `contacts(id)` | INTEGER FK | lien facultatif (dénormalisé ci-dessous) |
| `tiers_code/nom/niu/email/telephone/adresse`, `tiers_type` (`business` déf.) | TEXT | snapshot client au moment de la facture |
| `montant_ht/tva/ttc/restant` | REAL 0.0 | agrégats (`restant = ttc` à la création) |
| `statut` (`brouillon` déf.) | TEXT | `brouillon` → `valide` → `a_comptabiliser` |
| `valide` | INTEGER 0 | flag de validation |
| `type_doc` (`vente` déf.) | TEXT | `vente` \| `avoir` |
| `source` (`web` déf.) | TEXT | `web` \| `pos` \| import (`sage` historique) |
| `sfec_statut` (`''` déf.) | TEXT | `''` → `EN_COURS` → `CERTIFIE`/`DEJA_CERTIFIE`/`ERREUR` |
| `sfec_num_certif/signature/qr_code/date_certif/id` | TEXT | preuve de certification SFEC |
| `payment_method` (`bank_transfer`), `devise` (`XAF`) | TEXT | paiement + devise |
| `recipient_rccm`, `is_recipient_taxable` (1) | TEXT/INTEGER | conformité SFEC |
| `reference_invoice_id` | TEXT | **n° facture d'origine si avoir (R1)** |
| `montant_ht_brut`, `total_tax_t_amount` (TVA 18%), `total_tax_r_amount` (TVA 5%), `total_exempt_amount`, `discount_amount` (globale), `total_line_discount_amount`, `additional_cent_tax`, `electronic_stamp_duty` (= 0 absolu) | REAL 0.0 | ventilation pour SFEC |
| `vendeur_id` → `vendeurs(id)` | INTEGER FK | vendeur/caissier |
| `notes`, `payment_date` | TEXT | libres |
| `synced_sage/sage_domaine/sage_type(6)/sage_piece` | INTEGER/TEXT | **À REMPLACER** par `synced_backend` + `backend_ref` dans l'adaptateur |
| `created_at/updated_at` | TEXT `datetime('now')` | audit |

Index : `numero`, `date_facture`, `statut`, `sfec_statut`, `source`, `synced_sage` +
unique partiel anti-double-avoir sur `reference_invoice_id` (`WHERE type_doc='avoir'`).

### 1.2 `invoice_lines` — détail facture

`invoice_id` FK CASCADE, `numero_ligne`, `designation`, `quantite` (1.0), `prix_unitaire`,
`remise_pct`/`remise_montant`, `montant_ht` (net), `taux_tva` (18.0), `montant_tva/ttc`,
`code_article/compte/famille`, `unite` (`U`), `product_id` FK, `subtotal` (brut qté×PU),
`discount_type` (`fixed`/`percentage`), `type_article` (`product`/`service`), `classification_code`.
Index `(invoice_id)`, `(invoice_id, numero_ligne)`, `(product_id)`.

### 1.3 `pos_tickets` / `pos_ticket_lines` — caisse

Ticket : `numero` unique (`TK{:06d}`), `date_ticket`, `tiers_nom` (`Client comptoir` déf.),
`montant_ht/tva/ttc` (recalculés), `mode_paiement` (`especes` déf. : especes/carte/cheque/virement/mobile_money),
`montant_recu`, `monnaie_rendue = max(reçu − TTC, 0)`, `statut` (toujours `valide`),
`invoice_id` FK (facture miroir obligatoire), `vendeur_id`/`caissier`,
`remise_globale_pct/montant`, `synced_sage` → renommer `synced_backend`.
Lignes : `ticket_id` FK CASCADE, `numero_ligne`, `designation`, `quantite`, `prix_unitaire`,
`montant_ht` (net), `taux_tva` (18.0), `montant_tva/ttc`, `remise_pct/montant`, `code_article/barcode`, `product_id` FK.
Index `(ticket_id)`, `(ticket_id, numero_ligne)`, `(product_id)`.

### 1.4 Référentiels (tous gardés)

- **`contacts`** : `code` UNIQUE, `nom`, `type` (`client`/`fournisseur`), `niu` (unique si non vide),
  `email/telephone/adresse/ville/pays` (`CG` déf.), `rccm`, `is_taxable` (1),
  `synced_sage` → `synced_backend`, `sage_ct_num` → `backend_key`.
- **`products`** : `ref` UNIQUE, `barcode`, `designation`, `famille`, `nature`, `prix_vente/achat`,
  `tva_code` (`18` déf.), `unite` (`U`), `stock_reel`, `est_actif`, `synced_sage`, `sage_ar_ref` → `backend_key`.
- **`vendeurs`** : `code` UNIQUE, `nom/prenom/email/telephone`, `role` (`vendeur` déf.), `est_actif`, `pin`.
- **`tax_rates`** : `code` UNIQUE, `libelle`, `taux`, `est_actif` — seeds `18/5/0 %`.
- **`utilisateurs`** : `email` UNIQUE, mot de passe hashé `pbkdf2:sha256`, `role`
  (`admin/responsable/caissiere/financiere`), `est_actif`, `failed_attempts`, `locked_until`.
  Pas de table rôles : matrice JSON 16 permissions en `settings.auth_matrix`.
- **`audit_log`** : `ts/utilisateur/action/detail/ip` (détail tronqué à 500).
- **`settings`** : compteurs (`invoice/avoir/pos_next_number` + formats), `auth_matrix`, préférences.
- **`sync_log`** : `table_name/record_id/record_numero/action/direction/status/error` — traçabilité sync.

---

## 2. Règles de gestion

| # | Règle | Détail |
|---|---|---|
| R1 | Avoir : origine obligatoire | `type_doc='avoir'` ⇒ `reference_invoice_id` non vide, sinon rejet |
| R2 | Avoir : origine certifiée | la facture d'origine doit exister **et** `sfec_statut IN (CERTIFIE, DEJA_CERTIFIE)` |
| R3 | Avoir : unicité | 1 seul avoir par facture (check applicatif + index unique partiel) |
| R4 | NIU contact | unicité si renseigné ; `code + nom` obligatoires |
| R5 | Stock | si contrôle actif : `qté > stock_reel` ⇒ ticket bloqué avec détail ; décrément `MAX(stock − qté, 0)` |
| R6 | Monnaie | `max(reçu − TTC, 0)` |
| R7 | Facture miroir POS | tout ticket crée une facture `a_comptabiliser, valide=1, type_doc='vente', source='pos'`, `montant_restant=0`, `reference='PAIEMENT <mode>'` |
| R8 | Édition facture | whitelist de champs + lignes supprimées/recréées + recalcul |
| R9 | Éligibilité SFEC | `statut IN (valide, a_comptabiliser)` ET `sfec_statut NOT IN (CERTIFIE, DEJA_CERTIFIE, EN_COURS, ERREUR)` |
| R10 | Numérotation | `FA/AV/TK{:06d}`, boucle anti-collision sur `numero UNIQUE`, incrément dans la même transaction |
| R11 | Mots de passe | hash `pbkdf2:sha256`, verrouillage après échecs, bootstrap admin depuis config si 0 utilisateur |

---

## 3. Formules

**Ligne vente** : `subtotal = qté×PU` → `remise = subtotal×%` → `net = subtotal − remise` →
`TVA = net×taux/100` → `TTC = net + TVA` (arrondis 2 décimales).
**Ligne ticket** : idem + remise fixe cumulée, `net = max(subtotal − remise_totale, 0)`.
**Facture** : `HT/TVA/TTC = Σ lignes` ; buckets `T(≠5,0) / R(=5) / exonéré(=0 sur HT)` ;
`centime additionnel = arrondi(TVA_18% × 5 %, 2)` ; `timbre électronique = 0` (absolu).
**Ticket** (remise globale) : remise calculée puis **prorata HT/TVA** (la TVA est conservée).
**Ventilation POS→facture** : `HT brut = Σ subtotal`, `remises lignes = Σ`, `T/R/exonéré` par taux,
`remise globale résiduelle = max(net − HT recalculé, 0)`.
**Montant en lettres** : tranches milliard/million/mille + `X francs CFA`.
**Paiements → SFEC** : especes→cash, carte→card, cheque→check, virement→bank_transfer, mobile_money→mobile_money (défaut bank_transfer).

---

## 4. Machines à états

- **`statut`** : `brouillon` (défaut web) → `valide` → `a_comptabiliser` (POS crée directement `a_comptabiliser`).
- **`sfec_statut`** : `''` → `EN_COURS` → `CERTIFIE` / `DEJA_CERTIFIE` / `ERREUR`.
- **Pipeline ticket** : valider lignes → contrôle stock → INSERT ticket+lignes → recalcul+monnaie →
  décrément stock → facture miroir → certif SFEC (`EN_COURS` si hors ligne/désactivé, `ERREUR` si rejet).
- **Édition PDF** : société → entête légale (nom, NIU, RCCM, régime, capital) → lignes (10 col.) →
  11 lignes de totaux → montant en lettres → bloc SFEC (n°, date, signature, QR image exacte).

---

## 5. Contrat SFEC (inchangé dans le custom)

- Transport : `X-API-Key`, `base_url` / `sandbox`, `timeout=30`, `POST /api/v1/invoices` (certify),
  `GET /api/v1/invoices` (liste paginée 500), `GET /api/v1/invoices/{id}` (détail).
- Payload : `invoice_id`, `invoice_type` (`salesInvoice`/`creditNote`), `recipient_type`/`name`,
  `subtotal`, `total_tax_t_amount` (18%) / `total_tax_r_amount` (5%) / `total_exempt_amount`,
  `total_tax_amount`, `discount_amount`, `total_line_discount_amount`, `additional_cent_tax`,
  `electronic_stamp_duty = 0`, `total_amount`, `amount_due`, `currency` (XAF/USD),
  `items[]` (`designation`, `type` product/service, `unit_price`, `quantity`, `subtotal`,
  `discount_amount`, `net_amount`, `tax_rate` "0"/"5"/"18", `tax_amount`, `total_amount`),
  `taxpayer_niu` (≤ 20 car.), `reference_invoice_id` (avoir uniquement), `recipient_niu`
  (**16-17 car. requis** pour business/government), `recipient_rccm/phone/email/address` (requis si government/foreign).
- Validation : montants > 0, `total = subtotal − remises + TVA + centime + timbre` (tolérance 0.01),
  avoir ⇒ origine requise.
- Réponse : `signature` (64 hex), `short_signature` (20), `qr_code` (PNG data-URI), `certification_date`.
  Si pas de numéro : polling `12 × 2 s`. Codes : `400/422` rejet, `404` introuvable, `409` déjà certifié
  (⇒ marquer `DEJA_CERTIFIE`), `502` ⇒ à surveiller, `0` hors ligne ⇒ file retry.
- QR : **afficher l'image SFEC exacte** (décoder le data-URI), jamais la ré-encoder.

---

## 6. Sync à adaptateur abstrait (remplace Sage)

Ordre `full_sync` (conservé) : **certifier l'attente POS → pousser contacts → pousser factures → tirer le référentiel**.
Retry : file en mémoire + backoff, statuts `EN_COURS` bloqués remis à NULL, réconciliation toutes les 8 boucles.
Polling : 30 s (configurable), bi-sync 60 s, lookup SFEC en cache TTL 300 s, pool d'envoi 3-5 threads.

### Interface `BillingSystemAdapter` (à implémenter pour le système propre)

```python
class BillingSystemAdapter:
    def fetch_invoices(self, updated_from=None, limit=None): ...
    def fetch_contacts(self): ...
    def fetch_tax_rates(self): ...
    def fetch_articles(self): ...
    def write_contact(self, contact) -> ct_key: ...
    def write_invoice(self, invoice) -> piece_ref: ...
    def mark_certifying(self, ref): ...
    def mark_certified(self, ref, cert_data): ...
    def mark_failed(self, ref, error): ...
    def ensure_cert_columns(self): ...   # no-op si le système stocke déjà la preuve SFEC
```

Règles d'ordre impératives : **contacts avant factures** ; une facture ne part que `synced_backend=0`
et non `brouillon` (POS exige en plus la certif) ; ne jamais écraser les champs SFEC d'une pièce `CERTIFIE`.

---

## 7. Ce qu'on JETTE de BIJOU (ne pas reporter)

Tables `F_DOCENTETE/F_DOCLIGNE/F_COMPTET/F_TAXE/F_COMPTEG/F_ARTICLE/F_ARTSTOCK` ;
`DO_Domaine 0/1`, `DO_Type 6/7`, `DO_Statut 0-4`, `CT_Type 0/1/2`, `DO_Tiers/CT_Num` ;
triggers `TG_INS_CPTAF_DOCENTETE` (**erreur 82019**, cause du chantier push) ;
`DO_CodeTaxe1/DL_CodeTaxe1` (`C18/C05/C00/C20`) ; `TA_Taux/TA_Code` ;
colonnes `SFEC_*` ajoutées en base Sage ; `cbCreation/cbModification` ;
hypothèse `DO_Ref` = lien avoir (fausse) ; connexion ODBC + lock global + instance nommée.

---

## 8. Référentiels, auth, seeds (gardés tels quels)

Contacts (NIU unique), produits (`tva_code` 18/5/0), TVA (seeds 18/5/0), vendeurs, utilisateurs
+ matrice 16 permissions en JSON + `audit_log`, seeds déterministes (`FAMILLES`, 60 articles,
14 contacts, 3 vendeurs, `INSERT OR IGNORE`), migration additive idempotente
(`ALTER TABLE ADD COLUMN` si absent, jamais de `DROP`), config JSON par sections
(`db/sfec/company/queue/validation/notifications/rate_limit/dashboard/pos/log_level`)
avec overlay `SFEC_API_KEY` prioritaire.
