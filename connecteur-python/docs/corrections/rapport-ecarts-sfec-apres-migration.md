# Rapport d'écarts SFEC post-migration — Procédure de correction

> **Date** : 2026-09-14
> **Base analysée** : `connecteur-python/data/tconnector.db`
> **Schéma de référence** : `connecteur-python/app/storage/db.py`
> **Plan d'origine** : `docs/gap-invoice/a.md`

---

## 1. Résumé de l'avancement

Le plan d'alignement SFEC (`a.md`) prévoyait 8 étapes. La migration schéma (étape 1) et
une partie de l'étape 2 sont réalisées. Les points suivants sont **déjà en place** :

| Élément | Fichier | Statut |
|---|---|---|
| Colonnes SFEC dans `invoices` | `db.py:179-192` | OK |
| Colonnes SFEC dans `invoice_lines` | `db.py:217-220` | OK |
| Colonnes `rccm`, `is_taxable` dans `contacts` | `db.py:82-83` | OK |
| Index unique avoir (`idx_invoices_avoir_unique`) | `db.py:304-306` | OK |
| Index unique NIU (`idx_contacts_niu_unique`) | `db.py:307-308` | OK |
| Migration `_migrate()` pour toutes les colonnes | `db.py:330-395` | OK |
| `generate_avoir_number()` | `db.py:490-505` | OK |
| `avoir_existe_pour()` + `_validate_avoir()` | `invoices.py:13-50` | OK |
| Mapping `type_doc → SFEC` | `endpoints.py:14-17, 75-76` | OK |
| `db_invoice_to_sfec()` lit les nouveaux champs | `endpoints.py:246-298` | OK |
| `validate_sfec_payload()` vérifie `creditNote` | `endpoints.py:344` | OK |
| `sqlite_invoice_to_sfec()` propage tous les champs | `endpoints.py:516-565` | OK |
| `create_contact()` vérifie doublon NIU | `pos.py:624-635` | OK |
| `recalc_invoice_totals()` calcule totaux détaillés | `db.py:547-586` | OK |
| `_create_invoice_for_ticket()` écrit colonnes détaillées | `pos.py:124-208` | OK |
| `fmt_money()` helper monétaire | `core/utils.py:55-66` | OK |
| Impression certifiée utilise `fmt_money` | `dashboard.py:163-188` | OK |

---

## 2. Écarts résiduels (à corriger)

### BUG-001 — `create_invoice()` n'insère pas les nouvelles colonnes

**Fichier** : `app/domain/invoices.py:83-96`
**Criticité** : Bloquant

Le INSERT SQL ne inclut que les anciennes colonnes. Les valeurs lues du dict
(`payment_method`, `devise`, `recipient_rccm`, `is_recipient_taxable`,
`reference_invoice_id`, `montant_ht_brut`) ne sont jamais écrites en base.

**Procédure** :

1. Dans `invoices.py`, étendre le INSERT pour ajouter les colonnes manquantes :

```python
# invoices.py:83-96 — INSERT à modifier
cur.execute("""
    INSERT INTO invoices (
        numero, date_facture, date_echeance, reference,
        contact_id, tiers_code, tiers_nom, tiers_niu,
        tiers_email, tiers_telephone, tiers_adresse, tiers_type,
        statut, type_doc, source, notes,
        payment_method, devise, recipient_rccm,
        is_recipient_taxable, reference_invoice_id, montant_ht_brut
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    numero, date_facture, date_echeance, reference,
    contact_id, tiers_code, tiers_nom, tiers_niu,
    tiers_email, tiers_telephone, tiers_adresse, tiers_type,
    statut, type_doc, source, notes,
    payment_method, devise, recipient_rccm,
    is_recipient_taxable, reference_invoice_id, montant_ht_brut,
))
```

2. Valeurs par défaut à définir en amont si non fournies :

```python
payment_method = data.get("payment_method", "bank_transfer")
devise = data.get("devise", "XAF")
recipient_rccm = data.get("recipient_rccm", "")
is_recipient_taxable = data.get("is_recipient_taxable", 1)
reference_invoice_id = data.get("reference_invoice_id", "")
montant_ht_brut = data.get("montant_ht_brut", 0.0)
```

3. Ajouter un compteur d'avoirs dans `count_invoices()` :

```python
# invoices.py:254-273 — ajouter après les autres compteurs
cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE type_doc = 'avoir'")
stats["avoirs"] = cur.fetchone()["cnt"]
```

---

### BUG-002 — `_validate_avoir()` n'est jamais appelé

**Fichier** : `app/domain/invoices.py:79-80`
**Criticité** : Bloquant

La ligne 79 contient juste `print("c'est un autre avoir")` sans appeler
`_validate_avoir()`. Les 3 règles d'intégrité avoir ne sont jamais appliquées.

**Procédure** :

