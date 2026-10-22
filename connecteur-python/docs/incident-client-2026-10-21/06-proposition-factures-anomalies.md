# Proposition 06 — Qualifier les 12 factures non reconstructibles

> **Priorité** : 🟠 P1 — décision client/DGI requise, ne pas automatiser à l'aveugle.
> **Fichiers** : `app/integration/sfec/endpoints.py` (garde-fou), `app/web/routes/*` (page), `scripts/export_anomalies_sfec.py` (nouveau)
> **Réf. rapport** : `00-rapport-analyse-logs.md` §3.6

---

## 1. Contexte

Après le correctif `01` (centimes + taxes additionnelles), **177 des 191** factures rejetées se recertifient : 162 via `additional_cent_tax`, 15 via `additional_taxes` (3 %). Il en reste **12** dont le TTC Sage n'est pas reconstructible automatiquement, plus **2** absentes du miroir local (non encore rapatriées).

| Cluster | Nb | Écart au modèle | Factures |
|---|---|---|---|
| **B** — `TTC = HT` | 11 | Aucune TVA facturée | `25VFA3117`, `25VFA3118`, `25VFA3125`, `25VFA3126`, `25VFA3133`, `25VFA3136`, `25VFB3817`, `25VFR0137`, `25VFA3140`, `9FAZ00138`, `25VFB3807` |
| **D** — `TTC = HT × 1,1825` | 1 | Mixte 18 %/5 % + centimes, ou 18 % + 0,25 % | `25VFA3134` |
| HT introuvable | 2 | Factures non rapatriées | — |

> Les **15 factures à 21,9 %** (cluster C) ne figurent plus ici : la taxe de 3 % est une taxe additionnelle légitime, portée par `additional_taxes` (voir `01` §2.2-2.3). Les passes affichent parfois `33 erreurs` car le compteur inclut les avoirs et les doublons de numéro ; le décompte **distinct** est de 12.

### Preuves chiffrées

| Facture | HT | TTC Sage | Modèle | Constat |
|---|---|---|---|---|
| 25VFA3118 | 250 000 | 250 000 | 250 000 + 45 000 = 295 000 | **aucune TVA** |
| 25VFA3134 | 150 000 | 177 375 | 18 % seul : 178 350 → **manque 975** | mixte 18/5 possible |

Pour `25VFA3134`, deux lectures :

- **toutes les lignes à 18 %** : `TVA = 27 000`, `centimes = 1 350`, soit `178 350` — le TTC Sage est inférieur de **975 FCFA** → les centimes manquent → `A_SURVEILLER` ;
- **mixte 18 %/5 %** (`≈ 142 857` à 18 % + `≈ 7 143` à 5 %) : `TVA = 26 071,43`, `centimes = 1 303,57`, total `177 375` — **exact** → certifiable sans taxe additionnelle.

Le calcul par lignes (§4 de `01`) tranche automatiquement : résidu nul → certification, résidu négatif → `A_SURVEILLER`.

## 2. Décision de principe

**Ne pas absorber l'écart dans `additional_cent_tax`.** Cela produirait un payload « cohérent » mais fiscalement faux : le centime additionnel deviendrait une variable d'ajustement, et la facture certifiée ne correspondrait plus au détail imprimé.

Deux issues acceptables, au choix du client :

| Option | Description | Conséquence |
|---|---|---|
| **1. Corriger dans Sage** (recommandée) | Cluster B : appliquer la TVA ou émettre un avoir si la vente est exonérée. Cluster D : compléter les centimes si les lignes sont toutes à 18 %. Puis relancer | Factures conformes, aucune exception |
| **2. Certifier hors connecteur** | Émettre/certifier manuellement ces 12 factures via le portail SFEC, et les marquer `DEJA_CERTIFIE` dans Sage | Traçable, mais manuel |
| 3. Exception documentée | Ajouter un flag `allow_nonstandard_tax` **par facture** qui pousse l'écart en `additional_cent_tax` | ⚠️ Déconseillé : faux fiscalement |

Pour le cluster B, si l'exonération est **réelle et documentée**, la certification est possible avec `tax_rate = "0"`, `tax_amount = 0` et `total_exempt_amount = HT` : la facture certifiée reflète alors exactement la facture émise. C'est une décision du comptable, pas du connecteur.

## 3. Garde-fou automatique

Le contrôle `check_sage_totals()` de la proposition 01 (`endpoints.py`) rend le comportement déterministe :

```python
def check_sage_totals(invoice, payload, tolerance=None):
    ...
    # B : aucune TVA facturee
    if sage_ht is not None and abs(sage_ttc - sage_ht) <= tolerance:
        return ["TTC = HT (...) : aucune TVA facturee — exoneration a confirmer"]

    # D : residu non nul apres TVA + centimes + taxes additionnelles
    ecart = round(_safe_float(payload.get("total_amount"), 0) - sage_ttc, 2)
    if abs(ecart) <= tolerance:
        return []
    return ["TTC Sage={} != TTC calcule={} (ecart {} FCFA) : taxe non ventilable — "
            "corriger la facture dans Sage".format(...)]
```

