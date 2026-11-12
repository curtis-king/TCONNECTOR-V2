# Rapport d'analyse — logs client du 21/10/2026

> **Sources analysées**
> - `data/error.log` (23 189 lignes, 4,0 Mo)
> - `data/output.log`, `output.log.1`, `output.log.2` (89 586 lignes, ~14,5 Mo — fenêtre 17:15 → 18:36)
> - `data/tconnector.db` (190 Mo, copie client du 22/10 09:18)
> - `data/sites.json`, `data/sfec-certified.json`, `connecteur-python/config.json`
>
> **Périmètre** : flux Sage BIJOU → connecteur → certification SFEC → écriture Sage → impression.
> **Statut** : diagnostic terminé, correctifs non appliqués (aucun fichier de code modifié).

---

## 1. Synthèse exécutive

Le connecteur ne certifie **plus rien** : la dernière passe d'auto-certification se solde par `0 certifiée, 33 à 194 erreurs`, et les mêmes factures sont rejouées toutes les 30 secondes. Trois causes racines se cumulent, plus quatre chantiers secondaires.

| Priorité | Problème | Volume mesuré | Effet client |
|---|---|---|---|
| 🔴 P0-1 | Centimes et taxes additionnelles absents du payload SFEC | **14 866** rejets, **191** factures distinctes, **177** débloquées | Aucune certification |
| 🔴 P0-2 | `reset_stuck_en_cours()` efface les statuts `ERREUR` | **64** passes en 1 h 30, 33–194 erreurs/passe | Retentatives infinies, 409, logs qui explosent |
| 🔴 P0-3 | Une seule connexion SQLite partagée par tous les threads | 13 erreurs fatales, **3 × 500 sur `/login`** | Dashboard inaccessible par intermittence |
| 🟠 P1-1 | Sync articles : `12` bindings pour `11` valeurs | **0 / 47 322** articles insérés | Référentiel article figé (129 lignes de démo) |
| 🟠 P1-2 | Avoirs : références en texte libre, parent non certifié | **~41 000** warnings | Avoirs jamais certifiés |
| 🟠 P1-3 | 12 factures à qualifier fiscalement | 11 × `TTC=HT`, 1 × ratio `1,1825` ; 2 introuvables | Rejet définitif, décision client requise |
| 🟠 P1-4 | Miroir SQLite sans TVA ni type de pièce | **177 040** factures `TTC ≠ HT` avec `TVA = 0` | KPI/dashboard faux |
| 🟡 P2-1 | ODBC Sage instable, 409, verrous Sage | 704 timeouts (surtout 17–21/09), 22 × 409 | Certification non enregistrée dans Sage |
| 🟡 P2-2 | Impression & exploitation | 4 PDF bloqués dans `WATCH/IN`, `dry_run: true` | Aucune impression papier |

**Impact commercial direct** : les 4 PDF déposés dans `data/WATCH/IN` (dont `25VFA3142`, `25VFA3103`, `25VFA3104`) sont en attente de certification depuis le 21/10 et ne sortent pas.

---

## 2. Chronologie du 21/10/2026

| Heure | Événement |
|---|---|
| 17:01 → 18:36 | 64 passes d'auto-certification, `0 certifiée` à chaque fois |
| 18:02 | 33 factures rejouées (cluster centimes) |
| 18:13 | 3 exceptions `SystemError` sur `POST /login` |
| 18:27 → 18:30 | Écriture Sage impossible pour `25VFA3142` (`Cet élément est en cours d'utilisation`) |
| 18:31 | Passe complète : `0 certifiees, 33 erreurs` |
| 18:33 | `Statuts bloques remis a NULL: 16 factures` puis nouveau cycle immédiat |
| 18:36 | Dernière ligne : `Pull Sage: 0 nouvelles, 211417 mises a jour` |

Le cycle est **auto-entretenu** : chaque passe remet à `NULL` les statuts `ERREUR`, ce qui rend les factures à nouveau éligibles à la passe suivante.

