# Proposition 01 — Centimes additionnels et taxes additionnelles SFEC

> **Priorité** : 🔴 P0 — débloque **177 factures** sur les 191 rejetées (162 centimes + 15 taxes à 3 %).
> **Fichiers** : `app/integration/sfec/endpoints.py`, `config.json`
> **Tests** : `tests/test_sfec_centimes.py` (nouveau)
> **Réf. rapport** : `00-rapport-analyse-logs.md` §3.1
> **Sources vérifiées** : décret n° 2023-1737 art. 22 · PwC Congo (revue 08/2026) · `docs.sfec.gouv.cg/api/certify` · facture certifiée `26FAZ07297`

---

## 1. Symptôme

À chaque passe d'auto-certification :

```
[t-connector.sfec.endpoints] ERROR: Validation SFEC echouee:
  Incoherence totaux: total_amount=280588.0 mais attendu~=278464.66
  (ecart 2123.34 > tolerance 0.01)
```

**14 866** occurrences pour **191** factures distinctes. Sur **162** d'entre elles, l'écart vaut exactement **5 % de la TVA collectée** :

| Facture | HT | TTC Sage | Attendu | Écart | 5 % TVA |
|---|---|---|---|---|---|
| 25VFB3811 | 235 987 | 280 589 | 278 464,66 | 2 123,34 | 2 123,88 |
| 25VFA3065 | 31 960 | 38 001 | 37 712,80 | 288,20 | 287,64 |
| 25VFA3082 | 2 103 | 2 501 | 2 481,54 | 18,46 | 18,93 |

Médiane du ratio `écart / TVA` = **5,000 %** (min 4,877 %, max 5,556 % — arrondis de Sage).

## 2. Ce que dit la loi et ce que l'API SFEC attend

### 2.1 Le centime additionnel

**Décret n° 2023-1737, art. 22** : *« La base d'imposition des centimes additionnels est le montant de la TVA collectée. Le taux applicable est de cinq pourcent (5 %). »*

PwC (fiche Congo, revue du 07/08/2026) : *« a surtax calculated at the rate of 5% applies to the amount of VAT, which must be invoiced and paid at the same time as the VAT. Therefore, the VAT rate is globally 18.9%. The surtax is not deductible. »*

| TVA | Centimes = 5 % de la TVA | Exprimé en % du HT |
|---|---|---|
| 18 % | 0,9 % du HT | **0,9 %** |
| 5 % | 0,25 % du HT | **0,25 %** |
| 0 % | 0 | 0 % |

Le centime ne se calcule **jamais** directement sur le HT : c'est toujours `5 % × TVA collectée`, toutes tranches confondues.

### 2.2 Les taxes additionnelles

Le champ `additional_taxes` de l'API SFEC est un tableau **au niveau facture** :

| Paramètre | Type | Rôle |
|---|---|---|
| `tax_code` | string | Code de la taxe (ex. `TAR`, `TAX_MUNI`, `203`) |
| `tax_label` | string\|null | Libellé |
| `tax_amount` | number | Montant |
| `tax_rate` | number\|null | Taux en % |

La documentation cite explicitement `TAR`, `ASDI`, `TAX_TOUR`, `TAX_MUNI` et les droits d'accises. **C'est là que va la taxe de 3 %**, pas dans `additional_cent_tax`.

Le champ `total_tax_t_amount` est réservé à la **TVA 18 % seule**, `total_tax_r_amount` à la **TVA 5 % seule**, et `total_tax_amount` à leur somme. Ni les centimes ni les taxes additionnelles n'y sont inclus.

### 2.3 Preuve par une facture certifiée

Facture `26FAZ07297`, certifiée SFEC le 25/08/2026 (n° `f8cbb9144318831bb00295a08355dfc6a8beb4006707cff12f869359585b`) :

| Code | Base | Taux | Montant |
|---|---|---|---|
| 1 | 14 094 340 | 18 % | 2 536 981 |
| 202 | 14 094 340 | **0,9 %** | **126 849** |
| 203 | 14 094 340 | **3 %** | 422 830 |
| | | **Total taxes** | **3 086 660** |

