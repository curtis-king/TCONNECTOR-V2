"""Blueprint dashboard — Pages tableau de bord : / /invoices /pending
/certified /certified/<id>/print /sales /ready /health.

Migré depuis app/web/parking/dashboard_pages.py (étape A4). Les handlers
sont strictement inchangés ; les URL / méthodes HTTP sont identiques aux
app.add_url_rule() historiques de app/web/dashboard.py. Le wrapping @_login_required est reproduit à l'identique, désormais via
un import direct de app.web.auth.security (aucun import circulaire : la
sécurité ne dépend d'aucun module de routes).
"""

import calendar
import functools
import os
import time

from app.core.utils import fmt_money
from flask import Blueprint, jsonify, render_template, request
from app.config.manager import get_config
from app.domain import invoices as invoice_engine
from app.domain import pos as pos_engine
from app.domain import print_copies
from app.domain.pdf import (
    CGV_TEXT,
    CGR_TEXT,
    DOC_TITLE_AVOIR,
    DOC_TITLE_FACTURE,
    EURO_PARITY,
    PARITE_EURO_L1,
    PARITE_EURO_L2,
    VALIDITE_OFFRE,
    barcode_svg,
    company_footer_blocks,
    company_image_path,
    fmt_rate,
    fr_date,
    fr_date_long,
    frn_amount,
    image_data_uri,
    invoice_footer,
    invoice_operator,
    montant_en_lettres,
    payment_label,
    tirage_stamp,
)
from app.integration.sfec.client import SfecClient
from app.integration.sfec.endpoints import check_health
from app.sync.connectivity import get_status
from app.sync.engine import get_cache, get_metrics, get_retry_queue
from app.web.common import _esc, _page
from app.web.auth.security import _get_csrf_token, _login_required, logger


bp = Blueprint('dashboard', __name__)


@bp.route("/", methods=["GET"])
@_login_required
def index():
    m = get_metrics()
    sfec = check_health()
    conn = get_status()
    retry_q = get_retry_queue()

    cache = get_cache()
    val_count = sum(1 for inv in cache.get("sales_invoices", []) if inv.get("valide", 0) == 1)

    db_ok = "badge-ok" if m.get("db_connected") else "badge-err"
    db_txt = "Connectee" if m.get("db_connected") else "Deconnectee"
    sfec_ok = "badge-ok" if sfec.get("connected") else "badge-err"
    sfec_txt = "Connectee" if sfec.get("connected") else "Deconnectee"
    net_cls = "badge-ok" if conn.get("online") else "badge-err"
    net_txt = "EN LIGNE" if conn.get("online") else "HORS LIGNE"

    retry_html = ""
    if retry_q:
        retry_html = '<tr><td>File d\'attente SFEC</td><td><span class="queue-badge">{} factures en attente</span></td></tr>'.format(len(retry_q))

    return _page(render_template("dashboard/index.html",
        sales=m.get("sales_invoices", 0), purchases=m.get("purchase_invoices", 0),
        val_count=val_count,
        contacts=m.get("contacts", 0), tax=m.get("tax_rates", 0),
        syncs=m.get("sync_count", 0), certs=m.get("certified_count", 0),
        db_ok=db_ok, db_txt=db_txt, last_sync=_esc(m.get("last_sync_at") or "Jamais"),
        error=_esc(m.get("last_error") or "Aucune"),
        sfec_ok=sfec_ok, sfec_txt=sfec_txt, sfec_url=_esc(sfec.get("url", "")),
        net_cls=net_cls, net_txt=net_txt, retry_html=retry_html
    ))

