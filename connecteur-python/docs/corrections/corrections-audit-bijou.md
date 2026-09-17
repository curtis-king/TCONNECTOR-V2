# Corrections issues — audit base BIJOU (Sage 100)

Audit réalisé le 17/09/2026 sur la base `BIJOU` (serveur 10.42.0.2).
Chaque entrée suit le format : **référence du problème** → **bloc de code à remplacer** → **bloc de code corrigé**.

---

## ISSUE-ODBC-18 — Driver `SQL Server` introuvable sur Linux

### Problème

Erreur au démarrage :
`('01000', "[01000] [unixODBC][Driver Manager]Can't open lib 'SQL Server' : file not found (0) (SQLDriverConnect)")`

La machine n'a que `ODBC Driver 18 for SQL Server` d'installé (`odbcinst -q -d`), mais la liste `preferred` de
`_build_conn_string` ne contient que les versions 17/13/11 (`SQL Server` renvoyé en fallback = driver Windows uniquement).

### Fichier

`app/integration/sage/database.py` — fonction `_build_conn_string`

### Bloc à remplacer

```python
def _build_conn_string(cfg):
    import pyodbc as _pyodbc
    available = [d.strip() for d in _pyodbc.drivers()]
    preferred = [
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "ODBC Driver 11 for SQL Server",
        "SQL Server",
        "SQL Server Native Client 11.0",
    ]
    driver = next((d for d in preferred if d in available), "SQL Server")

    parts = [
        "DRIVER={{{}}}".format(driver),
        "SERVER={}".format(cfg["server"]),
        "DATABASE={}".format(cfg["database"]),
    ]
    if "\\" not in cfg["server"]:
        parts.append("PORT={}".format(cfg.get("port", 1433)))
    if cfg.get("trusted_connection"):
        parts.append("Trusted_Connection=yes")
    else:
        parts.append("UID={}".format(cfg.get("user", "")))
        parts.append("PWD={}".format(cfg.get("password", "")))
    if cfg.get("encrypt"):
        parts.append("Encrypt=yes")
    else:
        parts.append("Encrypt=no")
    if cfg.get("trust_server_certificate"):
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts)
```

### Bloc corrigé

```python
def _build_conn_string(cfg):
    import pyodbc as _pyodbc
    available = [d.strip() for d in _pyodbc.drivers()]
    preferred = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
        "ODBC Driver 11 for SQL Server",
        "SQL Server",
        "SQL Server Native Client 11.0",
    ]
    configured = (cfg.get("driver") or "").strip()
    driver = configured if configured in available else next((d for d in preferred if d in available), "SQL Server")

    parts = [
        "DRIVER={{{}}}".format(driver),
        "SERVER={}".format(cfg["server"]),
        "DATABASE={}".format(cfg["database"]),
    ]
    if "\\" not in cfg["server"]:
        parts.append("PORT={}".format(cfg.get("port", 1433)))
    if cfg.get("trusted_connection"):
        parts.append("Trusted_Connection=yes")
    else:
        parts.append("UID={}".format(cfg.get("user", "")))
        parts.append("PWD={}".format(cfg.get("password", "")))
    if cfg.get("encrypt"):
        parts.append("Encrypt=yes")
    else:
        parts.append("Encrypt=no")
    if cfg.get("trust_server_certificate"):
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts)
```

### Fichier complémentaire

`app/config/manager.py` — section `db` de `_defaults()` : ajouter la clé optionnelle `"driver": ""`.

Bloc à remplacer :

```python
        "db": {
            "server": "",
            "port": 1433,
            "database": "",
```

Bloc corrigé :

```python
        "db": {
            "server": "",
            "port": 1433,
            "database": "",
            "driver": "",
```

### Vérification

- `odbcinst -q -d` doit retourner le driver ; relancer le connecteur → connexion SQL Server établie.

---

## ISSUE-DEVISE-ENTIER — `DO_Devise` est un entier (0/5), pas un code devise

### Problème

Dans la base BIJOU, `F_DOCENTETE.DO_Devise` est `smallint` (valeurs distinctes `0`, `5`) et la table de référence
`F_DEVISE` n'existe pas. La requête renvoie donc `"0"` / `"5"` comme `devise` au lieu d'un code ISO (`XAF`…).
Vitrine SFEC, champ "devise".

### Fichier

`app/integration/sage/database.py`

### 1. Bloc à remplacer (sélecteur de la colonne devise, fonction `fetch_sales_invoices`)

```python
    devise_select = "ISNULL(d.DO_Devise, 'XAF') AS devise" if "DO_DEVISE" in doc_cols else "'XAF' AS devise"
```

### 1. Bloc corrigé

```python
    devise_select = "ISNULL(CAST(d.DO_Devise AS VARCHAR), '0') AS devise" if "DO_DEVISE" in doc_cols else "'0' AS devise"
```

### 2. Bloc à remplacer (ajout d'un helper de mapping, après la fonction `_safe_float`)

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

### 2. Bloc corrigé

```python
def _safe_float(val, default=0.0):
    """Convert value to float, handling French comma decimals."""
    if val is None:
        return default
    try:
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return default


_DEFAULT_DEVISE_MAP = {"0": "XAF", "5": "XAF"}


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

### 3. Bloc à remplacer (construction de chaque facture, boucle dans `fetch_sales_invoices`)

```python
            inv["montant_restant"] = round(_safe_float(row[columns.index("montant_restant")]), 2)
            try:
```

### 3. Bloc corrigé

```python
            inv["montant_restant"] = round(_safe_float(row[columns.index("montant_restant")]), 2)
            inv["devise"] = _map_devise(inv.get("devise", "0"))
            try:
