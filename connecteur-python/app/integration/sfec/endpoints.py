import re
import json
import os
import time
import logging
import threading
from datetime import datetime
from app.config.manager import get_company_config, get_config
from app.integration.sfec.client import SfecClient
from app.storage.db import compute_cent_tax

_DEFAULT_RECIPIENT_NIU = "M000000000000001"
_DEFAULT_RECIPIENT_PHONE = "069134144"
_DEFAULT_RECIPIENT_EMAIL = "relaud.aka@alucongo.com"
_TYPE_DOC_TO_SFEC = {
    "vente": "salesInvoice",
    "avoir": "creditNote",
}


logger = logging.getLogger("t-connector.sfec.endpoints")

from app.core.paths import data_dir as _uses_data_dir  # noqa: F401

DATA_DIR = _uses_data_dir()
CERTIFIED_FILE = os.path.join(DATA_DIR, "sfec-certified.json")

_certified_lock = threading.RLock()


def _load_certified():
    with _certified_lock:
        try:
            if os.path.exists(CERTIFIED_FILE):
                with open(CERTIFIED_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return []


def _save_certified(certified_list):
    with _certified_lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CERTIFIED_FILE, "w", encoding="utf-8") as f:
            json.dump(certified_list, f, indent=2, ensure_ascii=False)


def _extract_tax_rate(tax_rate_str):
    if not tax_rate_str:
        return 18, "18"
    clean = str(tax_rate_str).replace(",", ".")
    match = re.search(r"(\d+\.?\d*)", clean)
    rate = round(float(match.group(1)), 2) if match else 18
    return rate, str(rate)


def _map_recipient_type(raw_value):
    if not raw_value:
        return "business"
    val = str(raw_value).strip().upper()
    gov_keywords = {"GOUV", "ETAT", "MINISTERE", "MIN", "PUBLIC", "ADMIN", "MAIRIE", "MUNICIPAL", "PUB"}
    foreign_keywords = {"ETRANGER", "EXT", "ETR", "FOREIGN", "INTL", "INTERNATIONAL"}
    individual_keywords = {"PARTICULIER", "INDIVIDU", "PRIVE", "PERS", "PART", "INDIV"}
    for kw in gov_keywords:
        if kw in val:
            return "government"
    for kw in foreign_keywords:
        if kw in val:
            return "foreign"
    for kw in individual_keywords:
        if kw in val:
            return "individual"
    return "business"

def _map_invoice_type(type_doc):
    return _TYPE_DOC_TO_SFEC.get(type_doc, "salesInvoice")


# 25VFA3122, 9FAZ00124, VBA2818, FA00001...
_REF_TOKEN = re.compile(r"\b\d{0,2}[A-Z]{2,4}\d{3,}\b")


def resolve_invoice_number(raw, by_numero, manual_links=None, child_numero=None):
    """Retrouve un numero de piece Sage dans une reference libre.

    Ordre : lien manuel -> exact -> sans espaces/casse -> token ressemblant
    a un numero -> sous-chaine (le plus long d'abord). Retourne None si rien
    ne correspond : l'avoir part en A_SURVEILLER, il n'est jamais certifie
    avec une reference inventee.

    `manual_links` est la table `sfec_invoice_links` (numero avoir ->
    parent). Elle est consultee en priorite pour le `child_numero` courant.
    """
    if not raw and not (manual_links and child_numero):
        return None

    if manual_links and child_numero:
        parent = manual_links.get(str(child_numero).strip())
        if parent:
            # Le parent peut etre plus ancien que la fenetre du cache : le
            # moteur fera un GET SFEC de verification avant de certifier.
            logger.info("Avoir %s: lien manuel vers %s", child_numero, parent)
            return parent

    text = str(raw or "").strip()
    if not text:
        return None
    if text in by_numero:
        return text

    upper_index = {}
    for numero in by_numero:
        upper_index.setdefault(str(numero).strip().upper(), numero)

    compact = text.upper().replace(" ", "")
    if compact in upper_index:
        return upper_index[compact]

    for token in _REF_TOKEN.findall(text.upper()):
        numero = upper_index.get(token)
        if numero:
            return numero

    # Sous-chaine : on essaie les numeros les plus longs d'abord pour eviter
    # qu'un numero court contenu dans un autre gagne.
    for numero in sorted(by_numero, key=lambda n: len(str(n)), reverse=True):
        if len(str(numero)) >= 6 and str(numero).upper() in text.upper():
            return numero

    return None

def _safe_str(val, default=""):
    if val is None:
        return default
    s = str(val).strip()
    return s if s else default


def _normalize_date(val):
    if not val:
        return None
    s = str(val).strip()
    if not s or s == "1900-01-01":
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "{}-{}-{}".format(m.group(1), m.group(2), m.group(3))
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return "{}-{}-{}".format(m.group(3), m.group(2), m.group(1))
    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", s)
    if m:
        return "{}-{}-{}".format(m.group(3), m.group(2), m.group(1))
    nums = re.findall(r"\d+", s)
    if len(nums) >= 3:
        for i, n in enumerate(nums):
            if len(n) == 4:
                year = n
                rest = nums[:i] + nums[i+1:]
                if len(rest) >= 2:
                    return "{}-{}-{}".format(year, rest[1].zfill(2), rest[0].zfill(2))
    return None


