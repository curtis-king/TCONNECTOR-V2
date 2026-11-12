import re
import json
import os
import time
import uuid
import logging
import threading
from datetime import date, datetime, time as datetime_time, timezone
from app.config.manager import get_company_config, get_config
from app.core.utils import is_counter_customer
from app.domain.taxes import (
    ITEM_ROLES,
    classify_line_rate,
    compute_tax_breakdown,
    resolve_additional_tax,
    round_amount,
    snap_rate,
)
from app.integration.sfec.client import SfecClient
from app.storage.db import cent_tax_rate

_TYPE_DOC_TO_SFEC = {
    "vente": "salesInvoice",
    "avoir": "creditNote",
    "avoir_compte": "creditNoteOnAccount",
}

_ALLOWED_VAT_RATES = (0.0, 5.0, 18.0)
_ALLOWED_CURRENCIES = {"XAF", "USD", "EUR"}
_PAYMENT_METHODS = {
    "cash", "bank_transfer", "card", "mobile_money", "cheque", "credit",
}
_PAYMENT_METHOD_ALIASES = {
    "cash": "cash",
    "espece": "cash",
    "especes": "cash",
    "espèce": "cash",
    "espèces": "cash",
    "check": "cheque",
    "cheque": "cheque",
    "chèque": "cheque",
    "bank": "bank_transfer",
    "bank_transfer": "bank_transfer",
    "transfer": "bank_transfer",
    "virement": "bank_transfer",
    "card": "card",
    "carte": "card",
    "cb": "card",
    "mobile": "mobile_money",
    "mobile_money": "mobile_money",
    "momo": "mobile_money",
    "credit": "credit",
    "credit_note": "credit",
    "avoir": "credit",
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
    if tax_rate_str is None or tax_rate_str == "":
        return 18, "18"
    clean = str(tax_rate_str).replace(",", ".")
    match = re.search(r"(\d+\.?\d*)", clean)
    rate = round(float(match.group(1)), 2) if match else 0.0
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


def _sfec_config():
    try:
        return get_config().get("sfec", {}) or {}
    except Exception:
        return {}


def map_payment_method(raw_value):
    """Mappe un mode de paiement Sage/POS vers l'enum texte SFEC.

    Les codes numeriques (`DO_ModeReglement`) ne sont pas devines : ils
    doivent etre declares dans `sfec.payment_method_map`. Une valeur inconnue
    retourne None et sera rejetee par `validate_sfec_payload()`.
    """
    value = _safe_str(raw_value)
    if not value:
        return None
    key = value.strip().lower()
    mapped = _PAYMENT_METHOD_ALIASES.get(key)
    if mapped:
        return mapped

    cfg_map = _sfec_config().get("payment_method_map") or {}
    if isinstance(cfg_map, dict):
        configured = cfg_map.get(value) or cfg_map.get(key)
        if configured is not None:
            candidate = _safe_str(configured).strip().lower()
            if candidate in _PAYMENT_METHODS:
                return candidate
    return None


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


def _as_bool(val, default=False):
    if val is None:
        return default
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "oui", "yes", "on")
    return bool(val)


def _normalize_datetime(val):
    """Normalise une date/datetime en RFC 3339 UTC.

    SFEC stocke `invoice_due_date`/`payment_date` seulement si la valeur
    contient une heure. Une date seule doit donc devenir
    `YYYY-MM-DDT00:00:00Z`, sinon elle est silencieusement perdue.
    """
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        dt = val
    elif isinstance(val, date):
        dt = datetime.combine(val, datetime_time.min)
    else:
        text = str(val).strip()
        if not text or text.startswith("1900-01-01"):
            return None
        text = text.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            dt = None
            for fmt in (
                "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%Y %H:%M:%S",
                "%d-%m-%Y %H:%M:%S", "%Y/%m/%d", "%Y/%m/%d %H:%M:%S",
            ):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Compatibilite avec les appelants historiques.
