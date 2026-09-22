# Plan de correction — Push Sage : tiers d'abord, factures ensuite

> **Objectif** : `Push Sage: 0 ecrites, 11 erreurs (82019/50000)` → `N ecrites, 0 erreur`, sans perte de données.
> **Périmètre** : copie Windows `C:\Users\tp_m2\Documents\projects\TCONNECTOR-V2\connecteur-python` (celle qui tourne).
> **Date / base** : 2026-09-22, `BIJOU` live `172.31.16.1,1433` (`sageUser`), 59 tiers, `F_DOCENTETE` 62 pièces.

---

## 0. Diagnostic verrouillé (ne plus rouvrir)

| Fait | Preuve |
|---|---|
| `82019` = trigger `TG_INS_CPTAF_DOCENTETE` occ.1 (pos 871) : toute pièce `DO_Domaine IN (0,3,4)` avec tiers non vide doit matcher `F_COMPTET` avec `CT_Type = 0`, sinon `CB_Error 82019` → ODBC `42000/50000` | `sys.sql_modules` + `SUBSTRING` définition, lu le 2026-09-22 |
| Les 11 factures en échec portent des `tiers_code` locaux (`FA000026 → CLI-001`, `FA000013/015 → CLI-005`) inexistants dans `F_COMPTET` (tiers réels : `CARAT`, `CISEL`…) | SQLite `tconnector.db` (copie `/tmp`, original intact) + `SELECT CT_Num FROM F_COMPTET` |
| Aucun push contacts n'existe : 14 contacts SQLite `synced_sage=0, sage_ct_num=''` ; aucun `INSERT INTO F_COMPTET` dans `app/` (grep) | `contacts` + grep `F_COMPTET` |
| `writer.py` pousse `DO_Tiers = tiers_code` brut (`writer.py:51-52`) | lecture `writer.py` |
| La TVA/code-taxe n'est PAS la cause du `82019` (mais `DO_CodeTaxe1/DL_CodeTaxe1` déjà collés restent nécessaires pour l'étape d'après) | `writer.py:60-64,80-84` relus le 2026-09-22 |
| `CT_Type=2` ("Les deux", ex. `CAMET`, `CHEVALIER`) **échoue aussi** au trigger (exige strictement `=0`) | même trigger, occ.1 |
| `F_TAXE` `C18/C05/D18/D05` créés (`TA_No 22-25`) ; code figé à `18` (Option A) ; `avoir_type=7`, devise `CAST`, NIU→`CT_Identifiant`, `tp_cols` : tous OK côté Windows | vérifs `sqlcmd` + relectures du 2026-09-22 |

**Conséquence** : tant que les tiers n'existent pas côté Sage avec `CT_Type=0`, AUCUNE facture ne s'écrira, quel que soit le reste. L'ordre de correction est donc imposé : **tiers → factures**.

---

## Phase 1 — Push contacts → `F_COMPTET` (bloquant, à créer)

### 1.1 Spécification `write_contact_to_sage(contact)` (nouveau, `app/integration/sage/writer.py`)

Mapping SQLite `contacts` → `F_COMPTET`, **colonnes minimales sûres** (toutes les autres restent `NULL`/défaut) :