def _safe_float(val, default=0):
    try:
        if val is None:
            return default
        return float(str(val).replace(",", "."))
    except (ValueError, TypeError):
        return default


# Ratios TTC/HT des factures Sage connues. Le 1,219 correspond a 18 % de TVA
# + 3 % de taxe additionnelle + 5 % de centimes sur la TVA ; le 1,189 au seul
# 18 % + centimes ; le 1,0525 au 5 % + centimes.
_RATIO_TO_VAT_RATE = (
    (1.0, 0.0),
    (1.0525, 0.05),
    (1.189, 0.18),
    (1.219, 0.18),
)


def _validation_config():
    try:
        return get_config().get("validation", {}) or {}
    except Exception:
        return {}


def _infer_vat_rate(ratio):
    """Deduit le taux de TVA d'un ratio TTC/HT connu, sinon None."""
    if ratio is None:
        return None
    for ref, rate in _RATIO_TO_VAT_RATE:
        if abs(ratio - ref) <= 0.001:
            return rate
    return None


def _extract_additional_taxes(invoice, subtotal, total_tax, centimes,
                              tolerance=None, max_rate=None):
    """Deduit les taxes additionnelles du TTC Sage.

    SFEC separe la TVA (total_tax_t_amount / total_tax_r_amount), le centime
    additionnel (additional_cent_tax, 5 % de la TVA) et les autres taxes
    (additional_taxes : 3 %, 0,25 %, TAR, TAX_MUNI...). Le TTC Sage est la
    source de verite : on isole le residu apres TVA et centimes.
    """
    if subtotal <= 0:
        return []
    cfg = _validation_config()
    if tolerance is None:
        tolerance = _safe_float(cfg.get("ttc_sage_tolerance"), 1.0)
    if max_rate is None:
        max_rate = _safe_float(cfg.get("max_additional_tax_rate"), 10.0)

    # Un avoir peut porter des montants negatifs dans Sage : la ventilation
    # se fait toujours en valeur absolue (le signe est porte par le type de
    # piece creditNote, pas par les montants du payload).
    sage_ttc = abs(_safe_float(invoice.get("montant_ttc"), 0))
    if sage_ttc <= 0:
        return []

    extra = round(sage_ttc - subtotal - total_tax - centimes, 2)
    if extra <= tolerance:
        return []

    rate = round(extra / subtotal * 100, 2)
    # Les arrondis de Sage transforment 3 % en 2,99 % : on rehausse les taux
    # quasi entiers, sans toucher aux taux fractionnaires (0,25 %).
    if abs(rate - round(rate)) <= 0.05:
        rate = float(round(rate))
    if max_rate and rate > max_rate:
        return []

    code = _safe_str(invoice.get("additional_tax_code"), "")
    if not code:
        for key, val in (cfg.get("additional_tax_codes") or {}).items():
            try:
                if abs(float(key) - rate) <= 0.1:
                    code = _safe_str(val)
                    break
            except (TypeError, ValueError):
                continue
    code = code or "AUTRE"

    label = _safe_str(invoice.get("additional_tax_label"), "")
    if not label:
        label = "Taxe additionnelle {}%".format(("%g" % rate))

    return [{
        "tax_code": code,
        "tax_label": label,
        "tax_amount": extra,
        "tax_rate": rate,
    }]


def check_sage_totals(invoice, payload, tolerance=None):
    """Compare le TTC reconstruit par le payload au TTC Sage.

    Detecte notamment les factures TTC = HT (aucune TVA facturee) et les
    taxes non ventilables. Ne certifie jamais un ecart > tolerance.
    """
    cfg = _validation_config()
    if tolerance is None:
        tolerance = _safe_float(cfg.get("ttc_sage_tolerance"), 1.0)

    sage_ttc = _safe_float(invoice.get("montant_ttc"), None)
    if sage_ttc is None or sage_ttc <= 0:
        return []

    sage_ht = _safe_float(invoice.get("montant_ht"), None)
    if sage_ht is None or sage_ht <= 0:
        return ["HT Sage absent ou nul: ventilation TVA/centimes non fiable "
                "- verifier la facture dans Sage"]

    if abs(sage_ttc - sage_ht) <= tolerance:
        return ["TTC = HT ({} FCFA): aucune TVA facturee - exoneration a "
                "confirmer".format(sage_ttc)]

    max_rate = _safe_float(cfg.get("max_additional_tax_rate"), 10.0)
    for tax in payload.get("additional_taxes") or []:
        rate = _safe_float(tax.get("tax_rate"), 0)
        if max_rate and rate > max_rate:
            return ["Taxe additionnelle inhabituelle: {} % ({}) - verifier "
                    "la facture dans Sage".format(rate, tax.get("tax_code"))]

    computed = _safe_float(payload.get("total_amount"), 0)
    ecart = round(computed - sage_ttc, 2)
    if abs(ecart) <= tolerance:
        return []

    return ["TTC Sage={} != TTC calcule={} (ecart {} FCFA): taxe non "
            "ventilable - corriger la facture dans Sage".format(
                sage_ttc, computed, ecart)]