Vérifications :

```
2 536 981 + 126 849 + 422 830 = 3 086 660
14 094 340 + 3 086 660        = 17 181 000   (net à payer)
126 849                        = 5 % × 2 536 981   (centime)
422 830                        = 3 % × 14 094 340   (taxe additionnelle)
```

La facture étant **certifiée**, le 3 % est bien une taxe légitime acceptée par SFEC. **Les 15 factures à 21,9 % ne sont donc pas des anomalies** : elles sont certifiables via `additional_taxes`, sous réserve que le `tax_code` exact (`TAX_MUNI`, `TAR`…) soit accepté — à vérifier en sandbox avant généralisation.

> Le même mécanisme couvre la facture `25VFA3134` (ratio 1,1825) : soit un mixte 18 %/5 % + centimes, soit une taxe de 0,25 %. Le calcul par lignes (§4.3) tranchera ; dans les deux cas le payload reste reconstructible.

## 3. Causes racines (trois, pas une)

`db_invoice_to_sfec()` (`endpoints.py:120`) :

1. **Centimes absents.** `additional_cent_tax` est lu dans `invoice["additional_cent_tax"]` (`endpoints.py:289`), qui vaut **0** pour toute facture d'origine Sage (`writer.py:402-423` ne l'écrit pas).
2. **Taxes additionnelles absentes.** `DL_MontantTTC` contient le 3 %, mais il est mis dans `item.total_amount` sans être déclaré dans `additional_taxes`. `total_tax_t_amount` ne reçoit que `DL_Taxe1` (18 %), d'où un TTC supérieur de **3 % du HT** (≈ 2,5 % du TTC attendu).
3. **Sémantique des items non conforme.** La doc SFEC impose : `items[].subtotal` = HT **après remise**, `items[].net_amount` = **TTC** de la ligne, `items[].total_amount` = TTC. Le code envoie `subtotal` brut et `net_amount` = HT.

La règle des centimes est déjà correcte côté factures locales : `recalc_invoice_totals()` appelle `compute_cent_tax(TVA 18 + TVA 5)` (`app/storage/db.py:587-595`). Seul le chemin **Sage → SFEC** l'a oubliée.

## 4. Correctif

### 4.1 Import

En tête de `app/integration/sfec/endpoints.py` :

```python
from app.storage.db import compute_cent_tax
```

> `compute_cent_tax` lit `settings.cent_tax_rate` (5 en base client) et applique `round(tva * taux / 100, 2)`. Aucun import circulaire.

### 4.2 Sémantique des items — `endpoints.py:161-206`

**Bloc à remplacer** (`endpoints.py:161-167`)

```python
        rate_num, rate_str = _extract_tax_rate(line.get("taux_tva", "18"))
        tax_amount = _safe_float(line.get("montant_tva", 0), 0)
        if tax_amount == 0:
            tax_amount = round(net_amount * rate_num / 100, 2)
        total_amount = _safe_float(line.get("montant_ttc", 0), 0)
        if total_amount == 0:
            total_amount = round(net_amount + tax_amount, 2)
```

**Bloc corrigé**

```python
        rate_num, rate_str = _extract_tax_rate(line.get("taux_tva", "18"))
        tax_amount = _safe_float(line.get("montant_tva", 0), 0)
        if tax_amount == 0:
            tax_amount = round(net_amount * rate_num / 100, 2)
        # DL_MontantTTC inclut deja le centime ET les taxes additionnelles.
        # On ne l'utilise PAS ici : ces taxes sont portees une seule fois
        # par l'en-tete (additional_cent_tax / additional_taxes). Le TTC de
        # ligne SFEC = net HT + TVA de la ligne.
        total_amount = round(net_amount + tax_amount, 2)
```

**Dictionnaire `item` à remplacer** (`endpoints.py:193-206`)

```python
        item = {
            "designation": designation,
            "classification_code": classification_code,
            "type": item_type,
            "unit_price": round(unit_price, 2),
            "quantity": round(quantity, 2),
            "subtotal": round(subtotal, 2),
            "discount_amount": round(discount_amount, 2),
            "discount_type": discount_type,
            "net_amount": round(net_amount, 2),
            "tax_rate": rate_str,
            "tax_amount": round(tax_amount, 2),
            "total_amount": round(total_amount, 2),
        }
```