```

> Note : `_fetch_sales_invoices_fallback` et `fetch_purchase_invoices` n'ont pas de colonne `devise` → `_map_devise(inv.get("devise", "0"))` retombe sur la devise par défaut. L'écriture (writer.py) n'insère pas `DO_Devise` → valeur 0 en base = devise société, aucun changement nécessaire.

---

## ISSUE-NIU-CT-IDENTIFIANT — Colonne fiscale tiers : `CT_Identifiant` en secours

### Problème

`CT_NIU`, `CT_NIF`, `CT_IdentifiantFiscal` n'existent **pas** dans `F_COMPTET`. La colonne réellement remplie pour
l'identifiant fiscal est `CT_Identifiant` (23/23 tiers). Sans correctif, `recipient_niu` / `numero_fiscal` restent vides
(SFEC).

### Fichier

`app/integration/sage/database.py`

### 1. Bloc à remplacer (factures de vente — `fetch_sales_invoices`)

```python
        niu_select = "ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else "'' AS recipient_niu"
```

### 1. Bloc corrigé

```python
        niu_select = ("ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else
                      "ISNULL(c.CT_Identifiant, '') AS recipient_niu" if "CT_IDENTIFIANT" in tp_cols else
                      "'' AS recipient_niu")
```

### 2. Bloc à remplacer (factures d'achat — `fetch_purchase_invoices`)

```python
        niu_select = "ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else "'' AS recipient_niu"
```

### 2. Bloc corrigé

```python
        niu_select = ("ISNULL(c.CT_NIU, '') AS recipient_niu" if "CT_NIU" in tp_cols else
                      "ISNULL(c.CT_Identifiant, '') AS recipient_niu" if "CT_IDENTIFIANT" in tp_cols else
                      "'' AS recipient_niu")
```

### 3. Bloc à remplacer (contacts — `fetch_contacts`)

```python
    niu_select = "ISNULL(c.CT_NIU, '') AS numero_fiscal" if "CT_NIU" in tp_cols else "'' AS numero_fiscal"
```

### 3. Bloc corrigé

```python
    niu_select = ("ISNULL(c.CT_NIU, '') AS numero_fiscal" if "CT_NIU" in tp_cols else
                  "ISNULL(c.CT_Identifiant, '') AS numero_fiscal" if "CT_IDENTIFIANT" in tp_cols else
                  "'' AS numero_fiscal")
```

---

## ISSUE-TVA-DEFAUT-20 — Taux de TVA par défaut 18 alors que la base utilise 20

### Problème

La base BIJOU n'utilise que les taux `0` et `20` (`DL_Taxe1` distincts). Les fallbacks codés en dur à `18` produisent
des lignes TVA erronées.

### Fichier

`app/integration/sage/database.py` (`fetch_doc_lines`) et `app/integration/sage/writer.py` (`write_invoice_to_sage`)

### Database — bloc à remplacer

```python
                        except (ValueError, TypeError):
                            line[key] = 0.0 if key != "taux_tva" else 18.0
```

### Database — bloc corrigé

```python
                        except (ValueError, TypeError):
                            line[key] = 0.0 if key != "taux_tva" else 20.0
```

### Writer — bloc à remplacer

```python
                    ligne.get("taux_tva", 18),
```

### Writer — bloc corrigé

```python
                    ligne.get("taux_tva", 20),
```

> Note : si le taux doit rester paramétrable, préférer une clé `db.default_tva` (traitée en `cfg.get`), valeur 20 pour BIJOU.

---

## ISSUE-AUDIT-SEC — Le script d'audit ne garde que le dernier check par section

### Problème

Dans `/tmp/bijou_audit.py`, `sec(n)` réinitialise la section (`R[...][n] = []`) à **chaque app** de `add(...)` : seuls
les derniers checks par section apparaissent dans le JSON (ex. `distribution_domaine_type` perdu).

### Fichier

`/tmp/bijou_audit.py` (à copier dans `connecteur-python/scripts/bijou_audit.py`)

### Bloc à remplacer

```python
def sec(n): R.setdefault("sections", {})[n] = []; return R["sections"][n]
```

### Bloc corrigé

```python
def sec(n):
    R.setdefault("sections", {}).setdefault(n, [])
    return R["sections"][n]
```

### Vérification

Relancer le script : chaque section doit maintenant contenir **tous** ses checks (inventaire, schema, documents,
matching, tiers, tva, articles, lignes, coherence, sfec).

---

## Récapitulatif des fichiers modifiés

| Fichier | Issues |
| --- | --- |
| `app/integration/sage/database.py` | ISSUE-ODBC-18, ISSUE-DEVISE-ENTIER, ISSUE-NIU-CT-IDENTIFIANT, ISSUE-TVA-DEFAUT-20 |
| `app/integration/sage/writer.py` | ISSUE-TVA-DEFAUT-20 |
| `app/config/manager.py` | ISSUE-ODBC-18 (clé `db.driver`), ISSUE-TVA-DEFAUT-20 (option `db.default_tva`) |
| `scripts/bijou_audit.py` | ISSUE-AUDIT-SEC |

## Notes de données (audit BIJOU 17/09/2026)

- Types de documents domaine 0 : **6 = facture de vente** (FA00005-08), **7 = avoir** (FA00001-03) → suppositions du code validées.
- 29 pièces domaine 0, toutes statut 2 (A COMPTABILISER), aucune ligne orpheline, aucun écart entête/lignes.
- Colonnes SFEC déjà créées dans `F_DOCENTETE` (8) mais 0/29 certifiées.
- `F_TAXE` = `TA_Code`, `TA_Taux`, `TA_Intitule` (compatible `fetch_tax_rates`).