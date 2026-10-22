# Proposition 04 — Réparer la synchronisation des articles

> **Priorité** : 🟠 P1 — le POS vend un référentiel de démonstration.
> **Fichier** : `app/sync/article_sync.py` (+ script de nettoyage)
> **Tests** : `tests/test_article_sync.py` (nouveau)
> **Réf. rapport** : `00-rapport-analyse-logs.md` §3.4

---

## 1. Symptôme

```
[t-connector.db] INFO: Articles Sage charges: 47322
[t-connector.articles] ERROR: Sync articles: ecriture SQLite echouee:
  Incorrect number of bindings supplied.
  The current statement uses 12, and there are 11 supplied.
[t-connector.sync] INFO: Sync articles: {'ok': False, 'error': '...',
  'inserted': 0, 'updated': 0, 'total': 47322}
```

29 occurrences dans `error.log`. En base client :

```text
products                       -> 129 lignes (jeu de démonstration, synced_sage = 0)
articles lus depuis Sage       -> 47 322
articles insérés               -> 0
```

Conséquence : le POS, la recherche article et les lignes de facture locale travaillent sur **129 articles fictifs** (`VEN-MUR-001`, « Ventilateur mural 30cm »…) au lieu du catalogue réel.

## 2. Cause racine

`app/sync/article_sync.py:66-72` :

```python
        to_update, to_insert = [], []
        for n in normed:
            ref = n[0]
            if ref in existing:
                to_update.append((n[1], n[2], n[3], n[4], n[5], n[6], n[7], n[8], n[9], n[10], ref, existing[ref]))
            else:
                to_insert.append((ref,) + n[1:])      # <-- 11 valeurs
```

`n` contient 11 champs :

```python
normed.append((ref, barcode, designation, famille, nature, prix_vente,
               prix_achat, tva_code, unite, stock, est_actif))
```

Or l'`INSERT` (`article_sync.py:85-92`) attend **12 paramètres** :

```sql
INSERT OR IGNORE INTO products (
    ref, barcode, designation, famille, nature,
    prix_vente, prix_achat, tva_code, unite, stock_reel,
    est_actif, synced_sage, sage_ar_ref
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
--        1  2  3  4  5  6  7  8  9 10 11     12
```

`(ref,) + n[1:]` fournit `ref` + 10 champs = **11** : la valeur de `sage_ar_ref` manque. La branche `UPDATE`, elle, est correcte (12 valeurs).

## 3. Correctif

**Bloc à remplacer** (`article_sync.py:72`) :

```python
                to_insert.append((ref,) + n[1:])
```

**Bloc corrigé** :

```python
                # 12e param : sage_ar_ref (reference article Sage). Sans lui,
                # executemany leve "uses 12, and there are 11 supplied" et
                # aucun article n'est insere (audit 2026-10-21).
                to_insert.append((ref,) + n[1:] + (ref,))
```

C'est le correctif minimal. Deux améliorations recommandées dans le même lot :

### 3.1 Journaliser le détail de l'erreur de binding

`sync_articles_from_sage()` retourne `{"ok": False, "error": ...}` sans indice de ligne. Ajouter le nombre d'articles préparés :

```python
    except Exception as e:
        logger.error("Sync articles: ecriture SQLite echouee: %s (prepares=%d)",
                     e, len(articles))
        return {"ok": False, "error": str(e), "inserted": 0, "updated": 0,
                "total": len(articles), "prepared": len(articles)}
```

### 3.2 Nettoyer les articles de démonstration après une sync réussie

Tant que la sync échouait, `main.py:52-58` seedait 129 produits de démo. Après réparation, ils resteront mélangés au catalogue réel. Script ponctuel :

`scripts/purge_seed_products.py`