def db_invoice_to_sfec(invoice):
    company = get_company_config()
    lignes = invoice.get("lignes") or []
    raw_ttc = _safe_float(invoice.get("montant_ttc"), 0)
    # Facture d'avoir saisie en DO_Type=6 avec des montants negatifs : SFEC
    # attend un creditNote a montants positifs. Le signe est porte par le
    # type de piece, jamais par les montants du payload.
    is_credit_note = (
        _map_invoice_type(invoice.get("type_doc", "vente")) == "creditNote"
        or raw_ttc < 0
    )

    items = []
    total_tax_t = 0
    total_tax_r = 0
    total_exempt = 0

    for line in lignes:
        quantity = _safe_float(line.get("quantite", 1), 1)
        if is_credit_note:
            quantity = abs(quantity)
        quantity = max(quantity, 1)
        unit_price = _safe_float(line.get("prix_unitaire", 0), 0)
        if is_credit_note:
            unit_price = abs(unit_price)
        unit_price = max(unit_price, 0)
        subtotal = round(unit_price * quantity, 2)

        # BUG (Incoherence totaux a l'envoi SFEC) : le net HT d'une ligne Sage
        # (DL_MontantHT) peut différer de DL_Qte * DL_PrixUnitaire en presence
        # d'une remise de ligne. Le code ne lisait que les champs
        # discount_amount/remise_montant (absents des lignes Sage) et
        # recalculait net=brut -> subtotal brut mais discount=0, d'ou un ecart
        # "attendu" egal a la remise de ligne. On derive la remise du net reel
        # fourni (net = brut - remise), ce qui rend la coherence des totaux
        # exacte : subtotal - total_line_discount + total_tax == total_amount.
        explicit_discount = (
            "discount_amount" in line or "remise_montant" in line
        )
        raw_net = line.get("montant_ht")
        if is_credit_note and raw_net not in (None, ""):
            raw_net = abs(_safe_float(raw_net, 0))
        if explicit_discount or raw_net in (None, ""):
            discount_amount = round(_safe_float(
                line.get("discount_amount", line.get("remise_montant", 0)), 0
            ), 2)
            if is_credit_note:
                discount_amount = abs(discount_amount)
            net_amount = round(subtotal - discount_amount, 2)
        else:
            net_amount = round(_safe_float(raw_net, subtotal), 2)
            discount_amount = round(subtotal - net_amount, 2)
            if discount_amount < 0:
                # net > brut (arrondi ou frais) : on recale le brut sur le net
                subtotal = net_amount
                discount_amount = 0.0

        discount_type = _safe_str(line.get("discount_type"), "fixed")

        rate_num, rate_str = _extract_tax_rate(line.get("taux_tva", "18"))
        tax_amount = _safe_float(line.get("montant_tva", 0), 0)
        if is_credit_note:
            tax_amount = abs(tax_amount)
        if tax_amount == 0:
            tax_amount = round(net_amount * rate_num / 100, 2)
        # SFEC attend le TTC de ligne (HT net + TVA). Les centimes et taxes
        # additionnelles sont ventiles au niveau facture : ne pas reprendre
        # DL_MontantTTC, qui les inclut deja (double comptage sinon).
        item_total = round(net_amount + tax_amount, 2)

        designation = _safe_str(line.get("description"), "")
        if not designation:
            designation = _safe_str(line.get("article_design"), "")
        if not designation:
            designation = _safe_str(line.get("designation"), "")
        if not designation:
            designation = "Article"

        classification_code = _safe_str(line.get("classification_code"), "")
        if not classification_code:
            classification_code = _safe_str(line.get("article_famille"), "")
        classification_code = classification_code or None

        type_article = _safe_str(line.get("type_article"), "")
        if type_article in ("product", "service"):
            item_type = type_article
        else:
            article_nature = line.get("article_nature")
            try:
                article_nature = int(article_nature) if article_nature else 0
            except (ValueError, TypeError):
                article_nature = 0
            item_type = "service" if article_nature == 1 else "product"

        item = {
            "designation": designation,
            "classification_code": classification_code,
            "type": item_type,
            "unit_price": round(unit_price, 2),
            "quantity": round(quantity, 2),
            "subtotal": round(net_amount, 2),
            "discount_amount": round(discount_amount, 2),
            "discount_type": discount_type,
            "amount_after_discount": round(net_amount, 2),
            "net_amount": item_total,
            "tax_rate": rate_str,
            "tax_amount": round(tax_amount, 2),
            "total_amount": item_total,
        }
        items.append(item)

        if rate_num == 5:
            total_tax_r += tax_amount
        elif rate_num == 0:
            total_exempt += net_amount
        else:
            total_tax_t += tax_amount

    if not items:
        montant_ht = _safe_float(invoice.get("montant_ht"), 0)
        montant_ttc = _safe_float(invoice.get("montant_ttc"), 0)
        if is_credit_note:
            montant_ht = abs(montant_ht)
            montant_ttc = abs(montant_ttc)

        if montant_ht <= 0 and montant_ttc > 0:
            # Repli degrade : sans lignes ni HT, on suppose le taux standard
            # 18 % + centimes (1,189). check_sage_totals() signalera les
            # factures dont le TTC n'est pas reconstructible.
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

        item_total = round(montant_ht + montant_tva, 2)
        items = [{
            "designation": "Facture",
            "classification_code": None,
            "type": "product",
            "unit_price": montant_ht,
            "quantity": 1,
            "subtotal": montant_ht,
            "discount_amount": 0,
            "discount_type": "fixed",
            "amount_after_discount": montant_ht,
            "net_amount": item_total,
            "tax_rate": ("%g" % (rate * 100)),
            "tax_amount": montant_tva,
            "total_amount": item_total,
        }]
        if rate == 0.05:
            total_tax_t = 0
            total_tax_r = montant_tva
            total_exempt = 0
        elif rate == 0:
            total_tax_t = 0
            total_tax_r = 0
            total_exempt = montant_ht
        else:
            total_tax_t = montant_tva
            total_tax_r = 0
            total_exempt = 0

    total_tax = round(total_tax_t + total_tax_r, 2)
    subtotal = round(sum(i["subtotal"] for i in items), 2)
    total_line_discount = round(sum(i["discount_amount"] for i in items), 2)

    # Centime additionnel = 5 % de la TVA collectee (decret n° 2023-1737,
    # art. 22), jamais 5 % du HT. Meme regle que recalc_invoice_totals().
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

    invoice_type = _map_invoice_type(invoice.get("type_doc", "vente"))
    if raw_ttc < 0:
        # Facture d'avoir saisie en DO_Type=6 avec des montants negatifs :
        # SFEC attend un creditNote a montants positifs.
        invoice_type = "creditNote"

    amount_due = round(_safe_float(invoice.get("montant_restant"), total_amount), 2)
    discount_amount = round(_safe_float(invoice.get("discount_amount", 0), 0), 2)

    if invoice_type == "creditNote":
        # SFEC attend un creditNote a montants POSITIFS : on normalise en
        # valeur absolue, quel que soit le signe stocke dans Sage.
        subtotal = round(abs(subtotal), 2)
        total_tax_t = round(abs(total_tax_t), 2)
        total_tax_r = round(abs(total_tax_r), 2)
        total_exempt = round(abs(total_exempt), 2)
        total_tax = round(abs(total_tax), 2)
        additional_cent_tax = round(abs(additional_cent_tax), 2)
        discount_amount = round(abs(discount_amount), 2)
        additional_taxes = [
            dict(t, tax_amount=round(abs(_safe_float(t.get("tax_amount"), 0)), 2))
            for t in additional_taxes
        ]
        for item in items:
            for key in ("unit_price", "subtotal", "discount_amount",
                        "amount_after_discount", "net_amount", "tax_amount",
                        "total_amount"):
                item[key] = round(abs(_safe_float(item.get(key), 0)), 2)
        # Les lignes ont ete normalisees : on recalcule les totaux de ligne
        # pour garantir la coherence attendue par validate_sfec_payload().
        subtotal = round(sum(i["subtotal"] for i in items), 2)
        total_line_discount = round(sum(i["discount_amount"] for i in items), 2)
        total_amount = round(
            subtotal + total_tax + additional_cent_tax
            + round(sum(_safe_float(t.get("tax_amount"), 0)
                        for t in additional_taxes), 2),
            2,
        )
        amount_due = round(abs(amount_due), 2)

    raw_niu = _safe_str(invoice.get("recipient_niu"))
    raw_phone = _safe_str(invoice.get("recipient_phone"))
    raw_email = _safe_str(invoice.get("recipient_email"))
    raw_type = _safe_str(invoice.get("recipient_type_raw"))
    raw_addr = _safe_str(invoice.get("recipient_address"))
    raw_name = _safe_str(invoice.get("nom_tiers"), "Client")

    comp_niu = _safe_str(company.get("tax_number"))
    comp_phone = _safe_str(company.get("phone"))
    comp_email = _safe_str(company.get("email"))

    recipient_name = raw_name or "Client"
    recipient_type = _map_recipient_type(raw_type)
    if not raw_niu and not comp_niu:
        if recipient_type == "business":
            recipient_type = "individual"

    devise = _safe_str(invoice.get("devise"), "")
    if not devise:
        devise = "XAF" if company.get("currency", "XAF") == "XAF" else "USD"

    sfec_req = {
        "invoice_id": _safe_str(invoice.get("numero"), invoice.get("id", "")),
        "invoice_type": invoice_type,
        "recipient_type": recipient_type,
        "recipient_name": recipient_name,
        "is_recipient_taxable": bool(invoice.get("is_recipient_taxable", True)),
        "subtotal": subtotal,
        "total_tax_t_amount": round(total_tax_t, 2),
        "total_tax_r_amount": round(total_tax_r, 2),
        "total_exempt_amount": round(total_exempt, 2),
        "total_tax_amount": total_tax,
        "discount_amount": discount_amount,
        "total_line_discount_amount": total_line_discount,
        "additional_cent_tax": additional_cent_tax,
        "electronic_stamp_duty": 0,  # regle absolue SFEC : toujours 0, jamais lu depuis invoice
        "total_amount": total_amount,
        "amount_due": amount_due,
        "currency": devise,
        "payment_method": _safe_str(invoice.get("payment_method"), "bank_transfer"),
        "items": items,
    }

    if additional_taxes:
        sfec_req["additional_taxes"] = additional_taxes

    if comp_niu:
        sfec_req["taxpayer_niu"] = comp_niu

    ref = _safe_str(invoice.get("reference"))
    if ref:
        sfec_req["invoice_subject"] = ref
        sfec_req["payment_reference"] = ref
        sfec_req["notes"] = ref

    due_date = _normalize_date(invoice.get("date_echeance"))
    if due_date:
        sfec_req["invoice_due_date"] = due_date

    if invoice_type == "creditNote":
        ref_invoice_id = _safe_str(invoice.get("reference_invoice_id"))
        if ref_invoice_id:
            sfec_req["reference_invoice_id"] = ref_invoice_id
        # L'OpenAPI SFEC exige ces deux champs en plus de
        # reference_invoice_id (doc /api/certify, CertifyInvoiceRequest).
        for key in ("external_credit_note_number",
                    "original_sfec_invoice_number"):
            value = _safe_str(invoice.get(key))
            if value:
                sfec_req[key] = value

    recipient_rccm = _safe_str(invoice.get("recipient_rccm"))
    if recipient_rccm:
        sfec_req["recipient_rccm"] = recipient_rccm

    payment_date = _normalize_date(invoice.get("payment_date"))
    if payment_date:
        sfec_req["payment_date"] = payment_date

    recipient_niu = raw_niu or comp_niu or _DEFAULT_RECIPIENT_NIU
    if recipient_niu and len(recipient_niu) not in (16, 17):
        logger.warning("NIU recipient invalide (%d chars): '%s' - utilisation du NIU compagnie", len(recipient_niu), recipient_niu)
        recipient_niu = comp_niu or _DEFAULT_RECIPIENT_NIU
    sfec_req["recipient_niu"] = recipient_niu
    sfec_req["recipient_phone"] = raw_phone or _DEFAULT_RECIPIENT_PHONE
    sfec_req["recipient_email"] = raw_email or _DEFAULT_RECIPIENT_EMAIL

    if raw_addr:
        sfec_req["recipient_address"] = raw_addr

    logger.info("SFEC payload: type=%s items=%d amount=%s",
                sfec_req["recipient_type"], len(sfec_req["items"]), sfec_req["total_amount"])

    return sfec_req

