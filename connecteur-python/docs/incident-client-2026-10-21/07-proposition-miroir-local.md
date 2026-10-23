# Proposition 07 — Miroir SQLite : TVA, type de pièce et ventilation

> **Priorité** : 🟠 P1 — KPI et dashboard faux, aucune visibilité sur les avoirs.
> **Fichiers** : `app/sync/bidirectional.py`, `app/integration/sage/writer.py`, `scripts/backfill_mirror_totals.py` (nouveau)
> **Tests** : `tests/test_mirror_totals.py` (nouveau)
> **Réf. rapport** : `00-rapport-analyse-logs.md` §3.7

---

## 1. Constat en base client

```text
source='sage' AND montant_tva <> 0                    ->        0
source='sage' AND |montant_ttc - montant_ht| > 0.01    ->  177 040
montant_ht_brut <> 0                                  ->        2
type_doc = 'avoir'                                    ->        0
total_tax_t_amount <> 0                               ->        0
additional_cent_tax <> 0                              ->        0
sage_type = 6                                         ->  211 423
sage_type = 7                                         ->        3
```

Autrement dit : **211 426 factures** dans le miroir, **0 avec une TVA**, **0 avoir**, et le domaine/type Sage sont figés à `0/6`.

## 2. Cause racine

### 2.1 `_update_from_sage()` n'écrit que trois montants

`app/sync/bidirectional.py:150-179` :

```python
        cur.execute("""
            UPDATE invoices SET
                montant_ht = ?, montant_ttc = ?, montant_restant = ?,
                synced_sage = 1, updated_at = datetime('now')
            WHERE id = ?
        """, (
            sage_inv.get("montant_ht", 0),
            sage_inv.get("montant_ttc", 0),
            sage_inv.get("montant_restant", 0),
            invoice_id
        ))
```

C'est **le chemin principal** : `pull_invoices_from_sage()` appelle `_update_from_sage()` pour toute facture déjà connue (`bidirectional.py:134-136`). `sync_sage_invoice_to_sqlite()` ne sert qu'aux nouvelles.

### 2.2 L'`INSERT` code `sage_domaine`/`sage_type` en dur

`app/integration/sage/writer.py:402-423` :

```python
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sage', ?, ?, ?, ?, 1, 0, 6, ?)
```

`0` = domaine, `6` = type « facture » : **tous** les documents sont enregistrés comme des ventes du domaine 0, y compris les avoirs (`DO_Type = 7`).

### 2.3 La branche `UPDATE` de `sync_sage_invoice_to_sqlite` est aussi incomplète

`writer.py:364-392` ne met à jour que `ht`, `ttc`, `restant`, les coordonnées tiers et les champs SFEC.

### 2.4 Le champ `montant_tva` de Sage inclut le centime

`database.py:841` : `inv["montant_tva"] = montant_ttc - montant_ht`. Ce n'est **pas** la TVA déclarée : le delta contient la TVA **et** le centime additionnel (et la taxe 3 % éventuelle). Pour le miroir, il faut ventiler depuis les **lignes** :

```python
# database.py:1065 — montant_tva de ligne = HT * DL_Taxe1 / 100
ISNULL(ROUND(l.DL_MontantHT * l.DL_Taxe1 / 100, 2), 0) AS montant_tva
```

Les lignes sont déjà chargées dans `sage_inv["lignes"]` (`database.py:857`).

## 3. Correctif

### 3.1 Helper de ventilation — `app/sync/bidirectional.py`