def _normalize_date(val):
    return _normalize_datetime(val)


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
    """Deduit les taxes additionnelles du TTC Sage (wrapper historique).

    Le calcul complet est fait par `app.domain.taxes` (ventilation TVA /
    centime / taxes supplementaires a partir des lignes et du TTC). Cette
    fonction reste disponible pour les appelants historiques : elle isole
    le residu apres TVA et centimes et le qualifie avec le code SFEC
    configure (`validation.additional_tax_codes`, code 203 pour le 3 %).
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

    # Les arrondis de Sage transforment 3 % en 2,99 % : on rehausse les taux
    # quasi entiers, sans toucher aux taux fractionnaires (0,25 %).
    rate = snap_rate(extra / subtotal * 100.0)
    if max_rate and rate > max_rate:
        return []

    code = _safe_str(invoice.get("additional_tax_code"), "")
    label = _safe_str(invoice.get("additional_tax_label"), "")
    if not code or not label:
        default_code, default_label = resolve_additional_tax(rate, cfg)
        code = code or default_code
        label = label or default_label

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

    # Une facture exoneree (TTC = HT) est valide : pas de blocage
    # "exoneration a confirmer" (validation directe, demande client).

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
    sfec_cfg = get_config().get("sfec", {})
    lignes = invoice.get("lignes") or []
    raw_ttc = _safe_float(invoice.get("montant_ttc"), 0)
    is_credit_note = (
        _map_invoice_type(invoice.get("type_doc", "vente")) in (
            "creditNote", "creditNoteOnAccount")
        or raw_ttc < 0
    )
    is_on_account = (
        _as_bool(invoice.get("credit_note_on_account"))
        or _safe_str(invoice.get("sfec_invoice_type")) == "creditNoteOnAccount"
        or invoice.get("type_doc") == "avoir_compte"
    )

    devise = _safe_str(invoice.get("devise"), "")
    if not devise:
        devise = "XAF" if company.get("currency", "XAF") == "XAF" else "USD"

    items = []
    line_records = []
    item_record_indexes = []

    for line in lignes:
        quantity = _safe_float(line.get("quantite", 1), 1)
        if is_credit_note:
            quantity = abs(quantity)
        unit_price = _safe_float(line.get("prix_unitaire", 0), 0)
        if is_credit_note:
            unit_price = abs(unit_price)
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
        role = classify_line_rate(rate_num)

        # Une ligne de taxe (centime 0,9 %, taxe supplementaire 3 %...) n'est
        # pas un article : elle ne doit ni gonfler le HT ni les items SFEC.
        # Son montant est repris au niveau facture (centime 5 % de la TVA +
        # taxes supplementaires) a partir du TTC Sage.
        line_records.append({
            "net": net_amount,
            "rate": rate_num,
            "kind": role,
            "line": line,
        })
        if role not in ITEM_ROLES:
            continue

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
            # La TVA est allouee apres coup, par taux (voir ventilation
            # fiscale ci-dessous) : la somme des items vaut exactement le
            # total de TVA de la facture.
            "net_amount": round(net_amount, 2),
            "tax_rate": rate_str,
            "tax_amount": 0.0,
            "total_amount": round(net_amount, 2),
        }
        items.append(item)
        item_record_indexes.append(len(line_records) - 1)

    # Ventilation fiscale : TVA par taux, centime (5 % de la TVA collectee)
    # et taxes supplementaires (3 %...), le total recalcule doit retomber sur
    # le TTC Sage (source de verite).
    breakdown = compute_tax_breakdown(
        invoice,
        line_records,
        config=_validation_config(),
        currency=devise,
        cent_rate=cent_tax_rate(),
    )
    for message in breakdown.get("errors") or []:
        logger.warning("Ventilation fiscale impossible (%s): %s",
                       invoice.get("numero", "?"), message)
    for message in breakdown.get("warnings") or []:
        logger.info("Ventilation fiscale (%s): %s",
                    invoice.get("numero", "?"), message)

    if not items:
        # Repli degrade : sans lignes, on synthetise une ligne unique depuis
        # l'en-tete. check_sage_totals() signalera les factures dont le TTC
        # n'est pas reconstructible.
        synthetic = breakdown.get("synthetic") or {}
        net_amount = round_amount(
            synthetic.get("net", 0), breakdown.get("decimals", 2))
        tax_amount = round_amount(
            synthetic.get("tax_amount", 0), breakdown.get("decimals", 2))
        rate = synthetic.get("rate", 0) or 0
        items = [{
            "designation": "Facture",
            "classification_code": None,
            "type": "product",
            "unit_price": net_amount,
            "quantity": 1,
            "subtotal": net_amount,
            "discount_amount": 0,
            "discount_type": "fixed",
            "amount_after_discount": net_amount,
            "net_amount": round(net_amount + tax_amount, 2),
            "tax_rate": ("%g" % rate),
            "tax_amount": tax_amount,
            "total_amount": round(net_amount + tax_amount, 2),
        }]

    line_tax_amounts = breakdown.get("line_tax_amounts") or []
    for position, record_index in enumerate(item_record_indexes):
        if record_index >= len(line_tax_amounts):
            continue
        tax_amount = line_tax_amounts[record_index]
        item = items[position]
        item["tax_amount"] = tax_amount
        item["net_amount"] = round(item["subtotal"] + tax_amount, 2)
        item["total_amount"] = item["net_amount"]

    total_tax_t = breakdown["total_tax_t_amount"]
    total_tax_r = breakdown["total_tax_r_amount"]
    total_exempt = breakdown["total_exempt_amount"]
    total_tax = breakdown["total_tax_amount"]
    subtotal = round(sum(i["subtotal"] for i in items), 2)
    total_line_discount = round(sum(i["discount_amount"] for i in items), 2)

    # Centime additionnel = 5 % de la TVA collectee (decret n° 2023-1737,
    # art. 22), jamais 5 % du HT. Meme regle que recalc_invoice_totals().
    additional_cent_tax = breakdown["additional_cent_tax"]

    # Taxes supplementaires (3 %, 0,25 %, TAR, TAX_MUNI...) : hors
    # total_tax_amount, dans `additional_taxes`.
    additional_taxes = breakdown["additional_taxes"]
    additional_taxes_total = breakdown["additional_taxes_total"]

    total_amount = breakdown["total_amount"]

    invoice_type = _map_invoice_type(invoice.get("type_doc", "vente"))
    if is_on_account:
        invoice_type = "creditNoteOnAccount"
    elif raw_ttc < 0:
        # Facture d'avoir saisie en DO_Type=6 avec des montants negatifs :
        # SFEC attend un creditNote a montants positifs.
        invoice_type = "creditNote"

    amount_due = round(_safe_float(invoice.get("montant_restant"), total_amount), 2)
    discount_amount = round(_safe_float(invoice.get("discount_amount", 0), 0), 2)

    if invoice_type in ("creditNote", "creditNoteOnAccount"):
        # SFEC attend un avoir a montants POSITIFS : on normalise en
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

    raw_niu = _safe_str(invoice.get("recipient_niu")).upper().replace(" ", "")
    raw_phone = _safe_str(invoice.get("recipient_phone"))
    raw_email = _safe_str(invoice.get("recipient_email"))
    raw_type = _safe_str(invoice.get("recipient_type_raw"))
    raw_addr = _safe_str(invoice.get("recipient_address"))
    raw_name = _safe_str(
        invoice.get("nom_tiers") or invoice.get("tiers_nom"))

    comp_niu = _safe_str(company.get("tax_number"))

    recipient_name = raw_name
    recipient_type = _map_recipient_type(raw_type)
    counter_customer = is_counter_customer(
        invoice.get("tiers_code") or invoice.get("code_tiers"),
        raw_name,
        sfec_cfg,
    )
    if counter_customer:
        # Client comptoir / anonyme : particulier sans NIU. On ne modifie
        # aucune donnee client et on n'injecte jamais le NIU entreprise.
        recipient_type = "individual"

    sfec_req = {
        "invoice_id": _safe_str(invoice.get("numero"), invoice.get("id", "")),
        "invoice_type": invoice_type,
        "recipient_type": recipient_type,
        "recipient_name": recipient_name,
        "is_recipient_taxable": _as_bool(
            invoice.get("is_recipient_taxable"), True),
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
        "payment_method": (
            map_payment_method(invoice.get("payment_method"))
            or _safe_str(invoice.get("payment_method"))
            or "bank_transfer"
        ),
        "items": items,
    }

    if additional_taxes:
        sfec_req["additional_taxes"] = additional_taxes

    if comp_niu:
        sfec_req["taxpayer_niu"] = comp_niu

    ref = _safe_str(invoice.get("reference"))
    if ref:
        sfec_req["invoice_subject"] = ref[:100]
        sfec_req["payment_reference"] = ref[:100]
        sfec_req["notes"] = ref[:500]

    due_date = _normalize_datetime(invoice.get("date_echeance"))
    if due_date:
        sfec_req["invoice_due_date"] = due_date

    if invoice_type in ("creditNote", "creditNoteOnAccount"):
        ref_invoice_id = _safe_str(invoice.get("reference_invoice_id"))
        if not ref_invoice_id and invoice_type == "creditNoteOnAccount":
            # Reference unique de l'avoir sur compte : par defaut son numero.
            ref_invoice_id = sfec_req["invoice_id"]
        if ref_invoice_id:
            sfec_req["reference_invoice_id"] = ref_invoice_id
        if invoice_type == "creditNote":
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

    payment_date = _normalize_datetime(invoice.get("payment_date"))
    if payment_date:
        sfec_req["payment_date"] = payment_date

    if raw_niu:
        sfec_req["recipient_niu"] = raw_niu
    if raw_phone:
        sfec_req["recipient_phone"] = raw_phone
    if raw_email:
        sfec_req["recipient_email"] = raw_email
    if raw_addr:
        sfec_req["recipient_address"] = raw_addr

    logger.info(
        "SFEC payload: type=%s counter=%s recipient_niu=%s items=%d amount=%s",
        sfec_req["recipient_type"], counter_customer,
        "present" if raw_niu else "absent",
        len(sfec_req["items"]), sfec_req["total_amount"])

    return sfec_req

def validate_sfec_payload(payload, tolerance_amount=None):
    """Valide le payload avant tout appel HTTP a SFEC.

    L'API SFEC est volontairement permissive (elle accepte des taux, des
    quantites ou des totaux invalides). Le connecteur doit donc refuser ces
    cas localement, avec un motif exploitable par l'operateur.
    """
    errors = []
    total = _safe_float(payload.get("total_amount"), 0)
    if total <= 0:
        errors.append("total_amount doit etre > 0 (actuel: {})".format(total))

    if tolerance_amount is None:
        try:
            tolerance_amount = float(
                get_config().get("validation", {}).get("tolerance_amount", 1)
            )
        except Exception:
            tolerance_amount = 1.0
    tolerance_amount = max(0.0, _safe_float(tolerance_amount, 1.0))

    invoice_type = _safe_str(payload.get("invoice_type"))
    if invoice_type not in ("salesInvoice", "creditNote", "creditNoteOnAccount"):
        errors.append("invoice_type invalide: {!r}".format(invoice_type))

    currency = _safe_str(payload.get("currency")).upper()
    if not currency:
        errors.append("currency requis")
    elif currency not in _ALLOWED_CURRENCIES:
        errors.append("currency non supportee: {}".format(currency))

    recipient_type = _safe_str(payload.get("recipient_type"))
    if recipient_type not in ("business", "individual", "government", "foreign"):
        errors.append("recipient_type invalide: {!r}".format(recipient_type))

    payment_method = _safe_str(payload.get("payment_method")).lower()
    if payment_method not in _PAYMENT_METHODS:
        errors.append(
            "payment_method inconnu: {!r} (valeurs: {})".format(
                payment_method, ", ".join(sorted(_PAYMENT_METHODS)))
        )

    recipient_name = _safe_str(payload.get("recipient_name"))
    if not recipient_name:
        errors.append("recipient_name requis")
    elif len(recipient_name) > 100:
        errors.append("recipient_name trop long: {} > 100".format(len(recipient_name)))

    recipient_niu = _safe_str(payload.get("recipient_niu"))
    if recipient_niu and len(recipient_niu) not in (16, 17):
        errors.append("recipient_niu doit contenir 16 ou 17 caracteres")
    if recipient_type in ("business", "government") and not recipient_niu:
        errors.append("recipient_niu requis pour recipient_type={}".format(recipient_type))

    if recipient_type in ("government", "foreign"):
        for field in ("recipient_phone", "recipient_email"):
            if not _safe_str(payload.get(field)):
                errors.append("{} requis pour recipient_type={}".format(field, recipient_type))

    for field, limit in (
        ("invoice_subject", 100),
        ("payment_reference", 100),
        ("notes", 500),
        ("recipient_address", 255),
        ("external_credit_note_number", 100),
    ):
        value = _safe_str(payload.get(field))
        if value and len(value) > limit:
            errors.append("{} trop long: {} > {}".format(field, len(value), limit))

    items = payload.get("items", [])
    sum_subtotal = 0.0
    sum_discount = 0.0
    sum_line_total = 0.0
    if not items:
        errors.append("items: au moins 1 ligne requise")
    else:
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append("items[{}] invalide".format(i))
                continue
            unit_price = _safe_float(item.get("unit_price"), 0)
            quantity = _safe_float(item.get("quantity"), 0)
            subtotal = _safe_float(item.get("subtotal"), 0)
            discount = _safe_float(item.get("discount_amount"), 0)
            tax_amount = _safe_float(item.get("tax_amount"), 0)
            net_amount = _safe_float(item.get("net_amount"), 0)
            total_amount = _safe_float(item.get("total_amount"), 0)
            rate = _safe_float(item.get("tax_rate"), -1)
            after_discount = _safe_float(
                item.get("amount_after_discount"), subtotal)
            designation = _safe_str(item.get("designation"))

            if unit_price < 1:
                errors.append(
                    "items[{}].unit_price doit etre >= 1 (actuel: {})".format(
                        i, unit_price))
            if quantity < 1:
                errors.append(
                    "items[{}].quantity doit etre >= 1 (actuel: {})".format(
                        i, quantity))
            if rate not in _ALLOWED_VAT_RATES:
                errors.append(
                    "items[{}].tax_rate invalide: {} (attendu 0, 5 ou 18)".format(
                        i, item.get("tax_rate")))
            if subtotal < 0:
                errors.append("items[{}].subtotal negatif".format(i))
            if discount < 0:
                errors.append("items[{}].discount_amount negatif".format(i))
            if discount > subtotal + tolerance_amount:
                errors.append(
                    "items[{}].discount_amount > subtotal".format(i))
            if abs(after_discount - subtotal) > tolerance_amount:
                errors.append(
                    "items[{}].amount_after_discount != subtotal".format(i))
            if tax_amount < 0:
                errors.append("items[{}].tax_amount negatif".format(i))
            expected_line_net = round(subtotal + tax_amount, 2)
            if abs(net_amount - expected_line_net) > tolerance_amount:
                errors.append(
                    "items[{}].net_amount != subtotal + tax_amount".format(i))
            if abs(total_amount - net_amount) > tolerance_amount:
                errors.append(
                    "items[{}].total_amount != net_amount".format(i))
            if total_amount <= 0:
                errors.append("items[{}].total_amount doit etre > 0".format(i))
            if not designation:
                errors.append("items[{}].designation vide".format(i))
            elif len(designation) > 255:
                errors.append("items[{}].designation trop long".format(i))
            if item.get("type") not in ("product", "service"):
                errors.append(
                    "items[{}].type doit etre 'product' ou 'service'".format(i))
            sum_subtotal += subtotal
            sum_discount += discount
            sum_line_total += total_amount

        if abs(sum_subtotal - _safe_float(payload.get("subtotal"), 0)) > tolerance_amount:
            errors.append(
                "subtotal {} != somme des lignes {}".format(
                    payload.get("subtotal"), round(sum_subtotal, 2)))
        if abs(sum_discount - _safe_float(
                payload.get("total_line_discount_amount"), 0)) > tolerance_amount:
            errors.append(
                "total_line_discount_amount != somme des remises de ligne")

    total_tax = _safe_float(payload.get("total_tax_amount"), 0)
    total_tax_t = _safe_float(payload.get("total_tax_t_amount"), 0)
    total_tax_r = _safe_float(payload.get("total_tax_r_amount"), 0)
    if abs(total_tax - (total_tax_t + total_tax_r)) > tolerance_amount:
        errors.append(
            "total_tax_amount != total_tax_t_amount + total_tax_r_amount")

    total_exempt = _safe_float(payload.get("total_exempt_amount"), 0)
    if total_exempt < 0:
        errors.append("total_exempt_amount negatif")
    if total_tax_t < 0 or total_tax_r < 0:
        errors.append("total_tax_t_amount/total_tax_r_amount negatifs")

    additional_taxes_total = round(
        sum(_safe_float(t.get("tax_amount"), 0)
            for t in (payload.get("additional_taxes") or [])),
        2
    )
    expected_items_total = round(
        sum_line_total
        + _safe_float(payload.get("additional_cent_tax"), 0)
        + additional_taxes_total,
        2
    )
    if abs(expected_items_total - total) > tolerance_amount:
        errors.append(
            "total_amount={} != somme des lignes + centimes + taxes "
            "additionnelles={}".format(total, expected_items_total)
        )
    expected_total = round(
        _safe_float(payload.get("subtotal"), 0)
        + total_tax
        + _safe_float(payload.get("additional_cent_tax"), 0)
        + additional_taxes_total
        + _safe_float(payload.get("electronic_stamp_duty"), 0),
        2
    )
    if abs(expected_total - total) > tolerance_amount:
        errors.append(
            "Incoherence totaux: total_amount={} mais attendu~={} (ecart {} > tolerance {})".format(
                total, expected_total,
                round(abs(expected_total - total), 2), tolerance_amount
            )
        )

    amount_due = _safe_float(payload.get("amount_due"), 0)
    if amount_due < 0:
        errors.append("amount_due negatif")
    if amount_due > total + tolerance_amount:
        errors.append("amount_due > total_amount")

    if invoice_type == "creditNote":
        if not _safe_str(payload.get("reference_invoice_id")):
            errors.append("reference_invoice_id requis pour un avoir (creditNote)")
        if not _safe_str(payload.get("external_credit_note_number")):
            errors.append("external_credit_note_number requis pour un avoir")
        if not _safe_str(payload.get("original_sfec_invoice_number")):
            errors.append("original_sfec_invoice_number requis pour un avoir")
    elif invoice_type == "creditNoteOnAccount":
        if not _safe_str(payload.get("reference_invoice_id")):
            errors.append(
                "reference_invoice_id requis pour un creditNoteOnAccount")

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


def _canonical_sfec_id(sfec_inv):
    """Identifiant GET canonique : UUID/detail, pas la signature courte."""
    if not isinstance(sfec_inv, dict):
        return ""
    return str(
        sfec_inv.get("sfec_id")
        or sfec_inv.get("id")
        or sfec_inv.get("identifier")
        or ""
    ).strip()


def _is_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def _certification_lookup_keys(invoice_number, sfec_inv):
    keys = []
    for value in (
        invoice_number,
        sfec_inv.get("invoice_number", "") if isinstance(sfec_inv, dict) else "",
        sfec_inv.get("identifier", "") if isinstance(sfec_inv, dict) else "",
        sfec_inv.get("id", "") if isinstance(sfec_inv, dict) else "",
    ):
        key = str(value or "").strip()
        if key and key not in keys:
            keys.append(key)
    return keys


def _hydrate_certification_result(client, invoice_number, result,
                                  attempts=12, delay=2):
    """Complete une reponse POST SFEC avec le detail GET.

    Certains environnements retournent dans `identifier` la signature courte
    (`certification_short_signature`) au lieu de l'UUID de la facture. Un GET
    sur cette signature courte renvoie 404. On interroge donc d'abord le
    numero de facture, qui accepte aussi le detail, puis l'identifiant
    retourne.
    """
    result = dict(result or {})
    if invoice_number and not result.get("invoice_number"):
        result["invoice_number"] = str(invoice_number)
    cert_number, sig, qr, cert_date = _extract_cert_data(result)
    sfec_id = _canonical_sfec_id(result)
    if cert_number and _is_uuid(sfec_id):
        result["sfec_id"] = sfec_id
        result["identifier"] = sfec_id
        return result, cert_number, sig, qr, cert_date, sfec_id

    # Le POST peut deja contenir la signature courte sans UUID. Dans ce cas
    # on interroge quand meme le detail par numero pour conserver l'UUID.
    keys = _certification_lookup_keys(invoice_number, result)
    for attempt in range(max(1, int(attempts or 1))):
        if attempt:
            time.sleep(delay)
        for key in keys:
            try:
                full_data = client.get_invoice(key)
            except Exception as exc:
                logger.debug(
                    "SFEC: hydratation GET %s tentative %d echouee: %s",
                    key, attempt + 1, exc)
                continue
            if not isinstance(full_data, dict) or not full_data:
                continue
            full_cert = _extract_cert_data(full_data)
            if not full_cert[0] and not cert_number:
                continue
            merged = dict(result)
            merged.update(full_data)
            sfec_id = _canonical_sfec_id(full_data) or sfec_id
            if sfec_id:
                merged["sfec_id"] = sfec_id
                merged["identifier"] = sfec_id
            logger.info(
                "SFEC: detail certification recupere pour %s apres %ds",
                invoice_number or key, attempt * delay)
            if not full_cert[0]:
                # Detail sans signature : on garde la signature du POST mais
                # on recupere l'UUID pour les controles ulterieurs.
                return (merged, cert_number, sig, qr, cert_date, sfec_id)
            return (merged, full_cert[0], full_cert[1], full_cert[2],
                    full_cert[3], sfec_id)

    if sfec_id:
        result["sfec_id"] = sfec_id
        result["identifier"] = sfec_id
    return result, cert_number, sig, qr, cert_date, sfec_id


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
    result, cert_number, sig, qr, cert_date, identifier = (
        _hydrate_certification_result(
            client, sfec_req["invoice_id"], result)
    )

    logger.info(
        "SFEC POST/GET reponse: id='%s' cert_num='%s' date='%s' sig=%s qr=%s",
        identifier,
        (cert_number or "")[:30],
        cert_date or "",
        "oui" if sig else "non",
        "oui" if qr else "non")

    if not identifier:
        logger.error("SFEC: pas d'identifier dans la reponse pour %s", sfec_req["invoice_id"])
        exc = Exception("Pas d'identifier dans la reponse SFEC")
        exc.status_code = 502
        exc.error_body = "No identifier"
        raise exc

    if not cert_number:
        logger.error("SFEC: ECHEC - pas de donnees de certification pour %s", identifier)
        exc = Exception("Certification non finalisee sur SFEC apres delai d'attente: {}".format(identifier))
        exc.status_code = 502
        exc.error_body = "Polling timeout"
        raise exc

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
        type_doc in ("avoir", "avoir_compte")
        or _as_bool(invoice.get("credit_note_on_account"))
        or _safe_float(invoice.get("montant_ttc"), 0) < 0
    )
    adapted = {
        "id": invoice.get("id"),
        "numero": invoice.get("numero"),
        "tiers_code": invoice.get("tiers_code", ""),
        "reference": invoice.get("reference", ""),
        "date_facture": invoice.get("date_facture", ""),
        "date_echeance": invoice.get("date_echeance"),
        "montant_ht": invoice.get("montant_ht", 0),
        "montant_tva": invoice.get("montant_tva", 0),
        "montant_ttc": invoice.get("montant_ttc", 0),
        "montant_restant": invoice.get("montant_restant"),
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
        "credit_note_on_account": invoice.get("credit_note_on_account", 0),
        "payment_date": invoice.get("payment_date", ""),
    }
    if is_credit_note and _as_bool(adapted.get("credit_note_on_account")):
        adapted["reference_invoice_id"] = (
            adapted["reference_invoice_id"]
            or _safe_str(invoice.get("numero"))
        )
    elif is_credit_note:
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

def _sfec_error_payload(exc):
    body = getattr(exc, "error_json", None)
    if isinstance(body, dict):
        return body
    raw = getattr(exc, "error_body", "") or ""
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _on_account_conflict(exc):
    if int(getattr(exc, "status_code", 0) or 0) != 409:
        return None
    body = _sfec_error_payload(exc)
    reference = _safe_str(body.get("reference"))
    message = _safe_str(body.get("error") or body.get("message")).lower()
    if reference and ("account" in message or "reference" in message):
        return reference
    return None


def certify_sqlite_invoice(invoice):
    sfec_req = sqlite_invoice_to_sfec(invoice)
    errors = validate_sfec_payload(sfec_req) + check_sage_totals(invoice, sfec_req)
    if errors:
        msg = "Validation SFEC: {}".format("; ".join(errors))
        exc = Exception(msg)
        exc.status_code = 422
        exc.error_body = msg
        raise exc

    client = SfecClient()
    recovered = False
    try:
        result = client.certify(sfec_req)
    except Exception as e:
        from app.storage import db as sqlite_db
        status = int(getattr(e, "status_code", 0) or 0)
        body = _sfec_error_payload(e)
        if status == 400 and any(
                key in body for key in ("already_credited", "invoice_net", "remaining")):
            msg = "SFEC 400: avoir sur-solde: deja credite {}, facture {}, reste {} ({})".format(
                body.get("already_credited", "?"),
                body.get("invoice_net", "?"),
                body.get("remaining", "?"),
                body.get("error", ""),
            ).strip()
            try:
                sqlite_db.record_sfec_anomaly(invoice.get("numero", ""), msg)
                sqlite_db.update_invoice_sfec(invoice["id"], statut="A_SURVEILLER")
            except Exception:
                pass
            exc = Exception(msg)
            exc.status_code = 400
            exc.error_body = msg
            raise exc
        reference = _on_account_conflict(e)
        if reference:
            msg = "Reference d'avoir sur compte deja utilisee: {}".format(reference)
            try:
                sqlite_db.record_sfec_anomaly(invoice.get("numero", ""), msg)
                sqlite_db.update_invoice_sfec(invoice["id"], statut="A_SURVEILLER")
            except Exception:
                pass
            exc = Exception(msg)
            exc.status_code = 409
            exc.error_body = msg
            raise exc
        if status == 409 and invoice.get("numero"):
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

    result, cert_number, sig, qr, cert_date, identifier = (
        _hydrate_certification_result(
            client, sfec_req["invoice_id"], result)
    )
    if not identifier:
        raise Exception("Pas d'identifier dans la reponse SFEC")

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