---

## 3. Diagnostic détaillé

### 3.1 🔴 P0-1 — Centimes et taxes additionnelles absents du payload SFEC

**Preuve.** Sur les 191 factures distinctes rejetées, l'écart `total_amount − attendu` se répartit ainsi :

| Cluster | Nb | Constat | Formule | Traitement |
|---|---|---|---|---|
| **A** | **162** | écart = 5 % de la TVA collectée | `TTC = HT + TVA + 5 % × TVA` | **certifiable** : `additional_cent_tax` |
| B | 11 | `TTC = HT` | aucune TVA appliquée | **à qualifier** : exonération ou oubli de TVA |
| C | 15 | `TTC = HT × 1,219` | `18 % + 3 % + 0,9 %` | **certifiable** : `additional_taxes` (3 %, code à confirmer) |
| D | 1 | `TTC = HT × 1,1825` | mixte 18/5 + centimes, ou 18 % + 0,25 % | **à confirmer** par le calcul par lignes |
| — | 2 | HT introuvable dans le miroir local | — | à rapatrier |

Exemples du cluster A (factures réelles du client) :

| Facture | HT | TTC Sage | Attendu par le code | Écart | 5 % de la TVA |
|---|---|---|---|---|---|
| 25VFB3811 | 235 987 | 280 589 | 278 464,66 | 2 123,34 | 2 123,88 |
| 25VFA3065 | 31 960 | 38 001 | 37 712,80 | 288,20 | 287,64 |
| 25VFA3104 | 61 965 | 73 676 | 73 118,70 | 557,30 | 557,30 |
| 25VFA3082 | 2 103 | 2 501 | 2 481,54 | 18,46 | 18,93 |

157 factures ont un ratio `écart / TVA` compris entre **4,877 % et 5,556 %** (médiane 5,000 %) : c'est le centime additionnel, arrondi ligne par ligne par Sage.

**Le cluster C n'est pas une anomalie.** La facture `26FAZ07297`, certifiée SFEC le 25/08/2026, porte trois lignes : 18 % (2 536 981), 0,9 % (126 849 = 5 % de la TVA) et 3 % (422 830). L'API SFEC expose `additional_taxes` pour cette taxe de 3 %, et `additional_cent_tax` pour le centime. Le cluster C est donc **certifiable** (voir `01` §2.2-2.3). Le cluster D (ratio 1,1825) est probablement un mixte 18 %/5 % ; le calcul par lignes tranchera.

**Cause.** `app/integration/sfec/endpoints.py` (`db_invoice_to_sfec`) :
- `item.total_amount` reprend `DL_MontantTTC`, qui **inclut déjà** le centime et les taxes additionnelles ;
- `additional_cent_tax` est lu dans `invoice["additional_cent_tax"]`, toujours `0` pour une facture d'origine Sage ;
- `total_amount` = somme des TTC de ligne, sans ventilation du centime ni des taxes additionnelles ;
- `items[].subtotal` est le HT brut et `net_amount` le HT, alors que SFEC attend le HT **net de remise** et le **TTC** de ligne.

La règle des centimes est pourtant déjà appliquée côté factures locales (`recalc_invoice_totals` → `compute_cent_tax`, décret n° 2023-1737 art. 22, taux `cent_tax_rate = 5` en base). Seul le chemin **Sage → SFEC** l'a oubliée.

**Piège à éviter.** Ajouter simplement le centime dans `additional_cent_tax` **compterait deux fois**, puisque les TTC de ligne Sage le contiennent déjà. Il faut :
1. `item.total_amount = net_ht + tva` (pas `DL_MontantTTC`) ;
2. `additional_cent_tax = compute_cent_tax(TVA 18 + TVA 5)` ;
3. `additional_taxes` = résidu `TTC Sage − HT − TVA − centimes` (3 %, 0,25 %…) ;
4. `total_amount = HT + TVA + centimes + taxes additionnelles` ;
5. contrôler le résultat contre le TTC d'en-tête Sage.