def validate_sfec_payload(payload, tolerance_amount=None):
    errors = []
    total = payload.get("total_amount", 0)
    if not total or total <= 0:
        errors.append("total_amount doit etre > 0 (actuel: {})".format(total))

    items = payload.get("items", [])
    if not items:
        errors.append("items: au moins 1 ligne requise")
    else:
        for i, item in enumerate(items):
            if item.get("total_amount", 0) <= 0:
                errors.append("items[{}].total_amount doit etre > 0".format(i))
            if not item.get("designation"):
                errors.append("items[{}].designation vide".format(i))
            if item.get("type") not in ("product", "service"):
                errors.append("items[{}].type doit etre 'product' ou 'service'".format(i))

    if not payload.get("currency"):
        errors.append("currency requis")

    if not payload.get("recipient_type"):
        errors.append("recipient_type requis")

    if not payload.get("recipient_name"):
        errors.append("recipient_name requis")

    # BUG-002 : un avoir doit obligatoirement referencer sa facture de vente
    if payload.get("invoice_type") == "creditNote":
        if not payload.get("reference_invoice_id"):
            errors.append("reference_invoice_id requis pour un avoir (creditNote)")
        if not payload.get("external_credit_note_number"):
            errors.append("external_credit_note_number requis pour un avoir")
        if not payload.get("original_sfec_invoice_number"):
            errors.append("original_sfec_invoice_number requis pour un avoir")

    # Regles complementaires (section 5 du plan) -- ajout au-dela de la demande initiale
    recipient_type = payload.get("recipient_type")
    if recipient_type in ("business", "government") and not payload.get("recipient_niu"):
        errors.append("recipient_niu requis pour recipient_type={}".format(recipient_type))
    if recipient_type == "foreign":
        for f in ("recipient_email", "recipient_phone", "recipient_address"):
            if not payload.get(f):
                errors.append("{} requis pour recipient_type=foreign".format(f))

    # BUG-001 : coherence des totaux (tolerance configurable)
    if tolerance_amount is None:
        try:
            # T2 (audit 2026-09) : la tolerance vit dans la section TOP-LEVEL
            # `validation` de la config (config.validation.tolerance_amount),
            # pas sous `company` — l'ancien lookup lisait company.validation,
            # toujours absent, et la tolerance effective restait 1.0 quel que
            # soit la config.
            tolerance_amount = float(
                get_config().get("validation", {}).get("tolerance_amount", 1)
            )
        except Exception:
            tolerance_amount = 1.0

    additional_taxes_total = round(
        sum(_safe_float(t.get("tax_amount"), 0)
            for t in (payload.get("additional_taxes") or [])),
        2
    )
    expected_total = round(
        _safe_float(payload.get("subtotal"), 0)
        + _safe_float(payload.get("total_tax_amount"), 0)
        + _safe_float(payload.get("additional_cent_tax"), 0)
        + additional_taxes_total
        + _safe_float(payload.get("electronic_stamp_duty"), 0),
        2
    )
    actual_total = _safe_float(payload.get("total_amount"), 0)
    if abs(expected_total - actual_total) > tolerance_amount:
        errors.append(
            "Incoherence totaux: total_amount={} mais attendu~={} (ecart {} > tolerance {})".format(
                actual_total, expected_total,
                round(abs(expected_total - actual_total), 2), tolerance_amount
            )
        )

    return errors