1. Remplacer la ligne 79-80 par l'appel réel :

```python
# invoices.py:79-80 — remplacer
if type_doc == 'avoir':
    _validate_avoir(reference_invoice_id)
```

2. Le code d'appel de `create_invoice()` dans `billing.py:214` doit attraper
   `InvoiceIntegrityError` pour retourner un message propre :

```python
# billing.py:213-219 — le bloc try/except existe déjà, pas de changement nécessaire
# car InvoiceIntegrityError hérite de ValueError et sera capturé par le except Exception
```

3. Vérifier que `_validate_avoir` est bien importé (il est défini dans le même
   fichier, donc pas d'import nécessaire).

---

### BUG-003 — Pas de gestion `IntegrityError` dans `create_invoice()` pour l'index unique avoir

**Fichier** : `app/domain/invoices.py:83-96`
**Criticité** : Bloquant

Si un double avoir est tenté malgré `_validate_avoir()` (course conditionnelle),
le INSERT crash avec une `IntegrityError` non gérée → erreur 500.

**Procédure** :

1. Ajouter un `try/except` autour du INSERT dans `create_invoice()` :

```python
# invoices.py:83-96 — entourer le INSERT
import sqlite3

try:
    with get_cursor() as cur:
        cur.execute("""...""", (...))
        invoice_id = cur.lastrowid
except sqlite3.IntegrityError:
    raise InvoiceIntegrityError(
        "Un avoir existe deja pour la facture '{}'.".format(reference_invoice_id)
    )
```

2. S'assurer que `sqlite3` est importé en haut du fichier (déjà présent via
   `from app.storage.db import ...` mais pas `import sqlite3` directement).

---

### BUG-004 — PDF ne génère pas les mentions légales SFEC

**Fichier** : `app/domain/pdf.py`
**Criticité** : Majeur

Le PDF présente plusieurs lacunes :

| Problème | Ligne | Détail |
|---|---|---|
| Toujours "FACTURE DE VENTE" | `pdf.py:80` | Pas de variation selon `type_doc` |
| Pas de RCCM vendeur | `pdf.py:67-74` | En-tête company ne montre pas RCCM |
| Pas de régime fiscal | — | Absent |
| Pas de capital social | — | Absent |
| Pas de RIB/IBAN | — | Absent |
| Pas de montant en lettres | — | Absent |
| Pas de QR code | `pdf.py:163-172` | Certif SFEC affichée sans QR image |
| Pas de centime additionnel | — | Absent des totaux |
| Pas de décompte TVA 18%/5%/exonéré | — | Seulement TVA totale |
| Pas de remises lignes | — | Absent |
| Pas de net à payer | — | Seulement TTC |
| Pas de signature électronique | — | Absent |

**Procédure** :

#### 4.1 — En-tête vendeur enrichi (`pdf.py:67-74`)

```python
# Ajouter après l'affichage NIU (ligne 72)
if company.get("rc_number"):
    elements.append(Paragraph("RCCM: {}".format(company["rc_number"]), styles["SubTitle"]))
if company.get("tax_regime"):
    elements.append(Paragraph("Regime fiscal: {}".format(company["tax_regime"]), styles["SubTitle"]))
if company.get("capital"):
    elements.append(Paragraph("Capital social: {}".format(company["capital"]), styles["SubTitle"]))
if company.get("bank_iban") or company.get("bank_account"):
    bank_info = company.get("bank_iban") or company.get("bank_account", "")
    elements.append(Paragraph("RIB/IBAN: {}".format(bank_info), styles["SubTitle"]))
```

Vérifier que `config.json` contient les clés `rc_number`, `tax_regime`, `capital`,
`bank_iban` dans la section `company`. Sinon, les ajouter dans
`app/config/manager.py` au niveau du schema de config.

#### 4.2 — Nature du document (`pdf.py:80`)

```python
# Remplacer la ligne 80
type_doc = invoice.get("type_doc", "vente")
doc_title = "FACTURE D'AVOIR" if type_doc == "avoir" else "FACTURE DE VENTE"
elements.append(Paragraph("<b>{}</b>".format(doc_title), styles["Title2"]))

# Si avoir, afficher la référence facture d'origine
if type_doc == "avoir" and invoice.get("reference_invoice_id"):
    elements.append(Paragraph(
        "Facture d'origine: {}".format(invoice["reference_invoice_id"]),
        styles["SmallLeft"]
    ))
```

#### 4.3 — Décompte TVA détaillé + centime additionnel (`pdf.py:131-133`)

Remplacer la section totaux par :