@bp.route("/invoices", methods=["GET"])
@_login_required
def invoices_page():
    inv_type = request.args.get("type", "sale")
    cache = get_cache()

    if inv_type == "purchase":
        invoices = cache.get("purchase_invoices", [])
    elif inv_type == "avoir":
        invoices = [i for i in cache.get("sales_invoices", []) if i.get("type_doc") == "avoir"]
    else:
        invoices = [i for i in cache.get("sales_invoices", []) if i.get("type_doc", "vente") != "avoir"]

    rows = ""
    for inv in invoices[:200]:
        s = inv.get("statut", "")
        sb = "badge-ok" if s == "COMPTABILISE" else "badge-warn" if s in ("CONFIRME", "A COMPTABILISER") else ""
        ss = inv.get("sfec_statut", "")
        sfb = "badge-ok" if ss in ("CERTIFIE", "DEJA_CERTIFIE") else "badge-warn" if ss == "EN_COURS" else "badge-err" if ss == "ERREUR" else "badge-info" if ss == "A_SURVEILLER" else ""
        sftxt = "Certifie" if ss == "CERTIFIE" else "Deja certifie" if ss == "DEJA_CERTIFIE" else "En cours" if ss == "EN_COURS" else "Erreur" if ss == "ERREUR" else "A surveiller" if ss == "A_SURVEILLER" else "A certifier" if inv_type == "sale" else "-"
        cert_num = inv.get("sfec_num_certif", "") or ""
        val = inv.get("valide", 0)
        val_cls = "badge-ok" if val == 1 else "badge-err"
        val_txt = "Valide" if val == 1 else "Non valide"
        type_badge_cls = "badge-warn" if inv.get("type_doc") == "avoir" else "badge-info"
        type_badge_txt = "Avoir" if inv.get("type_doc") == "avoir" else "Vente"
        rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td><span class='badge {}'>{}</span></td><td style='text-align:right'>{:,.2f}</td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td>{}</td></tr>".format(
            _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")), _esc(inv.get("nom_tiers", "")),
            type_badge_cls, type_badge_txt,
            inv.get("montant_ttc", 0), sb, _esc(s), val_cls, val_txt, sfb, _esc(sftxt), _esc(cert_num or "-")
        )
    sale_cls = "btn-primary" if inv_type == "sale" else ""
    purchase_cls = "btn-primary" if inv_type == "purchase" else ""
    avoir_cls = "btn-primary" if inv_type == "avoir" else ""
    empty = '<tr><td colspan="9" style="text-align:center;color:#64748b">Aucune facture</td></tr>' if not rows else ""
    return _page(render_template("dashboard/invoices.html",
        inv_type=inv_type, sale_cls=sale_cls, purchase_cls=purchase_cls, avoir_cls=avoir_cls,
        rows=rows, empty=empty
    ))


@bp.route("/pending", methods=["GET"])
@_login_required
def pending_page():
    cache = get_cache()
    sfec_cfg = get_config().get("sfec", {})
    sfec_ids = set()
    try:
        client = SfecClient()
        sfec_result = client.list_invoices(page=1, page_size=500)
        for inv in sfec_result.get("invoices", []):
            seller_inv = inv.get("seller_invoice_number", "") or inv.get("invoice_number", "")
            if seller_inv:
                sfec_ids.add(seller_inv)
    except Exception:
        pass
    uncertified = [
        inv for inv in cache.get("sales_invoices", [])
        if inv.get("numero", "") not in sfec_ids
        and inv.get("valide", 0) == 1
        and inv.get("sfec_statut", "") not in ("EN_COURS", "ERREUR")
    ]
    uncertified.sort(key=lambda x: x.get("date_facture", ""), reverse=True)
    val_count = sum(1 for inv in cache.get("sales_invoices", []) if inv.get("valide", 0) == 1)
    rows = ""
    for inv in uncertified[:200]:
        badge_avoir = ' <span class="badge badge-warn">Avoir</span>' if inv.get("type_doc") == "avoir" else ""
        rows += "<tr><td>{}{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.2f}</td><td><button class='btn btn-sm btn-success' onclick=\"certifySingle('{}')\">Certifier</button> <button class='btn btn-sm' style='background:#4a1d96;color:#fff' onclick=\"toMonitor('{}')\">Surveiller</button></td></tr>".format(
            _esc(inv.get("numero", "")), badge_avoir, _esc(inv.get("date_facture", "")), _esc(inv.get("nom_tiers", "")),
            inv.get("montant_ttc", 0), inv.get("id", ""), inv.get("id", "")
        )
    sfec_warn = ""
    if not sfec_cfg.get("enabled"):
        sfec_warn = '<p class="badge badge-err" style="margin-bottom:12px">SFEC desactivee</p>'
    empty = '<tr><td colspan="5" style="text-align:center;color:#64748b">Toutes certifiees !</td></tr>' if not rows else ""
    return _page(render_template("dashboard/pending.html",
        count=len(uncertified),
        sfec_warn=sfec_warn, val_count=val_count, rows=rows, empty=empty
    ))