def _extract_cert_data(sfec_inv):
    cert_number = (sfec_inv.get("certification_short_signature", "")
                   or sfec_inv.get("short_signature", "")
                   or sfec_inv.get("certification_number", ""))
    sig = (sfec_inv.get("signature", "")
           or sfec_inv.get("certification_signature", "")
           or sfec_inv.get("short_signature", ""))
    qr = sfec_inv.get("qr_code", "") or sfec_inv.get("certification_qr_code", "")
    cert_date = sfec_inv.get("certification_date", "")
    return cert_number, sig, qr, cert_date


def record_certified_invoice(sage_invoice_id, sfec_inv):
    """Persiste une certification recuperee par GET (ex. apres un 409).

    `invoice_number` est conserve : c'est le numero SFEC de la facture
    d'origine, utilise comme `original_sfec_invoice_number` pour un avoir.
    Une fiche deja presente pour le meme `sage_invoice_id` est mise a jour
    au lieu d'etre dupliquee. Ne leve pas en cas d'echec d'ecriture JSON.
    """
    if not sage_invoice_id or not isinstance(sfec_inv, dict):
        return False

    cert_number, sig, qr, cert_date = _extract_cert_data(sfec_inv)
    invoice_number = str(sfec_inv.get("invoice_number") or "").strip()
    if not invoice_number and not cert_number:
        return False

    sfec_id = str(
        sfec_inv.get("sfec_id")
        or sfec_inv.get("identifier")
        or sfec_inv.get("id")
        or ""
    ).strip()
    now = datetime.utcnow().isoformat() + "Z"

    with _certified_lock:
        existing = _load_certified()
        target = None
        for item in existing:
            if not isinstance(item, dict):
                continue
            if item.get("sage_invoice_id") == sage_invoice_id:
                target = item
                break
        if target is None and invoice_number:
            for item in existing:
                if not isinstance(item, dict):
                    continue
                if item.get("invoice_number") == invoice_number:
                    target = item
                    break
        if target is None:
            target = {
                "sage_invoice_id": sage_invoice_id,
                "certified_at": now,
            }
            existing.append(target)

        if sfec_id:
            target["sfec_id"] = sfec_id
        if invoice_number:
            target["invoice_number"] = invoice_number
        if cert_number:
            target["certification_number"] = cert_number
            target["short_signature"] = cert_number[:50]
        if sig:
            target["signature"] = sig
        if qr:
            target["qr_code"] = qr
        if cert_date:
            target["certification_date"] = cert_date
        target.setdefault("certified_at", now)
        try:
            _save_certified(existing)
        except Exception as e:  # noqa: BLE001
            logger.warning("Ecriture sfec-certified.json echouee: %s", e)
            return False

    logger.info(
        "Certification SFEC memorisee pour %s (numero SFEC %s, certif %s)",
        sage_invoice_id, invoice_number or "-", (cert_number or "-")[:30],
    )
    return True