Appelé dans `certify_invoice()` :

```python
    errors = validate_sfec_payload(sfec_req) + check_sage_totals(invoice, sfec_req)
```

Combiné à la proposition 02 (§3.4), un rejet définitif (400 API ou 422 local) est marqué **`A_SURVEILLER`** avec le motif, plus jamais rejoué automatiquement.

> Pour le cluster C, `check_sage_totals` renvoie `[]` : la taxe de 3 % a été extraite dans `additional_taxes` et le total recoupe le TTC Sage. Elle ne doit donc **pas** apparaître en `A_SURVEILLER`.

## 4. Export de la liste pour le client

`scripts/export_anomalies_sfec.py` :

```python
"""Exporte les factures dont le TTC Sage n'est pas ventilable en SFEC.

Lit Sage (memes requetes que l'auto-certification), construit le payload
sans appeler l'API, et ecrit un CSV pour le client.
Usage : python scripts/export_anomalies_sfec.py --out anomalies_sfec.csv
"""
import argparse
import csv

from app.config.manager import load_config
from app.core.logging_setup import setup_logging
from app.integration.sage.database import fetch_sales_invoices
from app.integration.sfec.endpoints import (
    db_invoice_to_sfec, check_sage_totals, validate_sfec_payload,
)


def main(out_path):
    load_config()
    setup_logging()
    rows = []
    for inv in fetch_sales_invoices():
        if inv.get("statut_code") != 2:
            continue
        payload = db_invoice_to_sfec(inv)
        motifs = validate_sfec_payload(payload) + check_sage_totals(inv, payload)
        if not motifs:
            continue
        rows.append({
            "numero": inv.get("numero"),
            "date": inv.get("date_facture"),
            "tiers": inv.get("nom_tiers") or inv.get("tiers_code"),
            "montant_ht": inv.get("montant_ht"),
            "montant_ttc": inv.get("montant_ttc"),
            "ttc_calcule": payload.get("total_amount"),
            "ecart": round(payload.get("total_amount", 0) - (inv.get("montant_ttc") or 0), 2),
            "ratio_ttc_ht": round(
                (inv.get("montant_ttc") or 0) / inv.get("montant_ht"), 4)
                if inv.get("montant_ht") else "",
            "taxes_additionnelles": " | ".join(
                "{} {}% = {}".format(t.get("tax_code"), t.get("tax_rate"), t.get("tax_amount"))
                for t in (payload.get("additional_taxes") or [])
            ),
            "motif": " | ".join(motifs),
        })
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                                ["numero", "date", "tiers", "montant_ht",
                                 "montant_ttc", "ttc_calcule", "ecart",
                                 "ratio_ttc_ht", "taxes_additionnelles", "motif"])
        writer.writeheader()
        writer.writerows(rows)
    print("{} anomalies exportees vers {}".format(len(rows), out_path))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", default="anomalies_sfec.csv")
    main(p.parse_args().out)
```

Le CSV permet au client de trier par `ratio_ttc_ht` (1,000 / 1,1825) et de corriger en lot dans Sage. Les clusters A et C n'y figurent pas.

## 5. Page « À surveiller » avec motif

Aujourd'hui, `mark_to_monitor()` ne stocke pas le motif ; il n'apparaît que dans les logs. Pour que le client puisse agir :

1. Créer une table locale (migration `app/storage/db.py`) :

```sql
CREATE TABLE IF NOT EXISTS sfec_anomalies (
    numero TEXT PRIMARY KEY,
    motif TEXT NOT NULL DEFAULT '',
    detected_at TEXT DEFAULT (datetime('now'))
);
```

2. `mark_to_monitor(invoice_id, reason)` fait un `INSERT ... ON CONFLICT(numero) DO UPDATE` dans `sfec_anomalies` ;
3. La page `/pending` (ou une nouvelle `/a-surveiller`) affiche `numero`, `montant_ttc`, `motif`, `detected_at`, avec un bouton « Relancer » qui remet `SFEC_STATUT = NULL` pour cette pièce.

> La table est locale : elle ne nécessite **aucune** migration du schéma Sage.

## 6. Tests

1. `check_sage_totals` renvoie `[]` pour le cluster A **et** le cluster C (fixtures des montants du tableau de `01`) ; il renvoie un motif pour `25VFA3118` (B).
2. `25VFA3134` avec lignes mixtes 18 %/5 % → `[]` et `additional_taxes` vide ; avec lignes 18 % seules → motif « taxe non ventilable ».
3. `export_anomalies_sfec.py` produit 12 lignes sur un jeu de fixtures (pas 29).
4. La page « À surveiller » liste les motifs et le bouton « Relancer » remet le statut à `NULL`.
5. Un 422 de `check_sage_totals` ne déclenche **aucun** appel HTTP SFEC.

## 7. Critères d'acceptation

- [ ] Après le Lot 1, `SELECT count(*) FROM invoices WHERE sfec_statut='A_SURVEILLER'` ≈ **12** (hors avoirs), puis 14 après rapatriement des 2 manquantes.
- [ ] Le CSV contient les 12 factures avec un motif lisible.
- [ ] Aucune facture des clusters B/D n'est certifiée avec un montant différent du TTC Sage.
- [ ] Aucune facture du cluster C n'apparaît en `A_SURVEILLER`.
- [ ] Zéro `Incoherence totaux` pour ces factures dans les logs (elles ne sont plus tentées).