@bp.route("/certified", methods=["GET"])
@_login_required
def certified_page():
    cache = get_cache()
    last_sfec = cache.get("last_sfec_sync_at", "")
    return _page(render_template("dashboard/certified.html",
                                 last_sfec=(last_sfec or "")[:19].replace("T", " ") or "jamais"))


def _print_float(val, default=0.0):
    """Conversion tolerante des montants/quantites venant de SFEC ou Sage."""
    if val in (None, ""):
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        try:
            return float(str(val).replace("%", "").replace(",", ".").strip())
        except (TypeError, ValueError):
            return default


def _print_logo_src(company):
    """Logo societe en data-URI, pour l'impression navigateur."""
    raw = (company.get("logo_base64") or "").strip()
    if raw:
        return raw if raw.startswith("data:") else "data:image/png;base64," + raw
    return image_data_uri(company_image_path(company, "logo_file_name"))


def _print_sage_lines(numero, type_doc="vente", domaine=None, sage_type=None):
    """Lignes Sage d'une piece source='sage' absentes de SQLite.

    Les types de document par defaut viennent de la configuration Sage
    (facture/avoir). Toute erreur (Sage indisponible, table absente) est
    silencieuse : l'impression doit rester possible sans ces colonnes.
    """
    try:
        from app.integration.sage.database import fetch_doc_lines, get_domain_config
        cfg = get_domain_config()
        dom = int(domaine) if domaine not in (None, "") else cfg["vente_domain"]
        types = []
        if sage_type not in (None, ""):
            types.append(int(sage_type))
        elif type_doc == "avoir":
            types.extend([cfg["avoir_type"], cfg["facture_type"]])
        else:
            types.extend([cfg["facture_type"], cfg["avoir_type"]])
        for doc_type in types:
            lignes = fetch_doc_lines(dom, doc_type, numero)
            if lignes:
                return lignes
    except Exception as e:
        logger.warning("Lignes Sage indisponibles pour %s: %s", numero, e)
    return []


def _print_local_lines(numero, type_doc="vente"):
    """Facture locale + lignes : SQLite, ou lecture Sage si non stockee.

    L'API SFEC ne renvoie ni la reference article, ni l'unite, ni le
    conditionnement/colis/poids : ces champs d'affichage viennent du local.
    """
    if not numero:
        return None, []
    try:
        local = invoice_engine.get_invoice_by_numero(numero)
    except Exception as e:
        logger.warning("Lookup local facture %s impossible: %s", numero, e)
        local = None
    if not local:
        return None, []
    lignes = local.get("lignes") or []
    if not lignes and local.get("source") == "sage":
        lignes = _print_sage_lines(
            numero, type_doc,
            local.get("sage_domaine"), local.get("sage_type"))
    return local, lignes


@bp.route("/api/invoices/print-log", methods=["POST"])
@_login_required
def api_invoice_print_log():
    """Enregistre une impression navigateur ; renvoie la mention de la suivante."""
    numero = ((request.get_json(silent=True) or {}).get("numero") or "").strip()
    if not numero:
        return jsonify({"error": "numero manquant"}), 400
    printed = print_copies.register_print(numero, "navigateur")
    return jsonify({"printed": printed, "next": print_copies.next_mention(numero)})