```python
from app.storage.db import compute_cent_tax


def _mirror_fields(sage_inv):
    """Champs du miroir local derives de la facture Sage.

    Le miroir sert au dashboard et a l'impression de repli : il doit porter
    la ventilation fiscale, pas seulement HT/TTC (audit 2026-10-21 :
    177 040 factures a TVA=0 alors que TTC <> HT).
    """
    lignes = sage_inv.get("lignes") or []
    montant_ht = round(_safe_float(sage_inv.get("montant_ht")), 2)
    montant_ttc = round(_safe_float(sage_inv.get("montant_ttc")), 2)

    tva_t = tva_r = exempt = brut = 0.0
    for ligne in lignes:
        taux = round(_safe_float(ligne.get("taux_tva")))
        tva = _safe_float(ligne.get("montant_tva"))
        if taux >= 18:
            tva_t += tva
        elif taux > 0:
            tva_r += tva
        else:
            exempt += _safe_float(ligne.get("montant_ht"))
        brut += _safe_float(ligne.get("quantite")) * _safe_float(ligne.get("prix_unitaire"))

    if lignes:
        montant_tva = round(tva_t + tva_r, 2)
        montant_ht_brut = round(brut, 2)
        remise = round(max(0.0, montant_ht_brut - montant_ht), 2)
    else:
        # Repli : TTC = HT + TVA + 5 % de TVA  =>  TVA = (TTC - HT) / 1,05
        delta = round(montant_ttc - montant_ht, 2)
        montant_tva = round(delta / 1.05, 2)
        montant_ht_brut = montant_ht
        remise = 0.0

    type_doc = "avoir" if (
        sage_inv.get("type_doc") == "avoir" or montant_ttc < 0
    ) else "vente"

    return {
        "montant_tva": montant_tva,
        "montant_ht_brut": montant_ht_brut,
        "type_doc": type_doc,
        "total_tax_t_amount": round(tva_t if lignes else montant_tva, 2),
        "total_tax_r_amount": round(tva_r, 2),
        "total_exempt_amount": round(exempt, 2),
        "discount_amount": remise,
        "total_line_discount_amount": remise,
        "additional_cent_tax": compute_cent_tax(montant_tva),
        "electronic_stamp_duty": 0.0,
        "sage_domaine": 0,
        "sage_type": int(_safe_float(sage_inv.get("do_type_raw"), 6)),
        "sage_piece": sage_inv.get("numero", ""),
    }
```

> `_safe_float` est déjà importé dans `bidirectional.py` depuis `app.integration.sage.database` ; sinon l'ajouter à l'import.

> Le miroir ne stocke pas encore les **taxes additionnelles** : pour une facture à 1,219, `additional_cent_tax` reste 5 % de la TVA et le 3 % n'est pas persisté. Le backfill §3.4 les exporte en CSV. Ajouter une colonne `additional_taxes_json` si le dashboard doit les afficher — ce n'est pas nécessaire à la certification (le payload lit Sage).

### 3.2 `_update_from_sage()` — `bidirectional.py:169-179`

**Bloc à remplacer**

```python
        cur.execute("""
            UPDATE invoices SET
                montant_ht = ?, montant_ttc = ?, montant_restant = ?,
                synced_sage = 1, updated_at = datetime('now')
            WHERE id = ?
        """, (
            sage_inv.get("montant_ht", 0),
            sage_inv.get("montant_ttc", 0),
            sage_inv.get("montant_restant", 0),
            invoice_id
        ))
```

**Bloc corrigé**

```python
        m = _mirror_fields(sage_inv)
        cur.execute("""
            UPDATE invoices SET
                montant_ht = ?, montant_tva = ?, montant_ttc = ?, montant_restant = ?,
                montant_ht_brut = ?,
                total_tax_t_amount = ?, total_tax_r_amount = ?, total_exempt_amount = ?,
                discount_amount = ?, total_line_discount_amount = ?,
                additional_cent_tax = ?, electronic_stamp_duty = ?,
                type_doc = ?, sage_domaine = ?, sage_type = ?, sage_piece = ?,
                synced_sage = 1, updated_at = datetime('now')
            WHERE id = ?
        """, (
            sage_inv.get("montant_ht", 0), m["montant_tva"],
            sage_inv.get("montant_ttc", 0), sage_inv.get("montant_restant", 0),
            m["montant_ht_brut"],
            m["total_tax_t_amount"], m["total_tax_r_amount"], m["total_exempt_amount"],
            m["discount_amount"], m["total_line_discount_amount"],
            m["additional_cent_tax"], m["electronic_stamp_duty"],
            m["type_doc"], m["sage_domaine"], m["sage_type"], m["sage_piece"],
            invoice_id
        ))
```