| SQLite | Sage | Limite | Règle |
|---|---|---|---|
| `code` | `CT_Num` | `varchar(17)`, `NOT NULL` | tronquer à 17 ; `CLI-001` (7) OK |
| `nom` | `CT_Intitule` | `varchar(69)` | tronquer à 69 ; obligatoire non vide (fallback `code` si vide) |
| `type='client'` → `0`, `'fournisseur'` → `1`, autre → `0` + warning | `CT_Type` | `smallint` | **jamais `2`** (le trigger pièces l'exige `=0` côté clients) |
| `niu` | `CT_Identifiant` | `varchar(25)` | tronquer à 25 ; `''` si absent |
| `telephone` | `CT_Telephone` | `varchar(21)` | tronquer à 21 |
| `email` | `CT_EMail` | `varchar(69)` | tronquer à 69 |
| `adresse` | `CT_Adresse` | `varchar(35)` | tronquer à 35 |
| `ville` → `CT_Ville` (35), `pays` → `CT_Pays` (35) | — | — | tronquer |

**Interdits** (déduits de `TG_INS_F_COMPTET`, 4515 car. lus) :
- `CT_Raccourci` : laisser vide (sinon `81003/81004` si collision).
- `CT_NumPayeur`, `CG_NumPrinc`, `CO_No`, `CT_NumCentrale` : laisser vides/0 (chaque valeur non vide déclenche un contrôle d'existence `81035/81058/81248/81263`).

**Idempotence obligatoire** : `IF NOT EXISTS (SELECT 1 FROM F_COMPTET WHERE CT_Num=?) INSERT...` (pré-check `SELECT CT_Num,CT_Type`), puis `UPDATE contacts SET synced_sage=1, sage_ct_num=?, updated_at=... WHERE id=?`. Retour `{success, ct_num}` / `{success:False, error}` avec `log_sync("sage_contact",...)`.

### 1.2 Gate d'acceptation Phase 1

```bash
sqlcmd -S "172.31.16.1,1433" -U sageUser -P "MotDePasseSecurise123!" -C -Q "USE BIJOU; SELECT CT_Num,CT_Type FROM F_COMPTET WHERE CT_Num LIKE 'CLI-%' OR CT_Num LIKE 'FOU-%' ORDER BY CT_Num;"
```
Attendu : 10 `CLI-*` en `CT_Type=0` + 3 `FOU-*` en `CT_Type=1`. Rejouer le push : 0 doublon (`IF NOT EXISTS`), `contacts.synced_sage=1` partout.

---

## Phase 2 — `writer.py` : écrire le `CT_Num` Sage, pas le code local

### 2.1 Spécification
Dans `write_invoice_to_sage` (`writer.py:51-52`), résoudre avant l'`INSERT` :
```python
tiers_val = resolve_sage_tiers(invoice.get("tiers_code", ""))
# resolve = SELECT sage_ct_num FROM contacts WHERE code=? AND synced_sage=1
# si absent : appel write_contact_to_sage (ensure inline) puis relecture
# si échec : return {"success": False, "error": "tiers '<code>' non créé dans Sage"} (skip explicite, JAMAIS de remap silencieux vers COMPTOIR)
```
`DO_Tiers` reçoit donc toujours un `CT_Num` existant `CT_Type=0` (ventes).

### 2.2 Gate d'acceptation Phase 2
`grep DO_Tiers writer.py` → plus aucune écriture directe de `tiers_code` brut ; test unitaire : facture `tiers_code=CLI-001` → `DO_Tiers='CLI-001'` avec `F_COMPTET.CLI-001` présent.

---

## Phase 3 — Ordre de sync : contacts AVANT factures

### 3.1 Spécification
`full_sync()` (`bidirectional.py:210-214`) devient :
```python
cert_result = certify_pending_pos_invoices()
contact_result = push_contacts_to_sage()   # NOUVEAU : SELECT * FROM contacts WHERE synced_sage=0
push_result = push_to_sage()               # inchangé, bénéficie de la Phase 2 (ensure inline en filet)
pull_result = pull_from_sage()
```
`push_contacts_to_sage()` itère `contacts WHERE synced_sage=0`, appelle `write_contact_to_sage`, comptabilise `{pushed_contacts, errors}` séparément dans `_sync_stats` et dans le log `Sync terminee`.

### 3.2 Gate d'acceptation Phase 3
Log : ligne `Push contacts Sage: X ecrits, 0 erreurs` AVANT `Push Sage: ...` ; redémarrage serveur obligatoire après édition (le process garde l'ancien code en mémoire).

---

## Phase 4 — Vérifications déjà en place (ne pas régresser)

- `writer.py:60-64` `DO_CodeTaxe1=code_entete` + `80-84` `DL_CodeTaxe1=_code_taxe(...)` (conservés : nécessaires dès que les tiers passent).
- `_code_taxe` : `18→C18, 5→C05, 0→C00, 20→C20`.
- Code TVA figé à `18` (`database.py:844`, `writer.py:92`) — Option A, `C18/C05` en base, historique `C20` intact.
- `avoir_type=7`, devise `CAST`, NIU→`CT_Identifiant`, `tp_cols` : OK.

---

## Phase 5 — Validation de bout en bout (gates mesurables)

Exécuter dans l'ordre, **stop si un gate échoue** :

```bash
# G1 — tiers créés (Phase 1)
sqlcmd -S "172.31.16.1,1433" -U sageUser -P "MotDePasseSecurise123!" -C -Q "USE BIJOU; SELECT COUNT(*) FROM F_COMPTET WHERE CT_Num LIKE 'CLI-%' AND CT_Type=0;"
# attendu : 10

# G2 — push sans 82019 (Phases 2+3, après redémarrage serveur)
grep -c "82019" data/output.log   # ne doit plus augmenter après l'heure du redémarrage
grep "Push Sage" data/output.log | tail -n 3   # attendu : "Push Sage: N ecrites, 0 erreurs"

# G3 — FA000026 écrite avec code taxe (vérif métier)
sqlcmd -S "172.31.16.1,1433" -U sageUser -P "MotDePasseSecurise123!" -C -Q "USE BIJOU; SELECT DO_Piece,DO_Tiers,DO_CodeTaxe1,DO_TotalTTC FROM F_DOCENTETE WHERE DO_Piece='FA000026'; SELECT DL_Ligne,DL_CodeTaxe1,DL_Taxe1,DL_MontantTTC FROM F_DOCLIGNE WHERE DO_Piece='FA000026' ORDER BY DL_Ligne;"
# attendu : 1 entête (DO_Tiers=CLI-001, DO_CodeTaxe1=C18) + lignes DL_CodeTaxe1=C18

# G4 — miroir SQLite
# SELECT synced_sage, sage_piece FROM invoices WHERE numero='FA000026'; → 1, 'FA000026'
```

**Succès indiscutable = G1+G2+G3+G4 verts + `Push Sage: 0 erreur` sur 2 cycles complets consécutifs.**

---

## Phase 6 — Rollback (si un gate échoue)

```sql
-- Annuler les pièces test écrites (ordre : lignes puis entête, triggers DEL actifs)
USE BIJOU;
DELETE FROM F_DOCLIGNE WHERE DO_Piece='FA000026';
DELETE FROM F_DOCENTETE WHERE DO_Piece='FA000026';
-- Annuler les tiers créés UNIQUEMENT s'ils n'ont aucune pièce (vérifier d'abord)
SELECT CT_Num FROM F_COMPTET WHERE CT_Num LIKE 'CLI-%' AND CT_Num NOT IN (SELECT DO_Tiers FROM F_DOCENTETE);
-- DELETE FROM F_COMPTET WHERE CT_Num IN (...liste vérifiée...);
```
```sql
-- Côté SQLite : UPDATE contacts SET synced_sage=0, sage_ct_num='' WHERE code LIKE 'CLI-%';
-- Redémarrer le serveur pour revenir au comportement précédent.
```
Ne JAMAIS supprimer `C18/C05/D18/D05` de `F_TAXE` (référentiel partagé, historique `C20` intact de toute façon).

---

## Risques résiduels (connus, non bloquants)

1. `CT_Type=2` préexistants (`CAMET`, `CHEVALIER`, `BRILLE`) : si une facture les référence un jour → `82019` immédiat. Mitigation : Phase 2 ne mappe que du `CT_Type=0` ; alerter si `tiers_code` résout vers `CT_Type!=0`.
2. Noms/adresses > limites Sage : tronqués par spec (log warning à chaque troncature).
3. Concurrence : `_db_lock` existant côté `database.py` conservé ; `push_contacts` réutilise `get_cursor()` Sage (pool `pyodbc`, `autocommit=True`).