@bp.route("/certified/<invoice_id>/print", methods=["GET"])
@_login_required
def print_certified(invoice_id):
    inv = None
    try:
        client = SfecClient()
        inv = client.get_invoice(invoice_id)
    except Exception as e:
        logger.warning("Impossible de recuperer la facture SFEC %s: %s", invoice_id, e)

    if not inv:
        return "<h1>Facture non trouvee</h1><p><a href='/certified'>Retour</a></p>", 404

    company = get_config().get("company", {})
    # Numero imprime = numero vendeur (Sage) ; invoice_number SFEC peut etre
    # le numero national certifie.
    numero = (inv.get("seller_invoice_number") or inv.get("invoice_number") or "").strip()
    inv_type = inv.get("invoice_type", "") or inv.get("invoice_status", "")
    is_avoir = inv_type == "creditNote"
    local, lignes = _print_local_lines(numero, "avoir" if is_avoir else "vente")

    # ---- Lignes : fusion donnees SFEC (montants) + donnees locales (Sage)
    items = inv.get("items") or []
    items_rows = []
    total_poids = 0.0
    base18 = base5 = base_exempt = 0.0
    line_discounts = 0.0
    for i in range(max(len(items), len(lignes))):
        it = items[i] if i < len(items) else {}
        lg = lignes[i] if i < len(lignes) else {}
        qte = _print_float(it.get("quantity") if it.get("quantity") is not None
                           else lg.get("quantite"), 1.0)
        pu = _print_float(it.get("unit_price") if it.get("unit_price") is not None
                          else lg.get("prix_unitaire"), 0.0)
        taux = _print_float(it.get("tax_rate") if it.get("tax_rate") not in (None, "")
                            else lg.get("taux_tva"), 18.0)
        total_ligne = _print_float(it.get("net_amount") if it.get("net_amount") is not None
                                   else lg.get("montant_ht"), 0.0)
        brut_ligne = _print_float(it.get("subtotal") if it.get("subtotal") is not None
                                  else lg.get("subtotal"), 0.0) or qte * pu
        remise = _print_float(it.get("discount_amount"), None)
        if remise is None:
            remise = _print_float(lg.get("remise_montant"), 0.0)
        remise_pct = _print_float(lg.get("remise_pct"), 0.0)
        if not remise_pct and brut_ligne and remise:
            remise_pct = remise / brut_ligne * 100
        poids = _print_float(lg.get("poids_kg"), 0.0)
        nb_colis = _print_float(lg.get("nb_colis"), 0.0)
        pu_net = (total_ligne / qte) if qte else pu
        designation = it.get("designation") or lg.get("designation") or ""

        items_rows.append({
            "reference": lg.get("code_article", "") or it.get("article_code", "") or "",
            "designation": designation,
            "nb_colis": frn_amount(nb_colis, 2) if nb_colis else "",
            "conditionnement": lg.get("conditionnement", "") or "",
            "quantite": frn_amount(qte, 2),
            "unite": lg.get("unite", "") or "",
            "poids": frn_amount(poids, 2) if poids else "",
            "remise": fmt_rate(remise_pct) if remise_pct else "Prix Net",
            "pu": frn_amount(pu, 2),
            "pu_net": frn_amount(pu_net, 2),
            "total": frn_amount(total_ligne, 0),
        })

        if round(taux) == 18:
            base18 += total_ligne
        elif round(taux) == 5:
            base5 += total_ligne
        elif round(taux) == 0:
            base_exempt += total_ligne
        line_discounts += remise or 0
        total_poids += poids

    # ---- Totaux facture (SFEC en priorite, local en repli)
    total_ht = _print_float(inv.get("total_ht"), None)
    if total_ht is None:
        total_ht = _print_float((local or {}).get("montant_ht"), 0.0)
    ht_brut = _print_float((local or {}).get("montant_ht_brut"), 0.0)
    remise_lignes = _print_float((local or {}).get("total_line_discount_amount"), None)
    if remise_lignes is None:
        remise_lignes = line_discounts
    remise_globale = _print_float((local or {}).get("discount_amount"), None)
    if remise_globale is None:
        remise_globale = _print_float(inv.get("discount_amount"), 0.0)
    remise_totale = (remise_lignes or 0) + (remise_globale or 0)
    if not ht_brut:
        ht_brut = total_ht + remise_totale
    # Sage tronque le pourcentage de remise a une decimale (16,17 -> 16,1)
    remise_pct_glob = int(remise_totale / ht_brut * 1000) / 10.0 if ht_brut else 0

    tva18 = _print_float(inv.get("total_tax_t_amount", inv.get("total_tax18")), None)
    if tva18 is None:
        tva18 = _print_float((local or {}).get("total_tax_t_amount"), 0.0)
    tva5 = _print_float(inv.get("total_tax_r_amount", inv.get("total_tax5")), None)
    if tva5 is None:
        tva5 = _print_float((local or {}).get("total_tax_r_amount"), 0.0)
    exonere = _print_float(inv.get("total_exempt_amount"), None)
    if exonere is None:
        exonere = _print_float((local or {}).get("total_exempt_amount"), 0.0)
    centime = _print_float(inv.get("additional_cent_tax",
                                   (local or {}).get("additional_cent_tax")), 0.0)
    timbre = _print_float(inv.get("electronic_stamp_duty",
                                  (local or {}).get("electronic_stamp_duty")), 0.0)
    montant_tva = _print_float(inv.get("montant_tva", (local or {}).get("montant_tva")), None)
    if montant_tva is None:
        montant_tva = tva18 + tva5
    total_ttc = _print_float(inv.get("total_ttc", (local or {}).get("montant_ttc")), 0.0)
    net_a_payer = _print_float(inv.get("amount_due"), None)
    if net_a_payer is None:
        net_a_payer = total_ttc

    if not base18 and not base5 and not base_exempt:
        base18 = max(total_ht - exonere, 0)

    # ---- Tableau fiscal (codes Sage du modele : 1 / 202 / 203 / exonere)
    tax_rows = []
    if tva18 or base18:
        tax_rows.append({"code": "1", "base": frn_amount(base18), "taux": "18%",
                         "montant": frn_amount(tva18)})
    if tva5 or base5:
        tax_rows.append({"code": "1R", "base": frn_amount(base5), "taux": "5%",
                         "montant": frn_amount(tva5)})
    if exonere:
        tax_rows.append({"code": "0", "base": frn_amount(exonere), "taux": "Exonéré",
                         "montant": "-"})
    if centime:
        taux_c = (centime / total_ht * 100) if total_ht else 0
        tax_rows.append({"code": "202", "base": frn_amount(total_ht),
                         "taux": fmt_rate(taux_c),
                         "montant": frn_amount(centime)})
    if timbre:
        taux_t = (timbre / total_ht * 100) if total_ht else 0
        tax_rows.append({"code": "203", "base": frn_amount(total_ht),
                         "taux": fmt_rate(taux_t),
                         "montant": frn_amount(timbre)})
    if not tax_rows:
        tax_rows.append({"code": "1", "base": frn_amount(total_ht), "taux": "18%",
                         "montant": frn_amount(montant_tva)})

    total_taxes = montant_tva + centime + timbre
    summary = {
        "ht_brut": frn_amount(ht_brut),
        "remise_pct": frn_amount(remise_pct_glob, 1),
        "remise": frn_amount(remise_totale),
        "ht_net": frn_amount(total_ht),
        "taxes": frn_amount(total_taxes),
        "net": frn_amount(net_a_payer),
    }

    # ---- Certification / QR (image SFEC uniquement)
    qr_code = inv.get("qr_code") or inv.get("certification_qr_code") or ""
    qr_src = qr_code if qr_code.startswith("data:") else ""

    seller_lines = company_footer_blocks(company)[0][1:4]
    footer = invoice_footer(company)

    devise = (inv.get("currency") or (local or {}).get("devise") or "XAF").upper()
    euro_total = frn_amount(total_ttc / EURO_PARITY) if devise in ("XAF", "XOF") else "-"
    footer_txt = (company.get("invoice_footer") or "").strip()
    cgv_lines = [footer_txt] if footer_txt else [CGV_TEXT, CGR_TEXT]

    return render_template("dashboard/print.html",
        doc_title=DOC_TITLE_AVOIR if is_avoir else DOC_TITLE_FACTURE,
        is_avoir=is_avoir,
        reference_invoice=inv.get("reference_invoice_id") or (local or {}).get("reference_invoice_id", ""),
        invoice_number=numero,
        barcode_svg=barcode_svg(numero),
        invoice_date=fr_date(inv.get("invoice_date") or (local or {}).get("date_facture", "")),
        currency=devise,
        reference=inv.get("reference") or (local or {}).get("reference", "") or "",
        compte=(local or {}).get("tiers_code", "") or "",
        logo_src=_print_logo_src(company),
        seller_name=inv.get("seller_name", company.get("name", "")),
        seller_lines=seller_lines,
        buyer_name=inv.get("buyer_name", "") or (local or {}).get("tiers_nom", ""),
        buyer_niu=inv.get("buyer_niu", "") or (local or {}).get("tiers_niu", ""),
        buyer_addr=inv.get("buyer_address", "") or (local or {}).get("tiers_adresse", ""),
        buyer_phone=inv.get("buyer_phone", "") or (local or {}).get("tiers_telephone", ""),
        cert_num=inv.get("certification_number", "") or inv.get("certification_id", ""),
        cert_date=fr_date_long(inv.get("certification_date") or ""),
        qr_src=qr_src,
        operateur=invoice_operator(local),
        tirage=tirage_stamp(),
        items_rows=items_rows,
        filler_mm=max(3, 60 - 6 * len(items_rows)),
        total_poids=frn_amount(total_poids, 3) if total_poids else "",
        tax_rows=tax_rows,
        total_taxes=frn_amount(total_taxes),
        summary=summary,
        montant_lettres=montant_en_lettres(total_ttc),
        euro_total=euro_total,
        validity=VALIDITE_OFFRE,
        parity_l1=PARITE_EURO_L1,
        parity_l2=PARITE_EURO_L2,
        conditions_txt=company.get("default_payment_terms", "") or "",
        echeance=fr_date((local or {}).get("date_echeance", "")),
        payment_mode=payment_label(inv.get("payment_method")
                                   or (local or {}).get("payment_method", "")),
        cgv_lines=cgv_lines,
        mention=print_copies.next_mention(numero),
        csrf_token=_get_csrf_token(),
        footer=footer,
        footer_logo_src=image_data_uri(footer["logo_path"]) if footer else "",
    )