➡️ **Proposition** : [`01-proposition-centimes-additionnels.md`](01-proposition-centimes-additionnels.md)

---

### 3.2 🔴 P0-2 — Tempête de retentatives

**Preuves.**
- `error.log` : **14 866** `Incoherence totaux`, alors qu'il n'y a que **191** factures distinctes → ~78 tentatives par facture.
- `output.log` : 64 lignes `Auto-certification terminee`, avec `0 certifiees, 33 erreurs` puis `0 certifiees, 194 erreurs` en alternance.
- `Statuts bloques remis a NULL: 16 factures` à 18:33:11, immédiatement suivi d'un nouveau cycle.
- Même facture tentée **6 fois en 400 ms** (18:02:02 → 18:02:03), signature de deux passes concurrentes.

**Causes.**
1. `app/integration/sage/database.py:1716` — `reset_stuck_en_cours()` remet à `NULL` **`EN_COURS` ET `ERREUR`** à chaque passe.
2. `app/sync/engine.py:509` — `auto_certify_invoices()` n'a **aucun verrou** : `sync_all()` relâche `_lock` avant d'appeler la certification, donc une nouvelle sync peut démarrer pendant que la certification tourne.
3. `_certify_single_worker` (`engine.py:451`) marque `ERREUR` même pour un **422 de validation locale**, qui est pourtant non recouvrable.
4. `to_certify` n'est pas dédoublonné.

➡️ **Proposition** : [`02-proposition-tempete-retentatives.md`](02-proposition-tempete-retentatives.md)

---

### 3.3 🔴 P0-3 — Connexion SQLite partagée entre threads

**Preuves.**
```
SystemError: error return without exception set
  app/storage/db.py:68 in get_cursor -> conn.commit()
  app/web/auth/user_auth.py:111 / 142
  app/web/routes/auth.py:48  (POST /login)          -> 3 × HTTP 500

[t-connector.sync.bi] ERROR: cannot commit - no transaction is active    (4)
[t-connector.sync.bi] ERROR: bad parameter or other API misuse          (3)
[t-connector.auth]   WARNING: Audit log impossible: error return without exception set
```

**Cause.** `app/storage/db.py:28-70` conserve **un seul `_conn` global** (`check_same_thread=False`) utilisé simultanément par :
- Flask (`main.py:111`, `threaded=True`),
- le scheduler de sync,
- les **5 workers** `ThreadPoolExecutor` de certification (`engine.py:620`),
- le BI-sync, le watcher d'impression, le contrôle de connectivité.

Le module `sqlite3` n'est pas réentrant sur une même connexion : commits concurrents → `SystemError`, transactions perdues, connexion « empoisonnée » (d'où le 500 au login).

➡️ **Proposition** : [`03-proposition-sqlite-concurrence.md`](03-proposition-sqlite-concurrence.md)

---

### 3.4 🟠 P1-1 — Sync articles cassée

```
[t-connector.articles] ERROR: Sync articles: ecriture SQLite echouee:
  Incorrect number of bindings supplied. The current statement uses 12, and there are 11 supplied.
[t-connector.sync] INFO: Sync articles: {'ok': False, ..., 'inserted': 0, 'updated': 0, 'total': 47322}
```

`app/sync/article_sync.py:72` construit `to_insert` avec **11 valeurs** alors que l'`INSERT` (ligne 85-92) attend **12 `?`** : `sage_ar_ref` manque. Résultat : `0 / 47 322` articles insérés, le POS continue de vendre les **129 articles de démonstration** (`products` en base).

➡️ **Proposition** : [`04-proposition-sync-articles.md`](04-proposition-sync-articles.md)

---

### 3.5 🟠 P1-2 — Avoirs et pièces négatives