```python
# TVA 18%
tva_18 = invoice.get("total_tax_t_amount", 0) or invoice.get("montant_tva", 0)
# TVA 5%
tva_5 = invoice.get("total_tax_r_amount", 0)
# Exonéré
exonere = invoice.get("total_exempt_amount", 0)
# Centime additionnel
centime = invoice.get("additional_cent_tax", 0)
# Remises
remise_lignes = invoice.get("total_line_discount_amount", 0)
remise_globale = invoice.get("discount_amount", 0)

table_data.append(["", "", "", "", "TOTAL HT BRUT", "{:,.0f}".format(invoice.get("montant_ht_brut", 0)), "", ""])
if remise_lignes:
    table_data.append(["", "", "", "", "Remises lignes", "-{:,.0f}".format(remise_lignes), "", ""])
if remise_globale:
    table_data.append(["", "", "", "", "Remise globale", "-{:,.0f}".format(remise_globale), "", ""])
table_data.append(["", "", "", "", "TOTAL HT", "{:,.0f}".format(invoice.get("montant_ht", 0)), "", ""])
table_data.append(["", "", "", "", "TVA 18%", "", "{:,.0f}".format(tva_18), ""])
if tva_5:
    table_data.append(["", "", "", "", "TVA 5%", "", "{:,.0f}".format(tva_5), ""])
if centime:
    table_data.append(["", "", "", "", "Centime add.", "", "{:,.0f}".format(centime), ""])
total_tva_all = tva_18 + tva_5 + centime
table_data.append(["", "", "", "", "TOTAL TVA", "", "{:,.0f}".format(total_tva_all), ""])
if exonere:
    table_data.append(["", "", "", "", "Exonere", "", "{:,.0f}".format(exonere), ""])
table_data.append(["", "", "", "", "TOTAL TTC", "", "", "{:,.0f}".format(invoice.get("montant_ttc", 0))])
table_data.append(["", "", "", "", "NET A PAYER", "", "", "{:,.0f}".format(invoice.get("montant_ttc", 0))])
```

#### 4.4 — Montant en lettres

Créer une fonction `_montant_en_lettres(amount)` dans `pdf.py` :

```python
def _montant_en_lettres(amount):
    """Conversion nombre → lettres en français (simplifié, jusqu'à 999 999 999)."""
    if amount <= 0:
        return "Zero"
    # Utiliser la lib python-tifinagh ou implémenter un convertisseur basique
    # Pour une implémentation complète, voir la lib `num2words`
    try:
        from num2french import num2french
        return num2french(round(amount))
    except ImportError:
        return "{} FCFA".format("{:,.0f}".format(amount))
```

Ajouter `num2words` à `requirements.txt` si nécessaire.

#### 4.5 — QR code et signature électronique (`pdf.py:163-172`)

```python
# Après la section certification, ajouter le QR code image
sfec_qr = invoice.get("sfec_qr_code", "")
if sfec_qr:
    if sfec_qr.startswith("data:"):
        # QR code en base64 data URI
        import base64
        qr_data = sfec_qr.split(",", 1)[1] if "," in sfec_qr else ""
        if qr_data:
            qr_bytes = base64.b64decode(qr_data)
            qr_buf = BytesIO(qr_bytes)
            elements.append(Spacer(1, 3*mm))
            elements.append(Image(qr_buf, width=30*mm, height=30*mm))
    else:
        # QR code en texte brut
        elements.append(Paragraph("QR: {}".format(sfec_qr[:80]), styles["SmallLeft"]))

sfec_sig = invoice.get("sfec_signature", "")
if sfec_sig:
    elements.append(Paragraph("Signature electronique: {}".format(sfec_sig[:60]), styles["SmallLeft"]))
```

#### 4.6 — Ajouter `qrcode` dans `requirements.txt`

```
qrcode>=7.0
```

Ou si le QR est déjà fourni par SFEC sous forme d'image base64, aucune dépendance
supplémentaire n'est nécessaire.

---

### BUG-005 — `_save_lines()` n'insère pas les nouvelles colonnes des lignes

**Fichier** : `app/domain/invoices.py:276-310`
**Criticité** : Majeur

Le INSERT dans `_save_lines()` ne contient pas `subtotal`, `discount_type`,
`type_article`, `classification_code`.

**Procédure** :

1. Étendre le INSERT :

```python
# invoices.py:298-310 — ajouter les colonnes manquantes
cur.execute("""
    INSERT INTO invoice_lines (
        invoice_id, numero_ligne, designation, quantite, prix_unitaire,
        remise_pct, remise_montant, montant_ht, taux_tva,
        montant_tva, montant_ttc, code_article, code_compte,
        famille, unite, product_id,
        subtotal, discount_type, type_article, classification_code
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""", (
    invoice_id, numero_ligne, designation, quantite, prix_unitaire,
    remise_pct, remise_montant, montant_ht, taux_tva,
    montant_tva, montant_ttc, code_article, code_compte,
    famille, unite, product_id,
    subtotal, discount_type, type_article, classification_code,
))
```

2. Extraire les valeurs avant le INSERT :