@bp.route("/sales", methods=["GET"])
@_login_required
def sales_page():
    stats = pos_engine.count_tickets()

    ticket_rows = ""
    result = pos_engine.list_tickets(limit=200)
    for t in result["tickets"]:
        vendeur_txt = "{} {}".format(t.get("vendeur_prenom", "") or "", t.get("vendeur_nom", "") or "").strip() or "-"
        sstatut = t.get("sfec_statut", "") or ""
        inv_id = t.get("invoice_id")
        if sstatut in ("CERTIFIE", "DEJA_CERTIFIE"):
            sfec_cell = '<span class="badge badge-ok" title="{}">Certifiee</span>'.format(_esc((t.get("sfec_num_certif", "") or "")[:25]))
        elif sstatut == "EN_COURS":
            sfec_cell = '<span class="badge badge-warn">En attente</span>'
            if inv_id:
                sfec_cell += ' <button class="btn btn-sm" onclick="certifySales({})">Certifier</button>'.format(inv_id)
        elif sstatut == "ERREUR":
            sfec_cell = '<span class="badge badge-err">Erreur</span>'
            if inv_id:
                sfec_cell += ' <button class="btn btn-sm" onclick="certifySales({})">Certifier</button>'.format(inv_id)
        elif inv_id:
            sfec_cell = '<span class="badge badge-info">Non certifiee</span> <button class="btn btn-sm" onclick="certifySales({})">Certifier</button>'.format(inv_id)
        else:
            sfec_cell = "-"
        ticket_rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.0f}</td><td>{}</td><td>{}</td><td>{}</td><td><a href='/pos/ticket/{}/print' class='btn btn-sm' target='_blank'>Voir</a></td></tr>".format(
            _esc(t.get("numero", "")), _esc(t.get("date_ticket", "")[:16]),
            _esc(t.get("tiers_nom", "")), t.get("montant_ttc", 0),
            _esc(t.get("mode_paiement", "")), _esc(vendeur_txt), sfec_cell, t.get("id", "")
        )

    vendeurs = pos_engine.list_vendeurs()
    vendeur_options = '<option value="">Tous</option>'
    for v in vendeurs:
        vendeur_options += '<option value="{}">{} {}</option>'.format(v["id"], v.get("prenom", ""), v["nom"])

    return _page(render_template("directory/sales.html",
        total=stats.get("total", 0), tickets_jour=stats.get("tickets_jour", 0),
        ca_jour=stats.get("ca_jour", 0), ca_total=stats.get("ca_total", 0),
        count=result.get("total", 0), rows=ticket_rows,
        vendeur_opts=vendeur_options
    ))