**Preuves.**
- `output.log*` : **41 178** lignes `Avoir sans reference facture d'origine` ou `Facture d'origine '…' non certifiee`.
- Références rencontrées : `25VBA2818` (60×), `N°BC78799`, `KANE`, `UNI PRIX`, `SNC`, `SIBCL25080`, `N°PO2500422`… → **texte libre**, pas un numéro de pièce.
- Miroir local : **5 585** factures négatives (jusqu'à `-9 701 042`) mais **0** `type_doc = 'avoir'` et **0** `sage_type = 7` (le `INSERT` de `writer.py:410` code `sage_type = 6` en dur).
- **36** factures à `TTC = 0`.

**Conséquences.**
- un avoir dont la facture d'origine n'est pas encore certifiée est marqué `ERREUR` puis rejoué à l'infini, au lieu d'**attendre** le parent ;
- une facture négative (DO_Type=6) part en `salesInvoice` avec `total_amount < 0` → rejet SFEC ;
- une facture à 0 n'est pas certifiable.

➡️ **Proposition** : [`05-proposition-avoirs-references.md`](05-proposition-avoirs-references.md)

---

### 3.6 🟠 P1-3 — Factures à qualifier fiscalement dans Sage

Après le correctif `01`, **12** factures restent non reconstructibles automatiquement :

| Cluster | Nb | Factures (échantillon) | Décision attendue |
|---|---|---|---|
| `TTC = HT` | 11 | `25VFA3117`, `25VFA3118`, `25VFA3125`, `25VFA3126`, `25VFA3133`, `25VFA3136`, `25VFB3817`, `25VFR0137`, `25VFA3140`, `9FAZ00138`, `25VFB3807` | Exonération réelle ou TVA oubliée dans Sage ? |
| `TTC = HT × 1,1825` | 1 | `25VFA3134` | Mixte 18 %/5 % + centimes, ou taxe de 0,25 % ? Le calcul par lignes tranche |
| HT introuvable dans le miroir | 2 | — | Rapatrier puis recalculer |

Les **15 factures à 21,9 %** ne figurent plus ici : la taxe de 3 % est une **taxe additionnelle légitime**, représentable via `additional_taxes` (vérifié sur la facture certifiée `26FAZ07297`, voir `01` §2.2-2.3).

À noter : chaque passe d'auto-certification affiche `33 erreurs` car elle inclut aussi des avoirs et des doublons de numéro ; le décompte **distinct** est de 12 (+2 à rapatrier).

Ces factures ne doivent **pas** être « absorbées » silencieusement : c'est une décision fiscale. Elles doivent être marquées `A_SURVEILLER` avec un motif lisible, et corrigées dans Sage ou certifiées hors connecteur.

➡️ **Proposition** : [`06-proposition-factures-anomalies.md`](06-proposition-factures-anomalies.md)

---

### 3.7 🟠 P1-4 — Le miroir SQLite perd la TVA et le type de pièce

`sync_sage_invoice_to_sqlite()` (`writer.py:354`) ne persiste que `montant_ht` et `montant_ttc`. Vérification en base :

```text
source='sage' AND montant_tva <> 0          ->        0
source='sage' AND TTC <> HT                 ->  177 040
montant_ht_brut <> 0                        ->        2
type_doc = 'avoir'                          ->        0
total_tax_t_amount <> 0                     ->        0
sage_type                                   -> 211 423 × 6, 3 × 7
```

Le `INSERT` code d'ailleurs `sage_type = 6` en dur (`writer.py:410`). Les écrans et KPI basés sur le miroir affichent donc une TVA nulle ; l'impression certifiée, elle, lit les données SFEC (pas d'impact fiscal, mais un risque de confusion en cas de repli local).

➡️ **Proposition** : [`07-proposition-miroir-local.md`](07-proposition-miroir-local.md)

---

### 3.8 🟡 P2-1 — ODBC Sage, 409 et verrous

| Message | Occurrences | Commentaire |
|---|---|---|
| `Login timeout expired` | **704** | 53 le 17/09, 632 le 21/09, **19 le 21/10** → résiduel mais réel |
| `Ce serveur SQL n'existe pas ou son accès…` | 10 | réseau 10.42.0.47 instable |
| `Configuration base de donnees incomplete` | 11 + 3 | config/pool pas prêt (démarrage) |
| `Cet élément est en cours d'utilisation` | 8 | verrou Sage pendant l'écriture |
| `SFEC API error 409 invoice already exists` | **22** | certification créée mais non écrite dans Sage |

Le 409 est correctement détecté (`_handle_already_certified`), mais la lecture SFEC échoue ou le verrou Sage empêche l'écriture ; la facture est alors marquée `ERREUR` puis rejouée.

➡️ **Proposition** : [`08-proposition-sage-odbc-409.md`](08-proposition-sage-odbc-409.md)

---

### 3.9 🟡 P2-2 — Impression, sécurité, exploitation

- `data/WATCH/IN` contient **4 PDF** (17:44 → 18:29) ; `OUT`, `DONE` et `QUARANTINE` sont vides ; `processed_pdf_prints` et `invoice_print_log` sont vides.
- `data/sites.json` : **`dry_run: true`** → même certifiées, les factures ne sortiraient pas.
- Comptes admin par défaut : `admin@example.com`, `admin@admin.com`, `admin@test.com`.
- `config.json` : `session_secret: CHANGE-ME-IN-PRODUCTION`, dashboard `0.0.0.0:3000`, clé API SFEC et mot de passe SQL en clair.
- `tconnector.db` = 190 Mo, `sync_log` = 211 458 lignes ; rotation des logs **5 Mo × 3**, saturée en 1 h 30 par les rejets en boucle (3 fichiers `output.log*`).

➡️ **Proposition** : [`09-proposition-impression-securite-exploitation.md`](09-proposition-impression-securite-exploitation.md)

---

## 4. Méthode de vérification (reproductible)

> `sqlite3` CLI n'est pas installé sur ce poste ; les contrôles ci-dessous utilisent Python.

Compter les rejets et les factures distinctes :

```python
import re
pat = re.compile(r'Certification echouee pour ([A-Za-z0-9]+):.*?total_amount=(-?[\d.]+) mais attendu~=(-?[\d.]+)')
seen = {}
for line in open('data/error.log', encoding='utf-8', errors='replace'):
    m = pat.search(line)
    if m:
        seen.setdefault(m.group(1), (float(m.group(2)), float(m.group(3))))
print(len(seen), 'factures distinctes')
```

Classer les écarts :

```python
import sqlite3
c = sqlite3.connect('file:data/tconnector.db?mode=ro', uri=True)
for numero, (actual, expected) in seen.items():
    row = c.execute('select montant_ht from invoices where numero=?', (numero,)).fetchone()
    if not row or not row[0]:
        continue
    print(numero, round((actual - expected) / row[0], 4))
```

Vérifier le miroir :

```python
print(c.execute("select count(*) from invoices where source='sage' and montant_tva<>0").fetchone())
print(c.execute("select count(*) from invoices where source='sage' and abs(montant_ttc-montant_ht)>0.01").fetchone())
```

---

## 5. Plan de remédiation

### Lot 1 — Hotfix « déblocage » (≈ 1 journée)

| # | Action | Fichier | Impact attendu |
|---|---|---|---|
| 1 | Centimes + taxes additionnelles + contrôle TTC Sage | `app/integration/sfec/endpoints.py` | **~177 factures certifiées** |
| 2 | `reset_stuck_en_cours` sans `ERREUR`, verrou + dédoublonnage | `database.py`, `engine.py` | Fin de la tempête, logs ÷ 50 |
| 3 | 400/422 → `A_SURVEILLER` | `engine.py` | Plus de rejeu infini |
| 4 | Connexions SQLite thread-local | `app/storage/db.py` | Plus de 500 au login |
| 5 | Bindings articles | `app/sync/article_sync.py` | 47 322 articles synchronisés |

### Lot 2 — Avoirs & anomalies (≈ 2–3 jours)

6. Résolution des références d'avoirs + attente du parent (`05`).
7. Factures négatives → `creditNote` en valeurs absolues ; factures à 0 exclues (`05`).
8. Statut `A_SURVEILLER` + motif + bouton « Relancer » ; export des 12 anomalies (`06`).
9. 409 : relecture SFEC + file d'écriture différée (`08`).

### Lot 3 — Fiabilisation (≈ 2 jours)

10. Miroir local complet + script de backfill (`07`).
11. Retry/backoff ODBC, classification des erreurs (`08`).
12. Rotation des logs, rate-limit, sécurité, go-live impression (`09`).

### Séquence de déploiement

1. Lot 1 → redémarrage → laisser tourner **1 cycle**.
2. Vérifier : `Auto-certification terminee: ~177 certifiees, ~12 a verifier, 2 a rapatrier`.
3. Lot 2 → vérifier la disparition des ~41 000 warnings avoirs.
4. Lot 3 → `dry_run: false` → vérifier l'écriture Sage et la sortie des 4 PDF (débloqués dès que leur facture est certifiée, pas par `dry_run`).

### Critères d'acceptation globaux

- [ ] `error.log` < 100 lignes / heure en régime nominal.
- [ ] 0 `Incoherence totaux` sur les factures des clusters A **et C**.
- [ ] `products` ≥ 47 000 lignes, `synced_sage = 1`.
- [ ] 0 `SystemError` / `cannot commit` / 500 sur `/login` sur 24 h.
- [ ] `SELECT count(*) FROM invoices WHERE sfec_statut='A_SURVEILLER'` = liste d'anomalies **explicite**, pas un fourre-tout.
- [ ] Les 4 PDF de `WATCH/IN` sortent (ou sont acquittés) après passage en `dry_run: false`.

---

## 6. Décisions à prendre (client / DGI)

1. **Taxe de 3 %** : **tranchée** — c'est une taxe additionnelle légitime, portée par `additional_taxes` (facture certifiée `26FAZ07297`). Reste à confirmer le libellé officiel (`TAR`, `TAX_MUNI`…) pour le comptable.
2. **11 factures `TTC = HT`** : exonération réelle ou oubli de TVA dans Sage ?
3. **Avoirs** : peut-on obtenir le numéro de la facture d'origine autrement que par `DO_Ref` (texte libre) ? Sinon, prévoir une liaison manuelle.
4. **5 585 factures négatives** : à traiter en avoirs SFEC ou à exclure du périmètre ?
5. **Déploiement** : nouveau build ou hotfix ciblé sur les 5 fichiers du Lot 1 ?
6. **`dry_run: true`** : est-ce volontaire pour la phase de test ?

---

## 7. Index des propositions

| Doc | Sujet | Priorité |
|---|---|---|
| [01](01-proposition-centimes-additionnels.md) | Centimes et taxes additionnelles dans le payload SFEC | 🔴 P0 |
| [02](02-proposition-tempete-retentatives.md) | Statuts `ERREUR`, verrou, dédoublonnage, backoff | 🔴 P0 |
| [03](03-proposition-sqlite-concurrence.md) | Connexions SQLite thread-local | 🔴 P0 |
| [04](04-proposition-sync-articles.md) | Bindings `INSERT` articles | 🟠 P1 |
| [05](05-proposition-avoirs-references.md) | Avoirs, références, pièces négatives | 🟠 P1 |
| [06](06-proposition-factures-anomalies.md) | 12 factures à qualifier (TTC = HT, ratio 1,1825) | 🟠 P1 |
| [07](07-proposition-miroir-local.md) | Miroir SQLite (TVA, type, ventilation) | 🟠 P1 |
| [08](08-proposition-sage-odbc-409.md) | ODBC Sage, 409, verrous | 🟡 P2 |
| [09](09-proposition-impression-securite-exploitation.md) | Impression, sécurité, logs, maintenance | 🟡 P2 |