def certify_invoice(invoice):
    client = SfecClient()
    sfec_req = db_invoice_to_sfec(invoice)
    errors = validate_sfec_payload(sfec_req) + check_sage_totals(invoice, sfec_req)
    if errors:
        msg = "Validation SFEC echouee: {}".format("; ".join(errors))
        logger.error(msg)
        exc = Exception(msg)
        exc.status_code = 422
        exc.error_body = msg
        raise exc
    logger.info("SFEC: certification facture %s", sfec_req["invoice_id"])

    result = client.certify(sfec_req)

    identifier = result.get("identifier", "")
    logger.info("SFEC POST reponse: id='%s' cert_num='%s' short_sig='%s' date='%s' sig=%s qr=%s",
                identifier,
                (result.get("certification_number") or "")[:30],
                (result.get("short_signature") or "")[:30],
                result.get("certification_date", ""),
                "oui" if result.get("signature") else "non",
                "oui" if result.get("qr_code") else "non")

    if not identifier:
        logger.error("SFEC: pas d'identifier dans la reponse pour %s", sfec_req["invoice_id"])
        exc = Exception("Pas d'identifier dans la reponse SFEC")
        exc.status_code = 502
        exc.error_body = "No identifier"
        raise exc

    cert_number, sig, qr, cert_date = _extract_cert_data(result)

    if not cert_number:
        logger.info("SFEC: donnees de certification pas encore disponibles pour %s, polling...", identifier)
        for attempt in range(12):
            time.sleep(2)
            try:
                full_data = client.get_invoice(identifier)
                if full_data:
                    cert_number, sig, qr, cert_date = _extract_cert_data(full_data)
                    if cert_number:
                        logger.info("SFEC: donnees obtenues apres %ds pour %s", (attempt + 1) * 2, identifier)
                        result.update(full_data)
                        break
            except Exception as ex:
                logger.debug("SFEC: polling GET tentative %d echouee pour %s: %s", attempt + 1, identifier, ex)
        else:
            logger.error("SFEC: ECHEC - pas de donnees de certification pour %s apres polling", identifier)
            exc = Exception("Certification non finalisee sur SFEC apres delai d'attente: {}".format(identifier))
            exc.status_code = 502
            exc.error_body = "Polling timeout"
            raise exc

    logger.info("SFEC FINALES: cert_num='%s' date='%s' sig='%s' qr=%s",
                cert_number[:40] if cert_number else "(vide)",
                cert_date or "(vide)",
                sig[:30] if sig else "(vide)",
                "oui" if qr else "non")

    certified = {
        "sfec_id": identifier,
        "invoice_number": result.get("invoice_number", ""),
        "certification_number": cert_number,
        "certification_date": cert_date,
        "signature": sig,
        "short_signature": cert_number[:50] if cert_number else "",
        "qr_code": qr,
        "sage_invoice_id": invoice.get("id", ""),
        "certified_at": datetime.utcnow().isoformat() + "Z",
    }

    with _certified_lock:
        existing = _load_certified()
        existing.append(certified)
        _save_certified(existing)

    logger.info("SFEC: facture certifiee - certif: '%s', date: '%s', sig: '%s', qr: %s",
                cert_number[:30] if cert_number else "(vide)",
                cert_date or "(vide)",
                sig[:20] if sig else "(vide)",
                "oui" if qr else "non")

    return result


