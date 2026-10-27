"""Ventilation fiscale des factures pour le payload SFEC.

Regles fiscales appliquees (modele Congo, facture client `26FAZ07297`) :

  - la TVA 18 % va dans `total_tax_t_amount`, la TVA 5 % dans
    `total_tax_r_amount` ;
  - le centime additionnel vaut **5 % de la TVA collectee** (decret
    n° 2023-1737 art. 22), jamais 5 % du HT ; il va dans
    `additional_cent_tax` ;
  - les taxes supplementaires (3 %, 2,5 %, ...) vont dans `additional_taxes` ;
    elles ne sont **jamais** absorbees dans le centime ;
  - le TTC Sage est la source de verite : le montant final du payload
    reproduit exactement le net a payer imprime par Sage.

Sage ne stocke pas les montants de taxes : ils sont calcules. Le module
reconstruit donc la ventilation a partir du HT, des taux des lignes et du
TTC, puis verifie que le total recalcule retombe sur le TTC Sage.

Les montants des factures Sage sont exprimes en francs entiers (XAF) :
les taxes sont donc arrondies au franc, comme Sage. Les factures locales
(POS/web) restent au centime.
"""

import decimal

# ---------------------------------------------------------------------------
# Roles d'une ligne de facture
# ---------------------------------------------------------------------------
VAT_NORMAL = "vat_normal"
VAT_REDUCED = "vat_reduced"
EXEMPT = "exempt"
CENTIME = "centime"
ADDITIONAL = "additional"

ITEM_ROLES = (VAT_NORMAL, VAT_REDUCED, EXEMPT)

# Taux du centime exprime en % du HT (5 % de la TVA collectee) :
# 0,9 % pour une base a 18 %, 0,25 % pour une base a 5 %.
CENTIME_HT_RATES = (0.25, 0.9)

# Taux -> code SFEC / libelle, surchargeables par la configuration.
# La doc SFEC (openapi.yaml) ne donne aucune liste de valeurs autorisees pour
# tax_code : le seul exemple est "TAR" (Taxe sur les activites remunerées, 2,5 %).
# Le code "AUTRE" est un fallback a remplacer par le code officiellement
# accepte par SFEC une fois confirme avec le client.
DEFAULT_ADDITIONAL_TAX_CODES = {"3": "AUTRE"}
DEFAULT_ADDITIONAL_TAX_LABELS = {"3": "Taxe supplementaire 3 %"}

# Ratios TTC/HT connus, pour le repli quand les lignes sont absentes.
#   1,0000 -> exonere
#   1,0525 -> 5 % + centime
#   1,1890 -> 18 % + centime
#   1,2190 -> 18 % + centime + 3 %
KNOWN_RATIO_PROFILES = (
    (1.0, 0.0),
    (1.0525, 5.0),
    (1.189, 18.0),
    (1.219, 18.0),
)


def safe_float(value, default=0.0):
    """Conversion tolérante (accepte les chaines '1 234,56')."""
    try:
        if value is None or value == "":
            return default
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return default


def round_amount(value, decimals=2):
    """Arrondi commercial (demi-superieur), comme Sage.

    `round()` de Python applique l'arrondi du banker (2,5 -> 2) ; Sage
    arrondit au franc superieur. On utilise Decimal pour un resultat
    stable et identique a l'impression client.
    """
    try:
        quant = decimal.Decimal(1).scaleb(-int(decimals))
        return float(decimal.Decimal(str(float(value))).quantize(
            quant, rounding=decimal.ROUND_HALF_UP))
    except (TypeError, ValueError, decimal.InvalidOperation):
        return float(value or 0)


def tax_decimals(invoice, currency="XAF"):
    """Nombre de decimales des montants de taxe.

    Les factures Sage en XAF sont editees au franc entier : on arrondit
    les taxes au franc pour reproduire l'impression. Les factures locales
    (POS/web) restent au centime.
    """
    source = str((invoice or {}).get("source") or "").strip().lower()
    if str(currency or "").strip().upper() == "XAF" and source == "sage":
        return 0
    return 2