@bp.route("/print", methods=["GET"])
@_login_required
def impressions_page():
    from app.print_hijack.auto_printer import list_print_records

    def _local(ts):
        if not ts:
            return "-"
        try:
            epoch = calendar.timegm(time.strptime(ts, "%Y-%m-%d %H:%M:%S"))
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))
        except (ValueError, TypeError):
            return ts

    badge = {
        "IMPRIME": ("badge-ok", "Imprime"),
        "EN_COURS": ("badge-warn", "En cours"),
        "ATTENTE_CERTIF": ("badge-warn", "Attente certif"),
        "NON_CONFORME": ("badge-warn", "Non conforme"),
        "SKIP_DEDUP": ("badge-info", "Doublon"),
        "QUARANTINE": ("badge-err", "Quarantaine"),
        "REFUS": ("badge-err", "Refuse"),
        "ERREUR": ("badge-err", "Erreur"),
        "ERREUR_IMPRESSION": ("badge-err", "Erreur impression"),
    }
    sfec_badge = {
        "CERTIFIE": ("badge-ok", "Certifie"),
        "DEJA_CERTIFIE": ("badge-ok", "Deja certifie"),
        "EN_COURS": ("badge-warn", "En cours"),
        "ERREUR": ("badge-err", "Erreur"),
        "A_SURVEILLER": ("badge-info", "A surveiller"),
    }

    records = list_print_records(limit=200)
    rows = ""
    for r in records:
        st = (r["status"] or "").upper()
        cls, txt = badge.get(st, ("badge-info", st or "-"))
        ss = (r["sfec_statut"] or "").upper()
        scls, stxt = sfec_badge.get(ss, ("badge-info", ss or "-"))
        out_path = r["out_path"] or ""
        out_name = os.path.basename(out_path) if out_path else "-"
        err = (r["error"] or "").strip()
        out_cell = "<span title='{}'>{}</span>".format(_esc(out_path), _esc(out_name)) if out_path else "-"
        rows += ("<tr><td>{}</td><td>{}</td>"
                 "<td><span class='badge {}'>{}</span></td>"
                 "<td><span class='badge {}'>{}</span></td>"
                 "<td>{}</td><td>{}</td><td>{}</td></tr>").format(
            _esc(_local(r["printed_at"])), _esc(r["numero"] or "-"),
            cls, _esc(txt), scls, _esc(stxt),
            _esc(r["sfec_num_certif"] or "-"), _esc(r["printer"] or "-"),
            out_cell if not err else out_cell + " <span class='badge badge-err' title='{}'>!</span>".format(_esc(err)),
        )
    empty = ('<tr><td colspan="7" style="text-align:center;color:#64748b">'
             'Aucune impression</td></tr>') if not rows else ""
    return _page(render_template("dashboard/impressions.html",
                                 count=len(records), rows=rows, empty=empty))


@bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@bp.route("/ready", methods=["GET"])
def ready():
    return jsonify({"ready": True})