### 3.3 `sync_sage_invoice_to_sqlite()` — `writer.py:402-423`

Remplacer les deux `VALUES (..., 1, 0, 6, ?)` par des paramètres :

```python
            cur.execute("""
                INSERT INTO invoices (
                    numero, date_facture, tiers_code, reference,
                    tiers_nom, tiers_niu, tiers_adresse, tiers_telephone, tiers_email,
                    montant_ht, montant_tva, montant_ttc, montant_restant,
                    montant_ht_brut, total_tax_t_amount, total_tax_r_amount,
                    total_exempt_amount, discount_amount, total_line_discount_amount,
                    additional_cent_tax, type_doc,
                    statut, valide, source, sfec_statut, sfec_num_certif,
                    sfec_signature, sfec_qr_code, synced_sage,
                    sage_domaine, sage_type, sage_piece
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, 'sage', ?, ?, ?, ?, 1, ?, ?, ?)
            """, (
                numero, sage_inv.get("date_facture", ""),
                sage_inv.get("tiers_code", ""), sage_inv.get("reference", ""),
                sage_inv.get("tiers_nom", ""), sage_inv.get("tiers_niu", ""),
                sage_inv.get("tiers_adresse", ""), sage_inv.get("tiers_telephone", ""),
                sage_inv.get("tiers_email", ""),
                sage_inv.get("montant_ht", 0), m["montant_tva"],
                sage_inv.get("montant_ttc", 0), sage_inv.get("montant_restant", 0),
                m["montant_ht_brut"], m["total_tax_t_amount"], m["total_tax_r_amount"],
                m["total_exempt_amount"], m["discount_amount"],
                m["total_line_discount_amount"], m["additional_cent_tax"],
                m["type_doc"], statut, 1 if sage_inv.get("valide") else 0,
                sage_inv.get("sfec_statut", ""), sage_inv.get("sfec_num_certif", ""),
                sage_inv.get("sfec_signature", ""), sage_inv.get("sfec_qr_code", ""),
                m["sage_domaine"], m["sage_type"], m["sage_piece"],
            ))
```

> `m = _mirror_fields(sage_inv)` à calculer en tête de fonction, après le contrôle `numero`.

### 3.4 Backfill de l'historique

Les 211 426 lignes existantes ne seront corrigées qu'au prochain pull (et seulement pour les factures re-remontées). Script :

`scripts/backfill_mirror_totals.py`