**Dictionnaire corrigé**

```python
        item = {
            "designation": designation,
            "classification_code": classification_code,
            "type": item_type,
            "unit_price": round(unit_price, 2),
            "quantity": round(quantity, 2),
            # SFEC : subtotal = montant HT *apres* remise de ligne.
            # Le brut et la remise restent informatifs.
            "subtotal": round(net_amount, 2),
            "discount_amount": round(discount_amount, 2),
            "discount_type": discount_type,
            "amount_after_discount": round(net_amount, 2),
            # SFEC : net_amount = TTC de la ligne (subtotal + tax_amount),
            # malgre son nom. total_amount porte la meme valeur.
            "net_amount": round(net_amount + tax_amount, 2),
            "tax_rate": rate_str,
            "tax_amount": round(tax_amount, 2),
            "total_amount": round(net_amount + tax_amount, 2),
        }
```

> Conséquence : `subtotal` d'en-tête devient la somme des HT **nets** (conforme à l'exemple officiel SFEC : 3 320 + 7 960 = 11 280 avec 2 000 de remises lignes déjà déduites). `total_line_discount_amount` reste informatif et **n'est plus soustrait** du total.

### 4.3 Détection des taxes additionnelles

Le TTC Sage est la source de vérité. On isole le résidu après TVA et centimes.

**Nouvelle fonction à insérer avant `db_invoice_to_sfec()`** :

```python
def _extract_additional_taxes(invoice, subtotal, total_tax, centimes,
                              tolerance=None, max_rate=None):
    """Deduit les taxes additionnelles du TTC Sage.

    SFEC separe :
      - la TVA 18 % / 5 %  -> total_tax_t_amount / total_tax_r_amount
      - le centime          -> additional_cent_tax (5 % de la TVA)
      - les autres taxes    -> additional_taxes (3 %, 0,25 %, TAR, TAX_MUNI...)
    Le TTC Sage est la seule source fiable pour retrouver ces dernieres :
    residu = TTC - HT net - TVA - centimes.

    Un residu negatif (ex. TTC = HT avec une TVA a 18 %) n'est PAS absorbe :
    il est signale par check_sage_totals() et la facture part en A_SURVEILLER.
    """
    cfg = get_config().get("validation", {})
    if tolerance is None:
        tolerance = float(cfg.get("ttc_sage_tolerance", 1.0))
    if max_rate is None:
        max_rate = float(cfg.get("max_additional_tax_rate", 10.0))

    sage_ttc = _safe_float(invoice.get("montant_ttc"), 0)
    if sage_ttc <= 0 or subtotal <= 0:
        return []

    extra = round(sage_ttc - subtotal - total_tax - centimes, 2)
    if extra <= tolerance:
        return []

    rate = round(extra / subtotal * 100, 2)
    # Le residu contient les arrondis de Sage : 2,994 % -> 3 %.
    if abs(rate - round(rate)) <= 0.05:
        rate = float(round(rate))
    if rate > max_rate:
        return []          # taux inhabituel -> laisse le garde-fou decider

    code = _safe_str(invoice.get("additional_tax_code"), "")
    label = _safe_str(invoice.get("additional_tax_label"), "")
    if not code:
        # Code Sage (203 pour le 3 % chez le client) ou libelle configurable.
        for key, val in (cfg.get("additional_tax_codes") or {}).items():
            try:
                if abs(float(key) - rate) <= 0.1:
                    code = _safe_str(val)
                    break
            except (TypeError, ValueError):
                continue
        code = code or "AUTRE"
    if not label:
        label = "Taxe additionnelle {}%".format(rate)

    return [{
        "tax_code": code,
        "tax_label": label,
        "tax_amount": extra,
        "tax_rate": rate,
    }]
```