```python
subtotal = round(quantite * prix_unitaire, 2)
discount_type = ligne.get("discount_type", "fixed")
type_article = ligne.get("type_article", "product")
classification_code = ligne.get("classification_code", "")
```

---

### Seeds avoir absentes

**Fichier** : `app/storage/db.py:411-428`
**Criticité** : Majeur

`_seed_default_settings()` ne contient pas les clés `avoir_prefix`, `avoir_format`,
`avoir_next_number`. Or `generate_avoir_number()` (ligne 491) lit
`doc_format_avoir` / `doc_next_avoir` — il y a un **désalignement de noms de clés**.

**Procédure** :

1. Aligner les noms de clés dans `generate_avoir_number()` :

```python
# db.py:491 — corriger
fmt = get_setting("avoir_format", "AV{:06d}")
# db.py:493 — corriger
cur.execute("SELECT value FROM settings WHERE key = 'avoir_next_number'")
# db.py:501 — corriger
cur.execute(
    "UPDATE settings SET value = ?, updated_at = datetime('now') WHERE key = 'avoir_next_number'",
    (str(num + 1),)
)
```

2. Ajouter les seeds dans `_seed_default_settings()` :

```python
# db.py:411-428 — ajouter
"avoir_prefix": "AV",
"avoir_format": "AV{:06d}",
"avoir_next_number": "1",
```

---

### Formatage monétaire dans `dashboard.py` lignes tickets

**Fichier** : `app/web/routes/dashboard.py:223`
**Criticité** : Mineur

La ligne 223 utilise encore `{:,.0f}` en formatage inline pour les montants des
tickets POS, au lieu d'utiliser `fmt_money()`.

**Procédure** :

```python
# dashboard.py:223 — remplacer
ticket_rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{}</td><td>{}</td><td>{}</td><td>{}</td><td><a href='/pos/ticket/{}/print' class='btn btn-sm' target='_blank'>Voir</a></td></tr>".format(
    _esc(t.get("numero", "")), _esc(t.get("date_ticket", "")[:16]),
    _esc(t.get("tiers_nom", "")), fmt_money(t.get("montant_ttc", 0)),
    _esc(t.get("mode_paiement", "")), _esc(vendeur_txt), sfec_cell, t.get("id", "")
)
```

De même pour les lignes dans `invoices_page()` (ligne 78) et `pending_page()` (ligne 116).

---

## 3. Procédure d'application (ordre recommandé)

### Phase 1 — Bloquants (BUG-001, 002, 003)

1. `invoices.py` — Corriger le INSERT de `create_invoice()` pour inclure les
   nouvelles colonnes (BUG-001)
2. `invoices.py` — Remplacer le `print()` par `_validate_avoir()` (BUG-002)
3. `invoices.py` — Ajouter la gestion `IntegrityError` dans `create_invoice()` (BUG-003)
4. `invoices.py` — Corriger `_save_lines()` pour insérer `subtotal`, `discount_type`,
   `type_article`, `classification_code` (BUG-005 lignes)

### Phase 2 — Seeds et numérotation

5. `db.py` — Corriger les noms de clés dans `generate_avoir_number()`
6. `db.py` — Ajouter les seeds avoir dans `_seed_default_settings()`

### Phase 3 — PDF (BUG-004)

7. `pdf.py` — En-tête vendeur enrichi (RCCM, régime fiscal, capital, RIB)
8. `pdf.py` — Variation titre selon `type_doc` (VENTE / AVOIR)
9. `pdf.py` — Décompte TVA détaillé + centime additionnel + remises
10. `pdf.py` — Montant en lettres
11. `pdf.py` — QR code image + signature électronique
12. `pdf.py` — Référence facture d'origine pour les avoirs

### Phase 4 — Web / formatting

13. `dashboard.py` — Uniformiser `fmt_money()` sur toutes les pages
14. Vérifier les templates (`billing/form.html`, `billing/detail.html`,
    `billing/list.html`, `directory/clients.html`) pour les champs ajoutés

### Phase 5 — Tests

15. Lancer `pytest tests/` pour vérifier la non-régression
16. Vérifier que `tests/test_smoke.py` passe (94 routes, status codes)
17. Régénérer les snapshots si nécessaire : `python tests/compare_snapshots.py`

---

## 4. Vérification post-correction

```bash
# Depuis connecteur-python/
python -m pytest tests/ -v

# Vérifier le schéma SQLite
python -c "
from app.storage.db import init_database, get_cursor
init_database()
with get_cursor() as cur:
    cur.execute('PRAGMA table_info(invoices)')
    cols = [r['name'] for r in cur.fetchall()]
    print('Colonnes invoices:', len(cols))
    for c in ['payment_method','devise','reference_invoice_id','additional_cent_tax']:
        print(f'  {c}: {\"OK\" if c in cols else \"MANQUANT\"}')" 
```