```python
"""Recalcule montant_tva / centimes / type_doc du miroir local.

Le miroir historique n'a pas de lignes : on classe par ratio TTC/HT.
  - 1,000  : aucune TVA
  - 1,0525 : TVA 5 % + centimes
  - 1,189  : TVA 18 % + centimes
  - 1,219  : TVA 18 % + centimes + taxe additionnelle 3 %
  - autre  : exporte en CSV, aucune ecriture (ne pas deviner)

Le centime se calcule toujours sur la TVA (5 %), jamais sur le HT.
A lancer UNE FOIS sur une copie de sauvegarde, puis verifier les KPI.
"""
import csv
from app.config.manager import load_config
from app.storage.db import get_cursor, compute_cent_tax

RATIOS = {
    1.0: 0.0,
    1.0525: 0.05,
    1.189: 0.18,
    1.219: 0.18,
}


def _match_ratio(ratio):
    for ref, taux in RATIOS.items():
        if abs(ratio - ref) <= 0.001:
            return taux
    return None


def main(csv_path="backfill_anomalies.csv"):
    load_config()
    suspects = []
    updated = 0
    with get_cursor() as cur:
        cur.execute("""
            SELECT id, numero, montant_ht, montant_ttc
            FROM invoices
            WHERE source = 'sage'
              AND (montant_tva IS NULL OR montant_tva = 0)
              AND ABS(montant_ttc - montant_ht) > 0.01
        """)
        rows = cur.fetchall()

    for row in rows:
        ht = row["montant_ht"] or 0
        ttc = row["montant_ttc"] or 0
        ratio = round(ttc / ht, 4) if ht else None
        taux = _match_ratio(ratio) if ratio is not None else None

        if taux is None:
            suspects.append({
                "numero": row["numero"], "ht": ht, "ttc": ttc,
                "ratio": ratio, "motif": "ratio inconnu",
            })
            continue

        tva = round(ht * taux, 2)
        centimes = compute_cent_tax(tva)
        extra = round(ttc - ht - tva - centimes, 2)
        type_doc = "avoir" if ttc < 0 else "vente"

        if abs(extra) > 0.01:
            suspects.append({
                "numero": row["numero"], "ht": ht, "ttc": ttc,
                "ratio": ratio,
                "motif": "taxe additionnelle {} FCFA non stockee".format(extra),
            })

        with get_cursor() as cur:
            cur.execute("""
                UPDATE invoices SET
                    montant_tva = ?, additional_cent_tax = ?, total_tax_t_amount = ?,
                    type_doc = ?, updated_at = datetime('now')
                WHERE id = ?
            """, (tva, centimes, tva, type_doc, row["id"]))
        updated += 1

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["numero", "ht", "ttc", "ratio", "motif"])
        w.writeheader()
        w.writerows(suspects)

    print("Lignes corrigees: {} — a verifier: {}".format(updated, len(suspects)))


if __name__ == "__main__":
    main()
```

> Pour un avoir, le backfill produit des montants **négatifs** (comme HT/TTC). La mise en valeurs absolues se fait au moment du payload `creditNote` (proposition 05 §3.4), pas dans le miroir.

## 4. Tests

1. `_mirror_fields` sur une facture 18 % : HT 10 000, TTC 11 890 → `montant_tva=1 800`, `centimes=90`, `type_doc='vente'`.
2. Facture 18 % + 5 % : `total_tax_t=1 800`, `total_tax_r=500`, centimes 115.
3. Avoir `DO_Type=7` : `type_doc='avoir'`, `sage_type=7`.
4. Facture négative `DO_Type=6` : `type_doc='avoir'`, montants inchangés (négatifs).
5. Sans lignes : repli `tva = (ttc-ht)/1,05`.
6. `_update_from_sage` met bien à jour `montant_tva` (cursor factice).
7. Backfill : HT 10 000, TTC 12 190 → `montant_tva=1 800`, `centimes=90`, les 300 FCFA de taxe à 3 % sont signalés, **pas** absorbés dans les centimes.

## 5. Vérification après déploiement

```python
import sqlite3
c = sqlite3.connect('file:data/tconnector.db?mode=ro', uri=True)
print(c.execute("select count(*) from invoices where source='sage' and montant_tva<>0").fetchone())
print(c.execute("select count(*) from invoices where type_doc='avoir'").fetchone())
print(c.execute("select count(*) from invoices where abs(montant_ttc-montant_ht)>0.01 "
                "and abs((montant_ttc-montant_ht) - montant_tva - additional_cent_tax)>1").fetchone())
```

Attendu : TVA non nulle sur ~177 000 lignes, ~5 585 avoirs, écart résiduel < 1 FCFA sur la grande majorité.

## 6. Critères d'acceptation

- [ ] Les KPI du dashboard (CA HT, TVA collectée, centimes) sont non nuls et cohérents avec Sage.
- [ ] Les avoirs apparaissent dans les listes (`type_doc = 'avoir'`).
- [ ] `SUM(montant_tva) + SUM(additional_cent_tax) ≈ SUM(montant_ttc - montant_ht)` à 1 FCFA près.
- [ ] Le CSV de suspects contient les factures des clusters B/C/D, pas les autres.

## 7. Risques / rollback