> Le `tax_amount` est le **résidu exact** du TTC Sage, pas `base × taux` : les arrondis de Sage (ex. 3 928 × 3 % = 117,84 mais résidu = 117,61) sont absorbés dans le montant, et le total reste égal au TTC imprimé. Le taux est arrondi à 3 % pour l'affichage. Si SFEC contrôlait `base × taux`, il faudrait recalculer le montant et accepter un écart de quelques centimes sur le total — à vérifier en sandbox.

> Si le client confirme le code officiel (`TAX_MUNI`, `TAR`…), il suffit de le renseigner dans `validation.additional_tax_codes` — aucun redéploiement de code.

### 4.4 Totaux d'en-tête — `endpoints.py:245-251`

**Bloc à remplacer**

```python
    total_tax = round(total_tax_t + total_tax_r, 2)
    subtotal = round(sum(i["subtotal"] for i in items), 2)
    total_amount = round(
    sum(i["total_amount"] for i in items)
    + _safe_float(invoice.get("additional_cent_tax"), 0), 2
)
    total_line_discount = round(sum(i["discount_amount"] for i in items), 2)
```

**Bloc corrigé**

```python
    total_tax = round(total_tax_t + total_tax_r, 2)
    subtotal = round(sum(i["subtotal"] for i in items), 2)
    total_line_discount = round(sum(i["discount_amount"] for i in items), 2)

    # Centime additionnel : 5 % de la TVA collectee (decret n° 2023-1737,
    # art. 22) — meme regle que recalc_invoice_totals() / compute_cent_tax().
    additional_cent_tax = compute_cent_tax(total_tax)

    # Taxes additionnelles (3 %, 0,25 %, TAR, TAX_MUNI...) : hors
    # total_tax_amount, dans `additional_taxes`.
    additional_taxes = _extract_additional_taxes(
        invoice, subtotal, total_tax, additional_cent_tax
    )
    additional_taxes_total = round(
        sum(_safe_float(t.get("tax_amount"), 0) for t in additional_taxes), 2
    )

    total_amount = round(
        subtotal + total_tax + additional_cent_tax + additional_taxes_total, 2
    )
```

### 4.5 Payload — `endpoints.py:282-296`

**Bloc à remplacer**

```python
        "total_tax_amount": total_tax,
        ...
        "additional_cent_tax": round(_safe_float(invoice.get("additional_cent_tax", 0), 0), 2),
        "electronic_stamp_duty": 0,  # regle absolue SFEC : toujours 0, jamais lu depuis invoice
        "total_amount": total_amount,
```

**Bloc corrigé**

```python
        "total_tax_amount": total_tax,
        ...
        "additional_cent_tax": additional_cent_tax,
        "electronic_stamp_duty": 0,  # regle absolue SFEC : toujours 0
        "total_amount": total_amount,
```

Puis, juste après la construction de `sfec_req` :

```python
    if additional_taxes:
        sfec_req["additional_taxes"] = additional_taxes
```

> Le champ est optionnel côté API : on ne l'envoie que s'il y a réellement une taxe additionnelle.

### 4.6 Repli sans lignes — `endpoints.py:216-243`

**Bloc à remplacer**

```python
    if not items:
        montant_ht = _safe_float(invoice.get("montant_ht"), 0)
        montant_tva = _safe_float(invoice.get("montant_tva"), 0)
        montant_ttc = _safe_float(invoice.get("montant_ttc"), 0)
        if montant_ht == 0 and montant_ttc > 0:
            montant_ht = round(montant_ttc / 1.18, 2)
            montant_tva = round(montant_ttc - montant_ht, 2)
```

**Bloc corrigé**

```python
    if not items:
        montant_ht = _safe_float(invoice.get("montant_ht"), 0)
        montant_ttc = _safe_float(invoice.get("montant_ttc"), 0)

        if montant_ht <= 0 and montant_ttc > 0:
            # Repli degrade : sans lignes ni HT, on suppose 18 % + centimes.
            montant_ht = round(montant_ttc / 1.189, 2)
        if montant_ht <= 0 and montant_ttc <= 0:
            montant_ht = 1

        ratio = (montant_ttc / montant_ht) if montant_ht else None
        rate = _infer_vat_rate(ratio)
        if rate is None:
            rate = 0.18
        montant_tva = round(montant_ht * rate, 2)
        centimes = compute_cent_tax(montant_tva)
        if montant_ttc <= 0:
            montant_ttc = round(montant_ht + montant_tva + centimes, 2)
```