def get_certified_by_sage_id(sage_id):
    for c in _load_certified():
        if c.get("sage_invoice_id") == sage_id:
            return c
    return None


def get_all_certified():
    return _load_certified()


def preview_sfec_payload(invoice):
    return db_invoice_to_sfec(invoice)


def clear_failed_certifications():
    with _certified_lock:
        certified = _load_certified()
        cleaned = [c for c in certified if c.get("certification_number") and c.get("certification_number") != "DEJA_CERTIFIE"]
        removed = len(certified) - len(cleaned)
        _save_certified(cleaned)
    if removed > 0:
        logger.info("Certifications echouees supprimees: %d", removed)
    return removed


def check_health():
    client = SfecClient()
    connected = client.ping()
    return {"connected": connected, "url": client.base_url}


def sqlite_invoice_to_sfec(invoice):
    type_doc = invoice.get("type_doc", "vente")
    is_credit_note = (
        type_doc == "avoir"
        or _safe_float(invoice.get("montant_ttc"), 0) < 0
    )
    adapted = {
        "id": invoice.get("id"),
        "numero": invoice.get("numero"),
        "reference": invoice.get("reference", ""),
        "date_facture": invoice.get("date_facture", ""),
        "date_echeance": invoice.get("date_echeance"),
        "montant_ht": invoice.get("montant_ht", 0),
        "montant_tva": invoice.get("montant_tva", 0),
        "montant_ttc": invoice.get("montant_ttc", 0),
        "montant_restant": invoice.get("montant_restant", 0),  # fix: manquait, amount_due retombait toujours sur total_amount
        "recipient_niu": invoice.get("tiers_niu", ""),
        "recipient_phone": invoice.get("tiers_telephone", ""),
        "recipient_email": invoice.get("tiers_email", ""),
        "recipient_type_raw": invoice.get("tiers_type", "business"),
        "recipient_address": invoice.get("tiers_adresse", ""),
        "nom_tiers": invoice.get("tiers_nom", ""),
        "type_doc": type_doc,
        "payment_method": invoice.get("payment_method", "bank_transfer"),
        "devise": invoice.get("devise", "XAF"),
        "recipient_rccm": invoice.get("recipient_rccm", ""),
        "is_recipient_taxable": invoice.get("is_recipient_taxable", 1),
        "discount_amount": invoice.get("discount_amount", 0),
        "additional_cent_tax": invoice.get("additional_cent_tax", 0),
        "reference_invoice_id": invoice.get("reference_invoice_id", ""),
        "external_credit_note_number": invoice.get("external_credit_note_number", ""),
        "original_sfec_invoice_number": invoice.get("original_sfec_invoice_number", ""),
        "payment_date": invoice.get("payment_date", ""),
    }
    if is_credit_note:
        # Repli pour les avoirs locaux crees avant l'ajout des champs SFEC :
        # le numero ERP de l'avoir et celui de la facture d'origine sont
        # connus localement. Ne jamais utiliser la signature de certification.
        adapted["external_credit_note_number"] = (
            adapted["external_credit_note_number"]
            or _safe_str(invoice.get("numero"))
        )
        adapted["original_sfec_invoice_number"] = (
            adapted["original_sfec_invoice_number"]
            or adapted["reference_invoice_id"]
        )

    lignes_adaptees = []
    for l in invoice.get("lignes", []):
        lignes_adaptees.append({
            "description": l.get("designation", ""),
            "article_design": l.get("designation", ""),
            "designation": l.get("designation", ""),
            "article_famille": l.get("famille", l.get("code_article", "")),
            "classification_code": l.get("classification_code", ""),
            "article_nature": 0,
            "type_article": l.get("type_article", "product"),
            "quantite": l.get("quantite", 1),
            "prix_unitaire": l.get("prix_unitaire", 0),
            "montant_ht": l.get("montant_ht", 0),
            "montant_tva": l.get("montant_tva", 0),
            "montant_ttc": l.get("montant_ttc", 0),
            "taux_tva": str(l.get("taux_tva", 18)),
            "discount_amount": l.get("remise_montant", 0),
            "discount_type": l.get("discount_type", "fixed"),
            "subtotal": l.get("subtotal", 0),
        })
    adapted["lignes"] = lignes_adaptees

    return db_invoice_to_sfec(adapted)