| Risque | Parade |
|---|---|
| Backfill imprécis sur les clusters B/C/D | Export CSV + contrôle `ratio`, ne pas certifier depuis le miroir |
| Écriture de 177 000 lignes en une transaction | Lot de 5 000 lignes, ou script avec `commit` par ligne (déjà le cas via `get_cursor`) |
| Colonnes absentes sur une base ancienne | Vérifier `PRAGMA table_info(invoices)` ; `init_database()` fait les `ALTER TABLE` |
| `sage_type` écrasé à tort | Utiliser `do_type_raw` fourni par Sage, jamais une valeur devinée |

**Rollback** : restaurer la copie de `tconnector.db` ; revert de `bidirectional.py` et `writer.py`.

---

## 9. État d'implémentation — 2026-10-22

### 9.1 Ventilation conservatrice dans `_mirror_fields()`

`app/sync/bidirectional.py` contient désormais `_mirror_fields()`, avec trois niveaux de confiance :

1. **Lignes cohérentes avec l'en-tête** : la TVA est la somme des `montant_tva` de lignes, répartie entre `total_tax_t_amount` (taux ≥ 18 %) et `total_tax_r_amount` (taux 5 %), `total_exempt_amount` pour les lignes à 0 %, et `additional_cent_tax = compute_cent_tax(TVA T + TVA R)`. C'est le cas nominal du modèle Congo.
2. **Ratio connu sans lignes** : `1,000`, `1,0525`, `1,189`, `1,219`. Le 3 % éventuel du ratio `1,219` est calculé comme résidu et **signalé**, jamais absorbé dans `additional_cent_tax`.
3. **Ratio inconnu ou lignes incohérentes** : `montant_tva` reprend le delta historique `TTC - HT`, les centimes restent à 0 et un motif `_suspect_reason` est produit. Le backfill n'écrit pas ces lignes.

Deux garde-fous ont été ajoutés par rapport au texte initial :

- les ratios **sans centime** `1,05`, `1,18` et `1,20` (base BIJOU à 20 %, ou 18 % sans centime) sont reconnus pour écrire la TVA exacte **sans inventer de centimes**. Ils restent `_reliable=False` et sont exportés en suspects ;
- les taux non standard (`3 %`, `0,25 %`, etc.) ne sont **ni** de la TVA réduite **ni** des centimes : ils sont exclus de `montant_tva`, de `total_tax_r_amount` et de `compute_cent_tax()`, puis signalés. Le miroir ne stocke pas encore `additional_taxes` ; le 3 % n'est donc pas persisté.

La sémantique des lignes reste celle de la proposition 01 : `montant_ht_brut` = brut avant remise, `discount_amount` / `total_line_discount_amount` = remise de ligne, `montant_tva` = TVA réelle hors centimes et taxes additionnelles.

### 9.2 Chargement des lignes et écriture du miroir

`app/integration/sage/writer.py` :

- `read_invoices_from_sage()` charge désormais les lignes en lot via `fetch_doc_lines_batch(0, keys)`. Sans cela, le pull principal retombait toujours sur le delta `TTC - HT` ;
- `sync_sage_invoice_to_sqlite()` écrit les colonnes fiscales à l'`INSERT` **et** à l'`UPDATE` : `montant_ht_brut`, `total_tax_t_amount`, `total_tax_r_amount`, `total_exempt_amount`, `discount_amount`, `total_line_discount_amount`, `additional_cent_tax`, `electronic_stamp_duty`, `type_doc`, `sage_domaine`, `sage_type`, `sage_piece`.

`app/sync/bidirectional.py::_update_from_sage()` écrit les mêmes colonnes pour les factures déjà présentes, en conservant la logique SFEC existante. Aucune de ces fonctions n'écrit dans Sage : la proposition 07 ne fait que lire Sage et mettre à jour `data/tconnector.db`.

### 9.3 Backfill

`scripts/backfill_mirror_totals.py` :