Puis, dans l'item de repli :

```python
        # avant : "subtotal": montant_ht, "net_amount": montant_ht,
        #         "total_amount": montant_ttc,
        item_total = round(montant_ht + montant_tva, 2)
        "subtotal": montant_ht,
        "amount_after_discount": montant_ht,
        "net_amount": item_total,
        "total_amount": item_total,
```

Le reste (centimes, taxes additionnelles, total) est calculé par l'en-tête commun (§4.4) : le repli n'a plus à recalculer ces montants.

> Le repli reste un cas dégradé : si le HT manque, on suppose 18 % + centimes, et `check_sage_totals()` signale « HT Sage absent » — donc pas de certification silencieuse. Il ne doit servir qu'aux factures **sans lignes**.

### 4.7 Validateur — `endpoints.py:394-410`

Le total attendu doit inclure les taxes additionnelles et **ne plus soustraire** les remises lignes (elles sont déjà dans `subtotal`).

**Bloc à remplacer**

```python
    expected_total = round(
        _safe_float(payload.get("subtotal"), 0)
        - _safe_float(payload.get("discount_amount"), 0)
        - _safe_float(payload.get("total_line_discount_amount"), 0)
        + _safe_float(payload.get("total_tax_amount"), 0)
        + _safe_float(payload.get("additional_cent_tax"), 0)
        + _safe_float(payload.get("electronic_stamp_duty"), 0),
        2
    )
```

**Bloc corrigé**

```python
    additional_taxes_total = round(
        sum(_safe_float(t.get("tax_amount"), 0)
            for t in (payload.get("additional_taxes") or [])),
        2,
    )
    expected_total = round(
        _safe_float(payload.get("subtotal"), 0)
        # subtotal = HT net : les remises lignes y sont deja deduites.
        + _safe_float(payload.get("total_tax_amount"), 0)
        + _safe_float(payload.get("additional_cent_tax"), 0)
        + additional_taxes_total
        + _safe_float(payload.get("electronic_stamp_duty"), 0),
        2
    )
```

> `discount_amount` (escompte global) n'entre pas dans `total_amount` : la doc SFEC précise qu'il ne s'applique qu'à `amount_due`.

### 4.8 Garde-fou : contrôle contre le TTC Sage

**Nouvelle fonction à insérer après `db_invoice_to_sfec()`** :

```python
def check_sage_totals(invoice, payload, tolerance=None):
    """Verifie que le TTC recalcule recoupe la facture Sage.

    Le TTC Sage est la source de verite : c'est le montant imprime et
    comptabilise. Apres extraction des taxes additionnelles, l'ecart doit
    etre nul. Un ecart residuel signifie que la facture n'est pas
    representable (ex. TTC = HT sans TVA) -> A_SURVEILLER, pas certification.
    """
    cfg = get_config().get("validation", {})
    if tolerance is None:
        tolerance = float(cfg.get("ttc_sage_tolerance", 1.0))

    sage_ttc = _safe_float(invoice.get("montant_ttc"), None)
    if sage_ttc is None or sage_ttc <= 0:
        return []

    sage_ht = _safe_float(invoice.get("montant_ht"), None)
    if sage_ht is None or sage_ht <= 0:
        return [
            "HT Sage absent ou nul: ventilation TVA/centimes non fiable "
            "- verifier la facture dans Sage"
        ]

    if abs(sage_ttc - sage_ht) <= tolerance:
        return [
            "TTC = HT ({} FCFA) : aucune TVA facturee — exoneration a "
            "confirmer avant certification".format(sage_ttc)
        ]

    max_rate = _safe_float(cfg.get("max_additional_tax_rate"), 10.0)
    for tax in payload.get("additional_taxes") or []:
        rate = _safe_float(tax.get("tax_rate"), 0)
        if max_rate and rate > max_rate:
            return [
                "Taxe additionnelle inhabituelle: {} % ({}) — verifier la "
                "facture dans Sage".format(rate, tax.get("tax_code"))
            ]

    ecart = round(_safe_float(payload.get("total_amount"), 0) - sage_ttc, 2)
    if abs(ecart) <= tolerance:
        return []

    return [
        "TTC Sage={} != TTC calcule={} (ecart {} FCFA) : taxe non "
        "ventilable — corriger la facture dans Sage".format(
            sage_ttc, payload.get("total_amount"), ecart
        )
    ]
```

