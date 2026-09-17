# Rapport d'incohérences — Base BIJOU vs T-CONNECTOR

**Date :** 2026-09-16 · **Base :** BIJOU sur `10.42.0.2:1433` (login `sageUser`)
**Méthode :** connexion directe lue en live (pymssql ; `sqlcmd` absent de cette machine, connexion équivalente).
**État connexion :** OK en début d'audit, **puis serveur injoignable** (`nc -zv 1433` timeout en fin d'audit).
Les sections marquées ⏳ sont à compléter dès le retour du serveur (script prêt : `/tmp/bijou_audit.py`).

---

## 1. Constats vérifiés en live (données réelles BIJOU)

| # | Constat | Preuve |
|---|---------|--------|
| V1 | `F_DOCENTETE` (130 colonnes) et `F_DOCLIGNE` **existent** ; jointure `(DO_Domaine, DO_Type, DO_Piece)` fonctionnelle (12 lignes liées aux avoirs) | `COUNT TABLE = 2`, jointure testée |
| V2 | Domaine ventes (`DO_Domaine=0`) : types 0,1,2,3,4,5,6,7 présents ; **4 factures** (`DO_Type=6`), **3 avoirs** (`DO_Type=7` : FA00001, FA00002, FA00003) | `GROUP BY DO_Type` |
| V3 | **Aucun `DO_Ref` d'avoir ne matche un `DO_Piece` de facture** (`MOB-0901/001`, `FOR 0901/34`, `PAR-0109/48` → 0 match) | `LEFT JOIN … IS NULL` × 3 |
| V4 | **Colonnes `SFEC_*` absentes** de `F_DOCENTETE` (`LIKE 'SFEC%'` → 0 ligne) | `INFORMATION_SCHEMA` |
| V5 | Échantillon factures : `FA00007/CARAT/590.00/704.76`, `FA00008/PLATI/590.00/718.92` — tiers et montants renseignés | `SELECT TOP 2` |
| V6 | Pas de doublons de `DO_Ref` entre les 3 avoirs | `HAVING COUNT(*)>1` → vide |

---

## 2. Incohérences (ce qui empêche le connecteur de bien fonctionner)

### 🔴 I1 — Matching avoir ↔ facture basé sur `DO_Ref` : FAUX sur BIJOU
- **Code concerné :** `app/sync/engine.py:563` — `ref = inv.get("reference", "")  # HYPOTHESE: DO_Ref porte le numero de la facture d'origine`.
- **Réalité BIJOU (V3) :** `DO_Ref` porte des références externes (n° commande client/fournisseur), jamais un `DO_Piece`.
- **Impact :** tout avoir importé de Sage tombe dans « Avoir sans référence facture d'origine » ou « Facture d'origine non certifiée » → **jamais certifié**, et la règle « 1 seul avoir par facture » est inverifiable côté Sage.
- **Correspondance nécessaire :** `invoices.reference_invoice_id` (saisie formulaire billing) reste la **source de vérité**. Côté Sage, importer les `DO_Type=7` avec `reference_invoice_id = ''` (à compléter manuellement), **ne jamais utiliser `DO_Ref` comme lien**.
- **Piste `F_DOCREGL` — INFIRMÉE (vérifié 17/09/2026) :** `F_DOCREGL` ne contient que les **règles/échéanciers de règlement attachées à chaque pièce**, pas le lien avoir↔facture.
  - Chaque pièce (dont les avoirs FA00001-03, lignes DR_No 54/56/57) a ses propres lignes `(DO_Domaine, DO_Type, DO_Piece)` pointant vers **la pièce elle-même** ; aucun champ ne référence une autre pièce.
  - Sur BIJOU : `DR_TypeRegl=2`, `DR_Montant=0`, `EC_No=0`, `DR_RefPaiement=NULL` sur 60 lignes → aucun règlement/lettrage réel exploitable.
  - Les avoirs n'ont **aucun** lien ailleurs non plus : `DO_PieceOrig=''`, `DO_Ref` = références externes sans correspondance, lignes `F_DOCLIGNE` sans `DL_PieceBL/BC/DE`, `AC_RefClient=''`.
  - **Conclusion :** dans ce dossier, l'avoir est une pièce **autonome** ; le matching doit rester via `invoices.reference_invoice_id` (saisie manuelle/config). À re-tester sur des données de **production** créées « depuis une facture » (Sage renseigne alors `DO_Ref`/`DO_PieceOrig`), mais rien d'automatisable sur BIJOU.