def classify_line_rate(rate):
    """Role fiscal d'un taux de ligne.

    Meme classification que le miroir local (`_mirror_fields`) :
      - 0 %        -> exonere
      - >= 17,5 %  -> TVA normale (18 %, 20 %...)
      - 4,5-5,5 %  -> TVA reduite
      - 0,2-1 %    -> centime additionnel (0,25 % / 0,9 % du HT)
      - autre      -> taxe supplementaire (3 %, 2,5 %...)
    """
    value = abs(safe_float(rate))
    if value < 0.01:
        return EXEMPT
    if value >= 17.5:
        return VAT_NORMAL
    if 4.5 <= value <= 5.5:
        return VAT_REDUCED
    if 0.2 <= value <= 1.0:
        return CENTIME
    return ADDITIONAL


def infer_vat_rate_percent(ratio):
    """Taux de TVA (en %) deduit d'un ratio TTC/HT connu, sinon None."""
    if ratio is None:
        return None
    for ref, rate in KNOWN_RATIO_PROFILES:
        if abs(ratio - ref) <= 0.001:
            return rate
    return None


def snap_rate(rate):
    """Retablit le taux exact quand les arrondis Sage le déforment.

    2,994 % (residu d'un 3 %) redevient 3 % ; un taux fractionnaire
    (0,25 %) reste intact.
    """
    value = safe_float(rate)
    if abs(value - round(value)) <= 0.05:
        return float(round(value))
    return round(value, 2)


def _config_section(config, name):
    if not config:
        return {}
    section = config.get(name)
    return section if isinstance(section, dict) else {}


def tax_code_profile(config=None):
    """Profil des codes de taxe Sage (toujours vide : la doc SFEC
    n'en donne aucune liste autorisee)."""
    return {}


def resolve_additional_tax(rate, config=None, invoice=None):
    """Code et libelle SFEC d'une taxe supplementaire de taux `rate`.

    Ordre : surcharge explicite sur la facture, configuration
    (`validation.additional_tax_codes` / `additional_tax_labels`),
    puis defaut `AUTRE`. Aucun code Sage n'est resolu : la doc SFEC
    n'en donne aucune liste autorisee (exemple : "TAR").
    """
    rate = snap_rate(rate)
    code = ""
    label = ""

    invoice = invoice or {}
    explicit_code = str(invoice.get("additional_tax_code") or "").strip()
    explicit_label = str(invoice.get("additional_tax_label") or "").strip()
    if explicit_code:
        code = explicit_code
    if explicit_label:
        label = explicit_label

    codes = dict(DEFAULT_ADDITIONAL_TAX_CODES)
    codes.update(_config_section(config, "additional_tax_codes") or {})
    for key, value in codes.items():
        if not code and abs(safe_float(key, -1) - rate) <= 0.1:
            code = str(value or "").strip()
            break

    labels = dict(DEFAULT_ADDITIONAL_TAX_LABELS)
    labels.update(_config_section(config, "additional_tax_labels") or {})
    for key, value in labels.items():
        if not label and abs(safe_float(key, -1) - rate) <= 0.1:
            label = str(value or "").strip()
            break

    if not code:
        code = "AUTRE"
    if not label:
        label = "Taxe supplementaire {}%".format(("%g" % rate))
    return code, label


def allocate_amount(total, weights, decimals=2):
    """Repartit `total` entre `weights` (methode du plus grand ecart).

    La somme des montants alloues est exactement egale a `total` : la
    ventilation par taux reste coherent avec le total de facture.
    """
    count = len(weights)
    if count == 0:
        return []
    weight_sum = sum(weights)
    if weight_sum <= 0:
        shares = [total / count] * count
    else:
        shares = [total * weight / weight_sum for weight in weights]
    allocated = [round_amount(share, decimals) for share in shares]
    difference = round_amount(total - sum(allocated), decimals)
    if difference:
        heaviest = max(range(count), key=lambda idx: shares[idx])
        allocated[heaviest] = round_amount(
            allocated[heaviest] + difference, decimals)
    return allocated


def _merge_records(line_records):
    records = []
    for record in line_records or []:
        rate = abs(safe_float(record.get("rate")))
        kind = record.get("kind") or classify_line_rate(rate)
        records.append({
            "net": abs(safe_float(record.get("net"))),
            "rate": rate,
            "kind": kind,
            "line": record.get("line") or {},
        })
    return records