- **dry-run par défaut** ; `--apply` est obligatoire pour exécuter les `UPDATE` ;
- ne sélectionne que `source='sage' AND (montant_tva IS NULL OR montant_tva = 0) AND ABS(montant_ttc - montant_ht) > 0.01` ;
- classe par ratio (`1,0`, `1,0525`, `1,189`, `1,219`), écrit `montant_tva`, `additional_cent_tax`, `total_tax_t_amount`, `total_tax_r_amount`, `total_exempt_amount` et `type_doc` ;
- pour un avoir, conserve les montants négatifs (la valeur absolue est faite dans le payload `creditNote`) ;
- écrit un CSV UTF-8 BOM des suspects (`numero`, `ht`, `ttc`, `ratio`, `motif`) ; un ratio inconnu n'est **pas** corrigé ;
- appelle `init_database()` avant de travailler, pour garantir les colonnes et la table `settings`.

Le script ne touche jamais à BIJOU ni à Sage. Les données BIJOU restent modifiables depuis l'interface Sage ; le miroir sera recalculé au prochain pull, sans écriture en retour.

### 9.4 Tests

`tests/test_mirror_totals.py` — 19 tests :

1. TVA 18 % + centime ;
2. TVA 18 % + 5 % + centimes ;
3. avoir `DO_Type=7` ;
4. facture négative `DO_Type=6` conservée en négatif ;
5. ligne à 3 % non absorbée dans la TVA réduite ni dans les centimes ;
6. ligne à 0,25 % non absorbée dans la TVA réduite ni dans les centimes ;
7. ratio `1,189` sans lignes ;
8. ratio `1,20` sans centime, TVA écrite sans centime inventé ;
9. ratio `1,219` sans lignes : 300 FCFA de taxe 3 % signalés ;
10. exonération `TTC = HT` ;
11. BIJOU 20 % avec lignes, sans centime ;
12. incohérence en-tête/lignes de type `FA00005` ;
13. lignes à 20 % contredisant un en-tête à `1,189` : la TVA des lignes n'est pas écrasée ;
14. `_update_from_sage()` écrit bien `montant_tva` et `additional_cent_tax` ;
15. `sync_sage_invoice_to_sqlite()` écrit les colonnes fiscales ;
16. backfill `1,219` : 300 FCFA de taxe 3 % signalés, pas absorbés ;
17. backfill ratio inconnu : aucune écriture ;
18. backfill 5 % : alimente `total_tax_r_amount` ;
19. `read_invoices_from_sage()` attache les lignes en réutilisant le même curseur Sage.

Deux assertions de `tests/test_sfec_avoirs.py` ont été mises à jour : un avoir de `-1 000 / -1 189` porte désormais `montant_tva=-180` et `additional_cent_tax=-9` (au lieu de `-189`), conformément à la ventilation TVA réelle.

Résultat local :

```text
pytest tests/test_sfec_avoirs.py tests/test_sfec_centimes.py \
  tests/test_certification_retry.py tests/test_article_sync.py \
  tests/test_sqlite_concurrency.py tests/test_avoirs.py \
  tests/test_billing_security.py tests/test_dashboard_security.py \
  tests/test_directory_security.py tests/test_smoke.py \
  tests/test_bootstrap.py tests/test_sfec_anomalies.py \
  tests/test_mirror_totals.py -q
→ 123 tests OK
```

### 9.5 Critères restant à vérifier en exploitation

- [ ] Exécuter le backfill en dry-run sur une **copie** de `data/tconnector.db`, puis vérifier le CSV de suspects.
- [ ] Après `--apply`, contrôler `SUM(montant_tva) + SUM(additional_cent_tax) ≈ SUM(montant_ttc - montant_ht)` à 1 FCFA près sur la majorité des lignes.
- [ ] Vérifier que les 5 585 avoirs remontent bien avec `type_doc='avoir'`.
- [ ] Vérifier que les factures `TTC = HT` (cluster B) restent hors certification et apparaissent dans le CSV.
- [ ] Vérifier que le 3 % du cluster C n'est jamais dans `additional_cent_tax` ni dans `total_tax_r_amount`.