### 🔴 I2 — `read_invoices_from_sage` ne lit que `DO_Type=6`, ignore les avoirs
- **Code :** `app/integration/sage/writer.py:130` — `WHERE d.DO_Domaine = 0 AND d.DO_Type = 6`.
- **Impact :** les 3 avoirs BIJOU (V2) ne sont **jamais importés** dans SQLite → le matching avoir (règles §2.5) ne s'applique qu'aux avoirs créés dans le connecteur, jamais à ceux de Sage.
- **Correspondance nécessaire :** étendre à `DO_Type IN (6, 7)` + mapper `type_doc = 'avoir' si DO_Type=7`, `reference_invoice_id = ''`.

### 🔴 I3 — Colonnes `SFEC_*` inexistantes → lecture Sage en échec total
- **Code :** `writer.py:125-128` — `ISNULL(d.SFEC_STATUT,'')`, `SFEC_NUM_CERTIF`, `SFEC_SIGNATURE`, `SFEC_QR_CODE` sélectionnés **en dur**.
- **Réalité BIJOU (V4) :** colonnes absentes → erreur SQL → `read_invoices_from_sage` retourne `[]` (log « Lecture Sage echouee »).
- **Correspondance nécessaire :** exécuter la migration `ALTER TABLE F_DOCENTETE ADD SFEC_STATUT/SFEC_NUM_CERTIF/SFEC_SIGNATURE/SFEC_QR_CODE` (déjà prévue dans `app/integration/sage/database.py`, jamais lancée contre BIJOU), **ou** rendre la lecture tolérante (détection colonnes comme `fetch_contacts` le fait avec `tp_cols`).