**Appel dans `certify_invoice()` (`endpoints.py:426-436`)** :

```python
    sfec_req = db_invoice_to_sfec(invoice)
    errors = validate_sfec_payload(sfec_req) + check_sage_totals(invoice, sfec_req)
```

### 4.9 Configuration — `config.json`

```json
"validation": {
  "strict_mode": true,
  "tolerance_amount": 0.01,
  "ttc_sage_tolerance": 1.0,
  "max_additional_tax_rate": 10.0,
  "additional_tax_codes": {}
}
```

`ttc_sage_tolerance = 1,0 FCFA` absorbe les arrondis de Sage (écarts observés : −0,54 à +0,53 FCFA). `max_additional_tax_rate = 10 %` empêche d'absorber silencieusement une erreur de saisie.

`additional_tax_codes` reste **vide** tant que le code SFEC exact de la taxe de 3 % n'est pas confirmé : le payload envoie alors `AUTRE`. Le code Sage imprimé sur la facture (`203`) n'est pas nécessairement un code SFEC (`TAR`, `TAX_MUNI`…). À renseigner après le test sandbox, sans redéploiement.

## 5. Tests

Créer `tests/test_sfec_centimes.py` :

```python
import pytest
from app.integration.sfec.endpoints import (
    db_invoice_to_sfec, validate_sfec_payload, check_sage_totals,
)


def _invoice(lignes, **kw):
    inv = {
        "numero": "TEST-CENT",
        "type_doc": "vente",
        "montant_ht": sum(l["montant_ht"] for l in lignes),
        "montant_tva": sum(l["montant_tva"] for l in lignes),
        "montant_ttc": sum(l["montant_ttc"] for l in lignes),
        "lignes": lignes,
    }
    inv.update(kw)
    return inv


def test_centime_ajoute_une_seule_fois():
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 10000,
        "montant_ht": 10000, "taux_tva": 18, "montant_tva": 1800,
        "montant_ttc": 11890,   # 10 000 + 1 800 + 90
    }])
    p = db_invoice_to_sfec(inv)
    assert p["items"][0]["total_amount"] == 11800.0
    assert p["additional_cent_tax"] == 90.0
    assert "additional_taxes" not in p
    assert p["total_amount"] == 11890.0
    assert validate_sfec_payload(p) == []
    assert check_sage_totals(inv, p) == []


def test_centime_sur_tva_18_et_5():
    inv = _invoice([
        {"designation": "A", "quantite": 1, "prix_unitaire": 10000,
         "montant_ht": 10000, "taux_tva": 18, "montant_tva": 1800,
         "montant_ttc": 11890},
        {"designation": "B", "quantite": 1, "prix_unitaire": 10000,
         "montant_ht": 10000, "taux_tva": 5, "montant_tva": 500,
         "montant_ttc": 10525},
    ])
    p = db_invoice_to_sfec(inv)
    assert p["additional_cent_tax"] == 115.0      # 5 % de 2 300
    assert p["total_amount"] == 22415.0


def test_remise_ligne_aligne_sfec():
    inv = _invoice([{
        "designation": "Produit remise", "quantite": 1, "prix_unitaire": 10000,
        "montant_ht": 9000, "taux_tva": 18, "montant_tva": 1620,
        "montant_ttc": 10701,   # 9 000 + 1 620 + 81 (5 % de TVA)
    }])
    p = db_invoice_to_sfec(inv)
    assert p["items"][0]["subtotal"] == 9000.0          # HT net
    assert p["items"][0]["discount_amount"] == 1000.0
    assert p["items"][0]["net_amount"] == 10620.0       # TTC de ligne
    assert p["subtotal"] == 9000.0
    assert p["total_amount"] == 10701.0


def test_taxe_additionnelle_3_pourcent():
    """Cas 26FAZ07297 / 25VFA3122 : 18 % + 3 % + centime."""
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 3928,
        "montant_ht": 3928, "taux_tva": 18, "montant_tva": 707.04,
        "montant_ttc": 4788,
    }])
    p = db_invoice_to_sfec(inv)
    assert p["additional_cent_tax"] == pytest.approx(35.35, abs=0.01)
    assert len(p["additional_taxes"]) == 1
    tax = p["additional_taxes"][0]
    assert tax["tax_rate"] == pytest.approx(3.0, abs=0.05)
    assert tax["tax_amount"] == pytest.approx(117.61, abs=0.05)
    assert p["total_amount"] == 4788.0
    assert validate_sfec_payload(p) == []
    assert check_sage_totals(inv, p) == []


def test_ttc_egal_ht_signale():
    """Cas 25VFA3118 : aucune TVA facturee -> A_SURVEILLER, pas de payload."""
    inv = _invoice([{
        "designation": "Produit", "quantite": 1, "prix_unitaire": 250000,
        "montant_ht": 250000, "taux_tva": 18, "montant_tva": 45000,
        "montant_ttc": 250000,
    }])
    p = db_invoice_to_sfec(inv)
    assert any("TTC = HT" in e for e in check_sage_totals(inv, p))
```