## 8. Risques / rollback

| Risque | Parade |
|---|---|
| Le client ne corrige jamais les 12 factures | Elles restent visibles en « À surveiller », sans impact sur le flux |
| Le cluster D est mal qualifié (mixte vs 0,25 %) | Tester les lignes réelles en sandbox ; le résidu tranche, ne pas forcer |
| Faux positifs du garde-fou | Tolérance configurable `validation.ttc_sage_tolerance` (défaut 1,0 FCFA) |
| Le code `AUTRE` est refusé par SFEC | Renseigner `validation.additional_tax_codes` (ex. `203`) |

**Rollback** : retirer `check_sage_totals()` de `certify_invoice()`.

---

## 9. État d'implémentation — 2026-10-22

### 9.1 Stockage local du motif

`app/storage/db.py` :

- table `sfec_anomalies(numero PRIMARY KEY, motif, detected_at)` + index sur `detected_at` ;
- `record_sfec_anomaly()` : `INSERT ... ON CONFLICT(numero) DO UPDATE` ;
- `fetch_sfec_anomalies()` : jointure `LEFT JOIN invoices` pour récupérer `montant_ttc`, `date_facture` et `tiers_nom` quand le miroir local connaît la pièce ;
- `get_sfec_anomaly()` / `clear_sfec_anomaly()`.

`mark_to_monitor()` (`app/integration/sage/database.py:1798`) enregistre le motif **avant** de toucher à Sage : une panne SQL Server ne fait donc plus perdre l'information affichée à l'opérateur. Le statut Sage reste inchangé si la table `F_DOCENTETE` n'est pas disponible.

La certification réussie efface l'anomalie dans les deux chemins d'écriture : `write_sfec_to_invoice()` et `mark_certified()`.

### 9.2 Page « À surveiller »

La page `/pending` contient désormais une carte « Factures a surveiller » avec :

- `numero`, `date_facture`, `detected_at`, `motif`, `montant_ttc` ;
- un bouton **Relancer** par ligne.

`POST /api/sfec/relancer` (`app/web/routes/dashboard.py`) :

1. appelle `reset_sfec_monitor(numero)` : seul `A_SURVEILLER` repasse à `NULL`, jamais `ERREUR` ni `CERTIFIE` ;
2. remet aussi `sfec_statut=''` dans le miroir SQLite ;
3. si la facture est dans le cache, lance immédiatement `certify_single()` ; sinon le prochain cycle la reprend.

Un 422 de validation locale reste définitif : il est enregistré `A_SURVEILLER` et n'appelle jamais l'API SFEC. Un 400 API suit la même règle via la proposition 02.

### 9.3 Export client

`scripts/export_anomalies_sfec.py` :

- lit Sage avec `fetch_sales_invoices()` (mêmes requêtes que l'auto-certification) ;
- ne retient que `DO_Statut=2`, hors avoirs et montants négatifs ;
- charge les lignes avec `fetch_doc_lines()` puis reconstruit le payload **sans appel API** ;
- écrit un CSV UTF-8 BOM avec `numero`, `date`, `tiers`, `montant_ht`, `montant_ttc`, `ttc_calcule`, `ecart`, `ratio_ttc_ht`, `taxes_additionnelles`, `motif`.

Les clusters A et C sortent avec `[]` et ne figurent pas dans le CSV. Les clusters B (`TTC = HT`) et D (`1,1825`) restent avec un motif lisible. L'option `--limit` permet d'élargir la lecture au-delà des 1 000 factures récentes.

### 9.4 Tests

`tests/test_sfec_anomalies.py` — 6 tests :

1. export : A et C absents, B et D présents avec motif et écart ;
2. `certify_invoice()` lève 422 avant tout appel HTTP ;
3. `certify_single()` marque `A_SURVEILLER` sur 422 ;
4. `mark_to_monitor()` conserve le motif même sans Sage ;
5. `/pending` affiche le motif et le bouton Relancer ;
6. `/api/sfec/relancer` remet uniquement la pièce ciblée à zéro et relance.

Résultat local : `pytest tests/test_sfec_avoirs.py tests/test_sfec_centimes.py tests/test_certification_retry.py tests/test_article_sync.py tests/test_sqlite_concurrency.py tests/test_avoirs.py tests/test_billing_security.py tests/test_dashboard_security.py tests/test_directory_security.py tests/test_smoke.py tests/test_bootstrap.py tests/test_sfec_anomalies.py -q` → **104 tests OK**.

### 9.5 Critères restant à vérifier en exploitation

- [ ] `SELECT count(*) FROM invoices WHERE sfec_statut='A_SURVEILLER'` ≈ 12 (hors avoirs), puis 14 après rapatriement.
- [ ] Le CSV de production contient bien les 12 factures B/D.
- [ ] Aucun cluster C n'apparaît en `A_SURVEILLER` après une passe complète.
