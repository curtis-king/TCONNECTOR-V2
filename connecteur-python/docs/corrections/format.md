# Correction : zéros mal formatés sur l'impression de facture certifiée

## Symptôme

Sur la page d'impression (`/certified/<id>/print`), les montants s'affichent avec
des zéros mal formatés et un style incohérent avec le reste de l'application :

- les totaux créés à partir de données SFEC (Champ "Factures certifiées") s'affichent
  avec des flottants bruts (`0.0 XAF`, `1500.0 XAF`) au lieu d'entiers ;
- les lignes d'articles passent par `{:,.0f}` (arrondi à l'entier, séparateur de
  milliers) alors que les totaux passent bruts via `_esc()` — d'où l'incohérence ;
- toute valeur mal typée (virgule française, chaîne, `None`) fait crasher le format
  `{:,.0f}` avec une `ValueError` (page blanche / 500).

## Cause racine

Dans `app/web/routes/dashboard.py` (route `/certified/<id>/print`,
fonction `print_certified`) :

- les lignes d'articles formatent avec `'{:,.0f}'`, mais les valeurs SFEC peuvent
  être des chaînes (`"0.0"`, `"1,000"`) → `ValueError` ;
- les totaux (`total_ht`, `total_tax18`, `total_tax5`, `total_ttc`, `amount_due`)
  sont passés bruts à `_esc()` → un float SFEC comme `0.0` s'affiche `0.0 XAF`.

Il n'existe aucun formateur monétaire partagé : chaque module fait son propre
formatage inline (et le co-réplique), ce qui produit des zéros mal formés.

## Solution

### 1. Ajouter un formateur monétaire partagé dans `app/core/utils.py`

Placer cette fonction à côté des helpers existants (`safe_float`, `safe_int`,
`safe_str`) :

```python
def fmt_money(val, decimals=0):
    """Formate un montant depuis n'importe quel type (float, str, None).

    - décale la virgule française et arrondit à `decimals` décimales ;
    - ajoute les séparateurs de milliers ;
    - 0.0 → "0", 1500 → "1,500".
    """
    try:
        x = float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        x = 0.0
    return "{:,.{}f}".format(x, decimals)
```

### 2. Utiliser `fmt_money` dans `app/web/routes/dashboard.py`

1. Importer le helper :
   ```python
   from app.core.utils import fmt_money
   ```
2. Dans le rendu des **lignes d'articles**, remplacer le formatage inline par
   `fmt_money(...)` (évite le crash et uniformise) :
   ```python
   item_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td></tr>".format(
       _esc(item.get("designation", "")),
       fmt_money(item.get("quantity", 0)),
       fmt_money(item.get("unit_price", 0)),
       fmt_money(item.get("net_amount", 0), 2),
       _esc(item.get("tax_rate", "0")),
   )
   ```
   (adapter les colonnes exactes à la structure du template `print.html`)
3. Dans le rendu des **totaux**, passer par `fmt_money` au lieu de `_esc` brut :
   ```python
   total_ht=_esc(fmt_money(inv.get("total_ht", 0))),
   total_tax18=_esc(fmt_money(inv.get("total_tax18", 0))),
   total_tax5=_esc(fmt_money(inv.get("total_tax5", 0))),
   total_ttc=_esc(fmt_money(inv.get("total_ttc", 0))),
   amount_due=_esc(fmt_money(inv.get("amount_due", 0))),
   ```

### 3. Vérification

- Relancer l'application et ouvrir `/certified/<id>/print` :
  - `0.0 XAF` → `0 XAF`
  - `1500.0 XAF` → `1,500 XAF`
  - plus aucune page blanche sur valeurs SFEC mal typées.

## Fichiers concernés

- `app/core/utils.py` — ajout de `fmt_money`
- `app/web/routes/dashboard.py` — application aux lignes et totaux de l'impression

## Hors périmètre

- Le template `dashboard/print.html` reste inchangé (il reçoit déjà du HTML formaté).
- Pas de déduplication des `_safe_float` dupliqués dans `app/integration/*` — hors scope.


ancien

item_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td></tr>".format(
            _esc(item.get("designation", "")),
            item.get("quantity", 0),
            fmt_money(item.get("unit_price", 0)),
            _esc(item.get("tax_rate", "0")),
            item.get("net_amount", 0),
        )