def certify_sqlite_invoice(invoice):
    sfec_req = sqlite_invoice_to_sfec(invoice)
    errors = validate_sfec_payload(sfec_req) + check_sage_totals(invoice, sfec_req)
    if errors:
        msg = "Validation SFEC: {}".format("; ".join(errors))
        raise Exception(msg)

    client = SfecClient()
    recovered = False
    try:
        result = client.certify(sfec_req)
    except Exception as e:
        if getattr(e, "status_code", None) == 409 and invoice.get("numero"):
            logger.info("SFEC: %s deja certifiee (409) - recuperation des donnees...", invoice.get("numero"))
            existing = client.verify_by_invoice_number(str(invoice.get("numero")))
            if existing:
                existing = dict(existing)
                existing["identifier"] = (existing.get("sfec_id")
                                          or existing.get("identifier")
                                          or existing.get("id")
                                          or "DEJA_CERTIFIE")
                result = existing
                recovered = True
            else:
                raise
        else:
            raise

    identifier = result.get("identifier", "")
    if not identifier:
        raise Exception("Pas d'identifier dans la reponse SFEC")

    cert_number, sig, qr, cert_date = _extract_cert_data(result)

    if not cert_number:
        for attempt in range(12):
            time.sleep(2)
            try:
                full_data = client.get_invoice(identifier)
                if full_data:
                    cert_number, sig, qr, cert_date = _extract_cert_data(full_data)
                    if cert_number:
                        try:
                            result.update(full_data)
                        except Exception:
                            result = full_data
                        break
            except Exception:
                pass

    from app.storage import db as sqlite_db
    if not cert_number:
        sqlite_db.update_invoice_sfec(
            invoice["id"],
            sfec_id=identifier,
            statut="EN_COURS"
        )
        logger.warning("SFEC: certification en cours pour %s (id %s)", invoice.get("numero"), identifier)
        return {
            "success": True,
            "identifier": identifier,
            "certification_number": "",
            "certification_date": "",
            "signature": "",
            "qr_code": "",
        }

    sqlite_db.update_invoice_sfec(
        invoice["id"],
        sfec_id=identifier,
        certification_number=cert_number,
        signature=sig or "",
        qr_code=qr or "",
        certification_date=cert_date or "",
        statut="DEJA_CERTIFIE" if recovered else "CERTIFIE"
    )

    return {
        "success": True,
        "identifier": identifier,
        "certification_number": cert_number,
        "certification_date": cert_date or "",
        "signature": sig or "",
        "qr_code": qr or "",
    }
