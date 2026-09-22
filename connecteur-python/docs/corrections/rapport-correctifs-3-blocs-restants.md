# Correctifs — 3 blocs restants `corrections-audit-bijou.md` (BIJOU live `172.31.16.1,1433/sageUser`)

> Base de la vérif : `sqlcmd USE BIJOU; SELECT DO_Domaine,DO_Type,COUNT(*) GROUP BY` → `0:6=4,0:7=3` (`avoir_type=7` OK), `DO_Devise smallint 0:60,3:1,5:1`, `CT_NIU` absent / `CT_Identifiant` présent, `DL_Taxe1 0/20` seul. Fichier source : `C:\Users\tp_m2\Documents\projects\TCONNECTOR-V2\connecteur-python\app\integration\sage\database.py` (copie qui tourne) + `writer.py`.

---

## 1) ISSUE-DEVISE-ENTIER — `DO_Devise` entier → code ISO

### 1a. `database.py:11-18` — ajouter le helper après `_safe_float`

**Bloc à remplacer** (`database.py:11-18`) :
```python
def _safe_float(val, default=0.0):
    """Convert value to float, handling French comma decimals."""
    if val is None:
        return default
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return default
```

**Bloc corrigé** :
```python
def _safe_float(val, default=0.0):
    """Convert value to float, handling French comma decimals."""
    if val is None:
        return default
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return default


_DEFAULT_DEVISE_MAP = {"0": "XAF", "3": "XAF", "5": "XAF"}


def get_devise_map():
    cfg = get_db_config()
    custom = cfg.get("devise_map") or {}
    m = dict(_DEFAULT_DEVISE_MAP)
    m.update({str(k): v for k, v in custom.items()})
    return m


def _map_devise(val):
    m = get_devise_map()
    return m.get(str(val or "").strip(), m.get("0", "XAF"))
```

### 1b. `database.py:581` — `devise_select` vente

**Bloc à remplacer** :
```python
    devise_select = "ISNULL(d.DO_Devise, 'XAF') AS devise" if "DO_DEVISE" in doc_cols else "'XAF' AS devise"
```

**Bloc corrigé** :
```python
    devise_select = "ISNULL(CAST(d.DO_Devise AS VARCHAR), '0') AS devise" if "DO_DEVISE" in doc_cols else "'0' AS devise"
```

### 1c. `database.py:666` — mapping après `montant_restant` (boucle `fetch_sales_invoices`)

**Bloc à remplacer** :
```python
            inv["montant_restant"] = round(_safe_float(row[columns.index("montant_restant")]), 2)
            try:
                inv["statut_code"] = int(_safe_float(row[columns.index("statut_code")]))
```

**Bloc corrigé** :
```python
            inv["montant_restant"] = round(_safe_float(row[columns.index("montant_restant")]), 2)
            inv["devise"] = _map_devise(inv.get("devise", "0"))
            try:
                inv["statut_code"] = int(_safe_float(row[columns.index("statut_code")]))
```

> `fetch_purchase_invoices:895-896` utilise la même variable `devise_select` — la correction 1b s'y applique automatiquement ; si tu veux être explicite refais le même `CAST(...,'0')` à `database.py:895`.

---

## 2) ISSUE-NIU-CT-IDENTIFIANT — `CT_Identifiant` en fallback

### 2a. `database.py:546` — ventes

**Bloc à remplacer** :
```python
        niu_select = "ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else "'' AS recipient_niu"
```

**Bloc corrigé** :
```python
        niu_select = ("ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else
                      "ISNULL(c.CT_Identifiant, '') AS recipient_niu" if "CT_IDENTIFIANT" in tp_cols else
                      "'' AS recipient_niu")
```

### 2b. `database.py:864` — achats

**Bloc à remplacer** (même ligne, `fetch_purchase_invoices`) :
```python
        niu_select = "ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else "'' AS recipient_niu"
```

**Bloc corrigé** :
```python
        niu_select = ("ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else
                      "ISNULL(c.CT_Identifiant, '') AS recipient_niu" if "CT_IDENTIFIANT" in tp_cols else
                      "'' AS recipient_niu")
```

### 2c. `database.py:1067` — contacts

**Bloc à remplacer** :
```python
    niu_select = "ISNULL(c.CT_NIU, '') AS numero_fiscal" if "CT_NIU" in tp_cols else "'' AS numero_fiscal"
```

**Bloc corrigé** :
```python
    niu_select = ("ISNULL(c.CT_NIU, '') AS numero_fiscal" if "CT_NIU" in tp_cols else
                  "ISNULL(c.CT_Identifiant, '') AS numero_fiscal" if "CT_IDENTIFIANT" in tp_cols else
                  "'' AS numero_fiscal")
```

---

## 3) ISSUE-TVA-DEFAUT-20 — fallback `18` → `20` (BIJOU n'a que `0/20`)

### 3a. `database.py:826` — `fetch_doc_lines`

**Bloc à remplacer** :
```python
                        except (ValueError, TypeError):
                            line[key] = 0.0 if key != "taux_tva" else 18.0
```

**Bloc corrigé** :
```python
                        except (ValueError, TypeError):
                            line[key] = 0.0 if key != "taux_tva" else 20.0
```

### 3b. `writer.py:81` — `write_invoice_to_sage` (ligne)

**Bloc à remplacer** (`writer.py:78-82`) :
```python
                    sign * ligne.get("montant_ht", 0),
                    ligne.get("taux_tva", 18),
                    sign * ligne.get("montant_ttc", 0),
```

**Bloc corrigé** :
```python
                    sign * ligne.get("montant_ht", 0),
                    ligne.get("taux_tva", 20),
                    sign * ligne.get("montant_ttc", 0),
```

> Optionnel (spec) : `manager.py` `_defaults()["db"]` ajouter `"default_tva": 20` et remplacer `20.0`/`20` par `cfg.get("default_tva",20)`.

---

## Vérification (lecture seule)

```bash
sqlcmd -S "172.31.16.1,1433" -U sageUser -P "MotDePasseSecurise123!" -C -Q "USE BIJOU; SELECT DO_Domaine,DO_Type,COUNT(*) AS cnt FROM F_DOCENTETE GROUP BY DO_Domaine,DO_Type ORDER BY 1,2;"
# attendu : 0:7=3 (avoirs) à côté de 0:6=4 → confirme avoir_type=7 déjà OK côté database.py:117-122
```

Après collage : redémarrer le connecteur Windows et vérifier `data/output.log` (plus de `KeyError avoir_type`).
