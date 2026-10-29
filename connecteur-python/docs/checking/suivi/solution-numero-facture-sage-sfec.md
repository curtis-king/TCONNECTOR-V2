# Solution — Numéro de facture envoyé à SFEC

> **Projet** : T-CONNECTOR SFEC (`connecteur-python/`)
> **Dossier** : `docs/checking/suivi/`
> **Statut** : IMPLÉMENTÉ — 2026-10-28
> **Référence** : `probleme-numero-facture-sage-sfec.md`

---

## 1. Principe retenu

`INV-<id>` n'est **pas** l'`invoice_id` envoyé à SFEC : c'est une **clé de
routage interne** vers une ligne SQLite.

Le payload SFEC est construit par `db_invoice_to_sfec()` avec :

```python
"invoice_id": _safe_str(invoice.get("numero"), invoice.get("id", ""))
```

Donc une facture Sage `26FAZ07297` est déjà certifiée sur SFEC sous
`26FAZ07297`, même si l'interface a appelé l'API avec `INV-42`.

La correction consiste donc à rendre explicite le contrat entre la liste
`/billing` et `/api/sfec/certify`, sans casser les autres écrans.

| Origine de la facture | Requête vers `/api/sfec/certify` | Résolution backend | `invoice_id` SFEC |
|---|---|---|---|
| Sage (`source = "sage"`) | `{invoice_id: "26FAZ07297", by: "numero"}` | `get_invoice_by_numero()` | `26FAZ07297` |
| Locale / POS / web | `{invoice_id: "INV-42"}` | `get_invoice(42)` | `numero` de la ligne SQLite |
| Dashboard Sage (cache) | `{invoice_id: "<id_cache_sage>"}` | `certify_single(id)` | `numero` de la pièce Sage |

---

## 2. Modifications implémentées

### 2.1 Frontend — `app/web/templates/billing/list.html`

Le bouton ne construit plus une chaîne JavaScript avec le numéro de facture.
Il transporte les données dans des attributs HTML, échappés séparément :

```javascript
function escAttr(x){
  return esc(x).replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}

var cb = ' <button class="btn btn-sm" '
       + 'data-invoice-id="' + escAttr(t.id) + '" '
       + 'data-invoice-numero="' + escAttr(t.numero) + '" '
       + 'data-invoice-source="' + escAttr(t.source) + '" '
       + 'onclick="certifyBilling(this)">Certifier</button>';
```

La fonction choisit le contrat selon la source :

```javascript
function certifyBilling(btn){
  var id     = btn.getAttribute("data-invoice-id");
  var numero = btn.getAttribute("data-invoice-numero");
  var source = btn.getAttribute("data-invoice-source");
  var payload = (source === "sage" && numero)
      ? {invoice_id: numero, by: "numero"}
      : {invoice_id: "INV-" + id};

  api("POST", "/api/sfec/certify", payload)...
}
```

`escAttr()` évite les guillemets non échappés dans un attribut HTML. Le
`onclick` ne contient plus de valeur métier concaténée.

### 2.2 Backend — `app/web/routes/config.py`

La route accepte trois contrats :

1. **`by = "numero"`** : résolution par numéro de pièce Sage.
2. **`INV-<id>`** : contrat historique des écrans SQLite.
3. **Identifiant du cache Sage** : contrat du dashboard.

```python
if by == "numero":
    inv = invoice_engine.get_invoice_by_numero(raw_id)
    if not inv:
        return jsonify({"success": False,
                        "error": "Facture '{}' non trouvee".format(raw_id)}), 404
    return _certify_local_invoice(inv)

if raw_id.startswith("INV-"):
    try:
        inv_num = int(raw_id.replace("INV-", "", 1))
    except ValueError:
        # Un numéro local peut commencer par INV- (ex. INV-0001).
        inv = invoice_engine.get_invoice_by_numero(raw_id)
        if inv:
            return _certify_local_invoice(inv)
        return jsonify(...), 400
    ...
    return _certify_local_invoice(inv)

return jsonify(certify_single(raw_id))
```

La factorisation `_certify_local_invoice(inv)` conserve les contrôles
existants :

- facture absente → `404` ;
- brouillon → `422` ;
- facture déjà certifiée / synchronisée Sage → `409` ;
- validation locale SFEC → code `422` porté par l'exception ;
- audit `facture.certifiee` après succès.

Le backend ne fait jamais confiance au numéro fourni par le navigateur pour
construire directement le payload : il relit la facture en base, puis
`sqlite_invoice_to_sfec()` utilise le `numero` stocké.

---

## 3. Règles de gestion

| Cas | Action |
|---|---|
| Facture Sage importée | Envoyer le numéro de pièce Sage avec `by: "numero"` |
| Facture locale POS/web | Envoyer `INV-<id SQLite>` (compatibilité) |
| Facture introuvable par numéro | `404`, aucun appel SFEC |
| Facture brouillon | `422`, aucun appel SFEC |
| Facture déjà certifiée | `409`, aucun nouvel appel SFEC |
| Avoir / retour | Même logique : `invoice_id` = numéro de la pièce d'avoir ; le parent reste `reference_invoice_id` |

---

## 4. Tests ajoutés

`tests/test_sfec_certify_number.py` couvre :

- certification d'une facture Sage par `by: "numero"` ;
- résolution correcte de la ligne SQLite et de son `numero` ;
- compatibilité du contrat `INV-<id>` ;
- `404` pour un numéro inconnu ;
- `422` pour un brouillon, sans appel à `certify_sqlite_invoice()`.

Le test de payload existant vérifie déjà que `sqlite_invoice_to_sfec()` produit
`invoice_id = 26FAZ07297` pour une ligne SQLite `id = 42`.

---

## 5. Fichiers modifiés

| Fichier | Modification |
|---|---|
| `app/web/templates/billing/list.html` | Payload explicite, attributs échappés, `certifyBilling(this)` |
| `app/web/routes/config.py` | Résolution `by=numero`, helper `_certify_local_invoice`, conservation de `INV-<id>` |
| `tests/test_sfec_certify_number.py` | Tests de non-régression du contrat |

---

## 6. Points de vigilance

- Les autres écrans (`billing/detail.html`, `directory/sales.html`,
  `billing/form.html`, `dashboard/pending.html`) conservent leurs contrats
  actuels et ne sont pas impactés.
- Le numéro de facture Sage doit être unique dans SQLite. C'est déjà la
  contrainte `numero TEXT UNIQUE NOT NULL`.
- Ne pas réintroduire de concaténation `'"+numero+"'` dans un `onclick` :
  utiliser `data-*` + `escAttr()` ou `JSON.stringify()`.
- `print_hijack` est hors périmètre de ce correctif.