```python
"""Supprime les articles de demonstration (synced_sage = 0) absents de Sage.

A lancer UNE FOIS apres une synchronisation articles reussie.
Refuse de supprimer un article reference par une ligne de facture.

Usage :
    python scripts/purge_seed_products.py           # dry-run
    python scripts/purge_seed_products.py --apply   # suppression
"""
import argparse
from app.storage.db import get_cursor

_WHERE = """
    synced_sage = 0
    AND id NOT IN (SELECT DISTINCT product_id FROM invoice_lines
                   WHERE product_id IS NOT NULL)
    AND id NOT IN (SELECT DISTINCT product_id FROM pos_ticket_lines
                   WHERE product_id IS NOT NULL)
"""

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM products WHERE " + _WHERE)
        print("Articles de demonstration supprimables:", cur.fetchone()["c"])
        if not args.apply:
            print("Dry-run : aucune suppression. Relancer avec --apply.")
        else:
            cur.execute("DELETE FROM products WHERE " + _WHERE)
            print("Articles de demonstration supprimes:", cur.rowcount)
```

> Ne pas supprimer sur le simple critère `synced_sage = 0` : une facture locale ou un ticket POS peut référencer un article créé à la main.

### 3.3 Garde-fou au démarrage

`main.py:52-58` seed les produits **si la table est vide**. Après réparation elle ne l'est plus, donc le garde-fou suffit. Ajouter tout de même un log explicite quand la sync échoue :

```python
            result = sync_articles_from_sage()
            if not result.get("ok"):
                logger.error("Referentiel article NON synchronise: %s", result.get("error"))
            else:
                logger.info("Sync articles Sage au demarrage: %s", result)
```

## 4. Tests

`tests/test_article_sync.py` :

```python
import pytest

from app.storage import db
from app.sync import article_sync


def test_insert_12_parametres(monkeypatch):
    db.init_database()
    articles = [{
        "ref": "ART-001", "designation": "Article test", "barcode": "123",
        "famille": "Divers", "nature": 0, "prix_vente": 1000,
        "prix_achat": 700, "tva_code": "18", "unite": "U",
        "stock": 5, "est_actif": 1,
    }]
    monkeypatch.setattr(article_sync, "fetch_articles", lambda: articles)
    res = article_sync.sync_articles_from_sage()
    assert res["ok"] is True, res
    assert res["inserted"] == 1
    with db.get_cursor() as cur:
        row = cur.execute("SELECT * FROM products WHERE ref = 'ART-001'").fetchone()
    assert row["sage_ar_ref"] == "ART-001"
    assert row["synced_sage"] == 1


def test_update_existant(monkeypatch):
    db.init_database()
    # deux passages : le 2e doit mettre a jour, pas dupliquer
    ...
```

Cas à couvrir :

- insertion (12 bindings) ;
- mise à jour d'un article existant (branche `to_update`) ;
- article sans `ref` ignoré ;
- `tva_code` vide → `18`.

## 5. Vérification sur la copie client

```python
import sqlite3
c = sqlite3.connect('file:data/tconnector.db?mode=ro', uri=True)
print(c.execute("select count(*) from products").fetchone())          # 129 avant
print(c.execute("select count(*) from products where synced_sage=1").fetchone())  # 0 avant
```

Après déploiement et une sync :

- `products` ≈ **47 322** lignes ;
- `synced_sage = 1` pour tous ;
- `sage_ar_ref` renseigné ;
- les 129 articles de démo supprimés par `purge_seed_products.py` (s'ils ne sont pas référencés).

## 6. Critères d'acceptation

- [ ] `Sync articles: {'ok': True, 'inserted': 47322, ...}` dans les logs.
- [ ] 0 `Incorrect number of bindings` dans `error.log`.
- [ ] Le POS affiche les articles Sage réels (prix, TVA, stock).
- [ ] Aucun article de démonstration restant.

## 7. Risques / rollback

| Risque | Parade |
|---|---|
| 47 322 `INSERT` en une transaction : lenteur | `executemany` déjà utilisé ; ~1–3 s observés côté Sage (lecture plus longue que l'écriture) |
| Doublons ref/code-barre | `ref` est `UNIQUE`, `INSERT OR IGNORE` protège |
| Suppression d'un article utilisé | le script vérifie `invoice_lines.product_id` |
| `sage_ar_ref` doit rester distinct de `ref` | ici Sage utilise `AR_Ref` comme référence : les deux colonnes portent la même valeur, comme le fait déjà l'`UPDATE` |

**Rollback** : revert d'une ligne dans `article_sync.py`.