### 🟠 I4 — Clé de matching `numero` seule : collisions facture/avoir
- **Code :** `sync_sage_invoice_to_sqlite` — `SELECT id FROM invoices WHERE numero = ? AND source='sage'`.
- **Réalité BIJOU :** factures ET avoirs partagent le préfixe `FA` (`FA00001` avoir vs `FA00007` facture). Aujourd'hui pas de collision exacte, mais la clé naturelle Sage est `(DO_Domaine, DO_Type, DO_Piece)`, pas `DO_Piece` seul.
- **Correspondance nécessaire :** clé SQLite `(numero, sage_type)` ou stocker `sage_domaine/sage_type/sage_piece` (colonnes déjà prévues à l'INSERT ligne 207 !) et matcher dessus.

### 🟠 I5 — `DO_Tiers` vs `CT_Num` : contrainte non vérifiée ⏳
- **Code :** `database.py:480-484` détecte `DO_Tiers` (ou `CT_Num`) dans `F_DOCENTETE`, jointure `LEFT JOIN F_COMPTET c ON d.<tiers> = c.CT_Num`.
- **À vérifier :** `SELECT DISTINCT DO_Tiers … LEFT JOIN F_COMPTET … WHERE c.CT_Num IS NULL` (requête prête). Si des tiers de documents sont absents de `F_COMPTET`, les imports auront des factures sans nom de client.

### 🟠 I6 — NIU tiers : colonne `CT_NIU` à confirmer ⏳
- **Code :** `fetch_contacts` lit `c.CT_NIU AS numero_fiscal`, sinon `''` (silencieux).
- **Enjeu SFEC :** NIU acheteur **obligatoire** pour certifier (16-17 car.). Si BIJOU n'a pas de colonne NIU (ou vide), **aucune facture importée de Sage ne sera certifiable** sans enrichissement manuel.
- **À vérifier :** lister les colonnes fiscales de `F_COMPTET` (`NIU/NIF/FISCAL…`) + taux de remplissage (requêtes prêtes).

### 🟡 I7 — TVA : `DL_Taxe1` vs taux `F_TVA` ⏳
- **Code :** lignes lues avec `DL_Taxe1` comme taux ; `fetch_tax_rates` lit `F_TVA` (`TA_Code/TA_Taux`) ou variantes.
- **À vérifier :** `SELECT DISTINCT DL_Taxe1` vs `SELECT TA_Code, TA_Taux FROM F_TVA` — tout taux de ligne doit exister dans la table des taux, sinon `validate_sfec_payload` rejette à la certification.

### 🟡 I8 — Articles : `AR_Ref` des lignes vs `F_ARTICLE` ⏳
- **Code :** `article_sync.py` lit `F_ARTICLE` (log du 13/09 : « Table F_ARTICLE introuvable » → seed démo utilisé).
- **À vérifier :** existence + count de `F_ARTICLE` dans BIJOU ; `AR_Ref` des lignes sans correspondance (requête prête). Impact = catalogue vide /POS sans articles réels.

### 🟡 I9 — Cohérence montants entête vs lignes ⏳
- **À vérifier :** `SUM(DL_MontantHT)` par pièce vs `DO_TotalHT` (écart > 1.0 listé par le script). Un écart bloque `validate_sfec_payload` (tolérance `config.validation.tolerance_amount`).

### 🟡 I10 — Filtre `statut_code != 2` (engine:542) vs statuts réels ⏳
- **Code :** l'auto-certif ne traite que `DO_Statut = 2` (« A COMPTABILISER »).
- **À vérifier :** distribution `DO_Statut` des types 6/7 (requête prête). Si BIJOU utilise surtout 0 (SAISI) ou 3 (COMPTABILISE), **rien ne sera jamais auto-certifié**.

---

## 3. Matrice des correspondances (nécessaires au connecteur)

| Donnée SFEC/connecteur | Colonne Sage (BIJOU) | Statut |
|---|---|---|
| N° facture/avoir | `F_DOCENTETE.DO_Piece` + `DO_Type` (6/7) | ✅ vérifié |
| Type doc (vente/avoir) | `DO_Type` (6=vente, 7=avoir) | ✅ vérifié |
| Lien avoir → facture | **AUCUNE colonne Sage** (`DO_Ref` = ref externe, V3) → saisie `reference_invoice_id` | 🔴 I1 |
| Tiers (code) | `DO_Tiers` → `F_COMPTET.CT_Num` | ⏳ I5 |
| Tiers (nom) | `F_COMPTET.CT_Intitule` | ⏳ (même requête que I5) |
| NIU acheteur | `F_COMPTET.CT_NIU` (?) | ⏳ I6 |
| Montants HT/TVA/TTC | `DO_TotalHT`, `DO_Taxe1`, `DO_TotalTTC`, `DO_NetAPayer` | ⏳ I9 |
| Lignes | `F_DOCLIGNE` (`DL_Design`, `DL_Qte`, `DL_PrixUnitaire`, `DL_MontantHT`, `DL_Taxe1`, `DL_MontantTTC`, `AR_Ref`) | ✅ jointure OK (V1) |
| Taux TVA | `F_TVA.TA_Taux` vs `DL_Taxe1` | ⏳ I7 |
| Articles | `F_ARTICLE.AR_Ref/AR_Design` | ⏳ I8 |
| Statut certifiable | `DO_Statut = 2` | ⏳ I10 |
| Retour SFEC | `SFEC_STATUT/SFEC_NUM_CERTIF/SFEC_SIGNATURE/SFEC_QR_CODE` | 🔴 absentes (I3) |

---

## 4. Plan d'action proposé

1. **Relancer le serveur / réseau** `10.42.0.2:1433` (injoignable en fin d'audit) puis rejouer `/tmp/bijou_audit.py` → complète I5–I10.
2. **Migration `SFEC_*`** sur BIJOU (I3) — à valider avec toi avant exécution (écriture DDL).
3. **Correctifs code** (I1, I2, I4) : lecture `DO_Type IN (6,7)`, abandon `DO_Ref` comme lien, clé `(numero, sage_type)`.
4. Selon résultats NIU (I6) : décider enrichissement manuel vs import.

*Note : `sqlcmd` n'est pas installé sur cette machine — audit fait en pymssql (protocole TDS identique, mêmes résultats).*