Cas à couvrir aussi :

- facture locale (`source = 'web'`) : `montant_ttc` déjà calculé par `recalc_invoice_totals` → aucun `additional_taxes` parasite ;
- cluster A (`25VFB3811`) : écart résiduel −0,46 FCFA absorbé par `ttc_sage_tolerance = 1,0` ;
- cluster D (`25VFA3134`) : mixte 18 %/5 % → total 177 375, aucun `additional_taxes` ;
- `cent_tax_rate = 0` (setting) : `additional_cent_tax = 0`, pas de régression ;
- avoir (montants négatifs) : voir `05-proposition-avoirs-references.md` ;
- taux additionnel > 10 % : garde-fou déclenché, pas d'absorption.

## 6. Critères d'acceptation

- [ ] Les tests ci-dessus passent (`tests/test_sfec_centimes.py`), plus la suite `pytest -q`.
- [ ] Sur la copie client : les 162 factures du cluster A **et** les 15 du cluster C (3 %) ne produisent plus d'`Incoherence totaux` ; C passe en `CERTIFIE`, ou en `A_SURVEILLER` si SFEC refuse le `tax_code` (jamais de rejeu).
- [ ] Les 4 PDF de `WATCH/IN` (`25VFA3142`, `25VFA3141`, `25VFA3103`, `25VFA3104`) passent en certifié.
- [ ] `check_sage_totals` signale exactement les **11** factures `TTC = HT` et, le cas échéant, `25VFA3134` — ni plus, ni moins.
- [ ] Le taux des centimes reste piloté par `settings.cent_tax_rate` (pas de 5 codé en dur).
- [ ] Aucune facture certifiée avec un `total_amount` différent du TTC Sage.

## 7. Risques / rollback

| Risque | Parade |
|---|---|
| Le code de taxe (`AUTRE`) est refusé par SFEC | Tester le 3 % en sandbox ; renseigner le code officiel (`TAX_MUNI`…) dans `validation.additional_tax_codes` |
| Changement de sémantique des items (`subtotal` net) | Tester une facture sans remise, puis une avec remise, en sandbox |
| Une facture déjà certifiée est recalculée | Aucune : le payload n'est construit qu'avant certification |
| Écart résiduel de quelques FCFA | `ttc_sage_tolerance` configurable (défaut 1,0) |
| Un vrai écart fiscal passe pour une taxe additionnelle | `max_additional_tax_rate` (10 %) + journalisation du taux et du code |
| Régression sur les factures locales | `source = 'web'` : `montant_ttc` déjà cohérent, `additional_taxes` vide |

**Rollback** : revert du seul fichier `endpoints.py` + suppression de `check_sage_totals` de `certify_invoice`. Aucune migration de schéma.