def compute_tax_breakdown(invoice, line_records, config=None, currency="XAF",
                           cent_rate=None):
    """Ventile les taxes d'une facture.

    `line_records` : liste de dicts `{"net": float, "rate": float,
    "kind": str, "line": dict}` (une entree par ligne d'article).

    Retourne un dict :
      - `ok` : True si la ventilation est exploitable ;
      - `errors` / `warnings` : motifs operateur ;
      - `subtotal`, `total_tax_t_amount`, `total_tax_r_amount`,
        `total_tax_amount`, `total_exempt_amount`, `additional_cent_tax`,
        `additional_taxes`, `total_amount` ;
      - `line_tax_amounts` : TVA allouee a chaque ligne d'entree ;
      - `synthetic` : ligne de repli quand la facture n'a pas de lignes.
    """
    invoice = invoice or {}
    config = config or {}
    decimals = tax_decimals(invoice, currency)
    tolerance = safe_float(config.get("ttc_sage_tolerance"), 1.0)
    max_rate = safe_float(config.get("max_additional_tax_rate"), 10.0)
    if cent_rate is None:
        cent_rate = safe_float(config.get("cent_tax_rate"), 5.0) or 5.0

    errors = []
    warnings = []
    records = _merge_records(line_records)
    synthetic = None

    # -- Repli sans lignes : on synthetise une ligne depuis l'en-tete -----
    if not records:
        subtotal = abs(safe_float(invoice.get("montant_ht")))
        ttc = abs(safe_float(invoice.get("montant_ttc")))
        if subtotal <= 0 and ttc > 0:
            subtotal = round_amount(ttc / 1.189, decimals)
        if subtotal <= 0:
            subtotal = 1.0
        ratio = (ttc / subtotal) if subtotal else None
        rate = infer_vat_rate_percent(ratio)
        if rate is None:
            rate = 18.0
            warnings.append(
                "TTC/HT inconnu ({}) : TVA 18 % supposee - verifier la facture"
                .format(round(ratio, 6) if ratio else "-"))
        records = [{
            "net": subtotal,
            "rate": rate,
            "kind": classify_line_rate(rate),
            "line": {},
        }]
        synthetic = True

    # -- Regroupement des lignes par taux de TVA ----------------------------
    groups = {}
    for index, record in enumerate(records):
        if record["kind"] not in (VAT_NORMAL, VAT_REDUCED):
            continue
        groups.setdefault(round(record["rate"], 2), []).append(index)

    line_tax_amounts = [0.0] * len(records)
    vat_normal = 0.0
    vat_reduced = 0.0
    for rate, indexes in groups.items():
        base = sum(records[idx]["net"] for idx in indexes)
        group_total = round_amount(base * rate / 100.0, decimals)
        allocated = allocate_amount(
            group_total, [records[idx]["net"] for idx in indexes], decimals)
        for idx, amount in zip(indexes, allocated):
            line_tax_amounts[idx] = amount
        kind = classify_line_rate(rate)
        if kind == VAT_REDUCED:
            vat_reduced = round_amount(vat_reduced + group_total, decimals)
        else:
            vat_normal = round_amount(vat_normal + group_total, decimals)

    item_records = [rec for rec in records if rec["kind"] in ITEM_ROLES]
    subtotal = round_amount(sum(rec["net"] for rec in item_records), decimals)
    if subtotal <= 0:
        subtotal = round_amount(abs(safe_float(invoice.get("montant_ht"))), decimals)
    total_exempt = round_amount(
        sum(rec["net"] for rec in records if rec["kind"] == EXEMPT), decimals)
    total_vat = round_amount(vat_normal + vat_reduced, decimals)

    # -- Centime additionnel = 5 % de la TVA collectee ----------------------
    additional_cent_tax = round_amount(total_vat * cent_rate / 100.0, decimals)

    # -- Controle : les lignes de taxe confirme-elles la ventilation ? ------
    centime_from_lines = 0.0
    additional_from_lines = []
    for record in records:
        if record["kind"] == CENTIME:
            centime_from_lines = round_amount(
                centime_from_lines
                + record["net"] * record["rate"] / 100.0, decimals)
        elif record["kind"] == ADDITIONAL:
            amount = round_amount(
                record["net"] * record["rate"] / 100.0, decimals)
            code, label = resolve_additional_tax(record["rate"], config, invoice)
            additional_from_lines.append({
                "tax_code": code,
                "tax_label": label,
                "tax_amount": amount,
                "tax_rate": snap_rate(record["rate"]),
            })
    if centime_from_lines and abs(centime_from_lines - additional_cent_tax) > tolerance:
        warnings.append(
            "Centime calcule ({}) different du centime des lignes ({})"
            .format(additional_cent_tax, centime_from_lines))

    # -- Taxes supplementaires : residue du TTC Sage ------------------------
    ttc = abs(safe_float(invoice.get("montant_ttc")))
    additional_taxes = []
    residual = 0.0
    if ttc > 0:
        residual = round_amount(
            ttc - subtotal - total_vat - additional_cent_tax, decimals)
        if residual < -tolerance:
            errors.append(
                "TTC Sage ({}) inferieur a HT + TVA + centime ({}) : "
                "ventilation impossible".format(ttc, subtotal + total_vat
                                                + additional_cent_tax))
        elif residual > tolerance:
            if additional_from_lines:
                total_lines = sum(tax["tax_amount"] for tax in additional_from_lines)
                if abs(total_lines - residual) <= tolerance:
                    additional_taxes = [dict(tax) for tax in additional_from_lines]
                    difference = round_amount(residual - total_lines, decimals)
                    if difference and additional_taxes:
                        last = additional_taxes[-1]
                        last["tax_amount"] = round_amount(
                            last["tax_amount"] + difference, decimals)
                else:
                    warnings.append(
                        "Taxes des lignes ({}) incompatibles avec le TTC ({})"
                        .format(total_lines, residual))
            if not additional_taxes:
                rate = 0.0
                if subtotal > 0:
                    rate = snap_rate(residual / subtotal * 100.0)
                if max_rate and rate > max_rate:
                    errors.append(
                        "Taxe additionnelle inhabituelle : {} % - corriger la "
                        "facture dans Sage".format(rate))
                else:
                    code, label = resolve_additional_tax(rate, config, invoice)
                    additional_taxes = [{
                        "tax_code": code,
                        "tax_label": label,
                        "tax_amount": residual,
                        "tax_rate": rate,
                    }]
                    if code == "AUTRE":
                        warnings.append(
                            "Code SFEC de la taxe de {} % inconnu : configure "
                            "validation.additional_tax_codes".format(rate))
        elif abs(residual) > 0:
            # Ecart d'arrondi inferieur a la tolerance : il est signale mais
            # jamais compense sur le centime (le centime reste 5 % de la TVA).
            warnings.append(
                "Ecart d'arrondi de {} FCFA entre le TTC calcule et le TTC "
                "Sage".format(residual))

    additional_taxes_total = round_amount(
        sum(safe_float(tax.get("tax_amount")) for tax in additional_taxes),
        decimals)
    total_amount = round_amount(
        subtotal + total_vat + additional_cent_tax + additional_taxes_total,
        decimals)

    if ttc > 0 and abs(total_amount - ttc) > 0.01:
        errors.append(
            "TTC calcule ({}) different du TTC Sage ({}) : ecart {} FCFA"
            .format(total_amount, ttc, round(total_amount - ttc, 2)))

    if synthetic:
        synthetic_line = {
            "net": records[0]["net"] if records else 0.0,
            "rate": records[0]["rate"] if records else 0.0,
            "tax_amount": line_tax_amounts[0] if line_tax_amounts else 0.0,
        }
    else:
        synthetic_line = None

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "subtotal": subtotal,
        "total_tax_t_amount": vat_normal,
        "total_tax_r_amount": vat_reduced,
        "total_tax_amount": total_vat,
        "total_exempt_amount": total_exempt,
        "additional_cent_tax": additional_cent_tax,
        "additional_taxes": additional_taxes,
        "additional_taxes_total": additional_taxes_total,
        "total_amount": total_amount,
        "line_tax_amounts": line_tax_amounts,
        "synthetic": synthetic_line,
        "decimals": decimals,
    }
