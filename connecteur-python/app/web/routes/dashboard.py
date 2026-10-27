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
import json
import os
import time

from datetime import datetime, timezone
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
from app.integration.sage.database import reset_sfec_monitor
from app.sync.connectivity import get_status
from app.sync.engine import (
    certify_single,
    get_cache,
    get_metrics,
    get_retry_queue,
    polling_active,
)
from app.storage import db as sqlite_db
from app.web.common import _csv_response, _esc, _page
from app.web.auth.security import _get_csrf_token, _login_required, logger


bp = Blueprint('dashboard', __name__)


def _local_ts(value):
    """ISO UTC -> date/heure locale lisible, sans faire echouer la page."""
    if not value:
        return "Jamais"
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%d/%m/%Y %H:%M")
    except (ValueError, TypeError, OSError):
        return str(value)[:19].replace("T", " ")


def _safe_amount(value, decimals=0):
    return fmt_money(value, decimals)


@bp.route("/", methods=["GET"])
@_login_required
def index():
    m = get_metrics()
    cfg = get_config()
    sfec_cfg = cfg.get("sfec", {}) or {}
    conn = get_status(refresh=False)
    retry_q = get_retry_queue()
    cache = get_cache()

    val_count = m.get("pending_accounting", 0)
    sfec_enabled = bool(sfec_cfg.get("enabled"))
    sfec_configured = sfec_enabled and bool(sfec_cfg.get("api_key"))
    polling = polling_active()

    db_ok = "badge-ok" if m.get("db_connected") else "badge-err"
    db_txt = "Connectee" if m.get("db_connected") else "Deconnectee"
    if not sfec_enabled:
        sfec_ok, sfec_txt = "badge-err", "Desactivee"
    elif not sfec_configured:
        sfec_ok, sfec_txt = "badge-err", "Cle manquante"
    elif cache.get("last_sfec_sync_at"):
        sfec_ok, sfec_txt = "badge-ok", "Synchronisee"
    else:
        sfec_ok, sfec_txt = "badge-warn", "Jamais synchronisee"
    sfec_url = (sfec_cfg.get("sandbox_url") if sfec_cfg.get("use_sandbox")
                else sfec_cfg.get("base_url")) or ""
    poll_ok = "badge-ok" if polling else "badge-err"
    poll_txt = "Actif" if polling else "Inactif"
    net_cls = "badge-ok" if conn.get("online") else "badge-err"
    net_txt = "EN LIGNE" if conn.get("online") else "HORS LIGNE"

    retry_html = ""
    if retry_q:
        retry_html = '<tr><td>File d\'attente SFEC</td><td><span class="queue-badge">{} factures en attente</span></td></tr>'.format(len(retry_q))

    return _page(render_template("dashboard/index.html",
        sales=m.get("sales_invoices", 0), purchases=m.get("purchase_invoices", 0),
        val_count=val_count, credit_notes=m.get("credit_notes", 0),
        contacts=m.get("contacts", 0), tax=m.get("tax_rates", 0),
        syncs=m.get("sync_count", 0), certs=m.get("certified_count", 0),
        db_ok=db_ok, db_txt=db_txt, last_sync=_esc(_local_ts(m.get("last_sync_at"))),
        error=_esc((m.get("last_error") or "Aucune")[:300]),
        sfec_ok=sfec_ok, sfec_txt=sfec_txt, sfec_url=_esc(sfec_url),
        net_cls=net_cls, net_txt=net_txt, retry_html=retry_html,
        poll_ok=poll_ok, poll_txt=poll_txt
    ))

@bp.route("/invoices", methods=["GET"])
@_login_required
def invoices_page():
    inv_type = request.args.get("type", "sale")
    if inv_type not in ("sale", "purchase", "avoir"):
        inv_type = "sale"
    cache = get_cache()

    if inv_type == "purchase":
        invoices = cache.get("purchase_invoices", [])
    elif inv_type == "avoir":
        invoices = [i for i in cache.get("sales_invoices", []) if i.get("type_doc") == "avoir"]
    else:
        invoices = [i for i in cache.get("sales_invoices", []) if i.get("type_doc", "vente") != "avoir"]

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    limit = 100
    total = len(invoices)
    pages = max(1, (total + limit - 1) // limit)
    page = min(page, pages)
    offset = (page - 1) * limit
    page_rows = invoices[offset:offset + limit]

    rows = ""
    for inv in page_rows:
        s = inv.get("statut", "")
        sb = "badge-ok" if s == "COMPTABILISE" else "badge-info" if s in ("LETTRE", "LETTRÉ", "LETTRE ") else "badge-warn" if s in ("CONFIRME", "A COMPTABILISER") else ""
        ss = inv.get("sfec_statut", "")
        sfb = "badge-ok" if ss in ("CERTIFIE", "DEJA_CERTIFIE") else "badge-warn" if ss == "EN_COURS" else "badge-err" if ss == "ERREUR" else "badge-info" if ss == "A_SURVEILLER" else ""
        sftxt = "Certifie" if ss == "CERTIFIE" else "Deja certifie" if ss == "DEJA_CERTIFIE" else "En cours" if ss == "EN_COURS" else "Erreur" if ss == "ERREUR" else "A surveiller" if ss == "A_SURVEILLER" else "A certifier" if inv_type == "sale" else "-"
        cert_num = inv.get("sfec_num_certif", "") or ""
        val = inv.get("valide", 0)
        val_cls = "badge-ok" if val == 1 else "badge-err"
        val_txt = "Valide" if val == 1 else "Non valide"
        is_avoir = inv.get("type_doc") == "avoir"
        sage_type = inv.get("sage_type")
        try:
            sage_type = int(sage_type) if sage_type not in (None, "") else None
        except (TypeError, ValueError):
            sage_type = None
        type_badge_cls = "badge-warn" if is_avoir else "badge-info"
        if is_avoir and sage_type == 8:
            type_badge_txt = "Retour"
        elif is_avoir:
            type_badge_txt = "Avoir"
        else:
            type_badge_txt = "Achat" if inv_type == "purchase" else "Vente"
        rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td><span class='badge {}'>{}</span></td><td style='text-align:right'>{}</td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td>{}</td></tr>".format(
            _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")), _esc(inv.get("nom_tiers", "")),
            type_badge_cls, type_badge_txt,
            _safe_amount(inv.get("montant_ttc", 0), 2), sb, _esc(s), val_cls, val_txt, sfb, _esc(sftxt), _esc(cert_num or "-")
        )
    sale_cls = "btn-primary" if inv_type == "sale" else ""
    purchase_cls = "btn-primary" if inv_type == "purchase" else ""
    avoir_cls = "btn-primary" if inv_type == "avoir" else ""
    empty = '<tr><td colspan="9" style="text-align:center;color:#64748b">Aucune facture</td></tr>' if not rows else ""
    return _page(render_template("dashboard/invoices.html",
        inv_type=inv_type, sale_cls=sale_cls, purchase_cls=purchase_cls, avoir_cls=avoir_cls,
        rows=rows, empty=empty, total=total, page=page, pages=pages,
        has_prev=page > 1, has_next=page < pages,
        offset=offset, shown=len(page_rows)
    ))


@bp.route("/pending", methods=["GET"])
@_login_required
def pending_page():
    cache = get_cache()
    sfec_cfg = get_config().get("sfec", {})
    sfec_enabled = bool(sfec_cfg.get("enabled"))
    sfec_configured = sfec_enabled and bool(sfec_cfg.get("api_key"))
    sfec_ids = set()
    for item in cache.get("sfec_invoices", []):
        seller_inv = item.get("seller_invoice_number", "") or item.get("invoice_number", "")
        if seller_inv:
            sfec_ids.add(seller_inv)
    sfec_lookup_available = bool(sfec_ids)

    uncertified = []
    for inv in cache.get("sales_invoices", []):
        if inv.get("statut_code") != 2:
            continue
        local_status = inv.get("sfec_statut", "")
        if local_status in ("EN_COURS", "ERREUR", "A_SURVEILLER", "CERTIFIE", "DEJA_CERTIFIE"):
            continue
        numero = inv.get("numero", "")
        if sfec_lookup_available and numero in sfec_ids:
            continue
        uncertified.append(inv)
    uncertified.sort(key=lambda x: x.get("date_facture", ""), reverse=True)

    pending_reasons = cache.get("pending_reasons", {}) or {}

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    limit = 100
    total_pending = len(uncertified)
    pages = max(1, (total_pending + limit - 1) // limit)
    page = min(page, pages)
    offset = (page - 1) * limit
    page_rows = uncertified[offset:offset + limit]

    rows = ""
    for inv in page_rows:
        sage_type = inv.get("sage_type")
        try:
            sage_type = int(sage_type) if sage_type not in (None, "") else None
        except (TypeError, ValueError):
            sage_type = None
        badge_avoir = ' <span class="badge badge-warn">{}</span>'.format(
            "Retour" if sage_type == 8 else "Avoir") if inv.get("type_doc") == "avoir" else ""
        motif = _esc(pending_reasons.get(inv.get("id"), ""))
        rows += "<tr><td>{}{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{}</td><td>{}</td><td><button class='btn btn-sm btn-success' onclick=\"certifySingle('{}')\">Certifier</button> <button class='btn btn-sm' style='background:#4a1d96;color:#fff' onclick=\"toMonitor('{}')\">Surveiller</button></td></tr>".format(
            _esc(inv.get("numero", "")), badge_avoir, _esc(inv.get("date_facture", "")),
            _esc(inv.get("nom_tiers", "")), _safe_amount(inv.get("montant_ttc", 0), 2),
            motif,
            inv.get("id", ""), inv.get("id", "")
        )
    sfec_warn = ""
    if not sfec_enabled:
        sfec_warn = '<p class="badge badge-err" style="margin-bottom:12px">SFEC desactivee</p>'
    elif not sfec_configured:
        sfec_warn = '<p class="badge badge-err" style="margin-bottom:12px">SFEC activee mais cle API manquante</p>'
    elif not sfec_lookup_available:
        sfec_warn = ('<p class="badge badge-warn" style="margin-bottom:12px">'
                     'Liste SFEC indisponible : filtre base sur le statut local, verification impossible</p>')
    empty = '<tr><td colspan="6" style="text-align:center;color:#64748b">Toutes certifiees !</td></tr>' if not rows else ""

    # Proposition 06 : les factures A_SURVEILLER ne sont plus muettes. Le
    # motif est enregistre par mark_to_monitor() et affiche ici avec une
    # action « Relancer » qui remet le statut a NULL pour un seul passage.
    try:
        anomalies = sqlite_db.fetch_sfec_anomalies(limit=500)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lecture des anomalies SFEC impossible: %s", exc)
        anomalies = []
    anomaly_rows = ""
    for anomaly in anomalies:
        numero = str(anomaly.get("numero") or "")
        montant = anomaly.get("montant_ttc")
        montant_txt = _safe_amount(montant, 2) if montant not in (None, "") else "-"
        retry_arg = _esc(json.dumps(numero))
        anomaly_rows += (
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td style='text-align:right'>{}</td>"
            "<td><button class='btn btn-sm btn-success' "
            "onclick=\"relancer({})\">Relancer</button></td></tr>"
        ).format(
            _esc(numero),
            _esc(anomaly.get("date_facture", "")),
            _esc(anomaly.get("detected_at", "")),
            _esc(anomaly.get("motif", "")),
            montant_txt,
            retry_arg,
        )
    anomaly_empty = (
        '<tr><td colspan="6" style="text-align:center;color:#64748b">'
        'Aucune anomalie SFEC</td></tr>'
    ) if not anomaly_rows else ""

    return _page(render_template("dashboard/pending.html",
        count=total_pending,
        sfec_warn=sfec_warn,
        val_count=sum(1 for i in cache.get("sales_invoices", [])
                      if i.get("statut_code") == 2 and i.get("type_doc", "vente") != "avoir"),
        rows=rows, empty=empty, page=page, pages=pages,
        has_prev=page > 1, has_next=page < pages,
        offset=offset, shown=len(page_rows),
        anomaly_rows=anomaly_rows,
        anomaly_empty=anomaly_empty,
        anomalies_count=len(anomalies),
    ))


@bp.route("/api/sfec/relancer", methods=["POST"])
@_login_required
def api_sfec_relancer():
    """Relance manuelle d'une anomalie SFEC (proposition 06).

    Le statut Sage est remis a NULL uniquement pour cette piece et seulement
    s'il vaut A_SURVEILLER. Si la facture est presente dans le cache, on
    tente immediatement la certification ; sinon le prochain cycle la
    reprendra.
    """
    data = request.get_json(silent=True) or {}
    numero = str(data.get("numero") or "").strip()
    if not numero:
        return jsonify({"success": False, "error": "numero requis"}), 400

    try:
        reset_sfec_monitor(numero)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Relance SFEC: reset Sage impossible pour %s: %s", numero, exc)

    try:
        with sqlite_db.get_cursor() as cur:
            cur.execute(
                "UPDATE invoices SET sfec_statut = '' "
                "WHERE numero = ? AND sfec_statut = 'A_SURVEILLER'",
                (numero,),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Relance SFEC: reset SQLite impossible pour %s: %s", numero, exc)

    invoice = None
    for inv in get_cache().get("sales_invoices", []):
        if str(inv.get("numero") or "") == numero:
            invoice = inv
            break
    if not invoice:
        return jsonify({
            "success": True,
            "queued": True,
            "message": "Statut remis a vide : reprise au prochain cycle",
        })

    try:
        return jsonify(certify_single(invoice.get("id")))
    except Exception as exc:  # noqa: BLE001
        logger.error("Relance SFEC echouee pour %s: %s", numero, exc)
        return jsonify({"success": False, "error": str(exc)}), 500


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
            types.extend([cfg["avoir_type"], cfg["retour_type"], cfg["facture_type"]])
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

    # Fallback lecture directe des lignes Sage quand la facture n'est pas
    # (ou plus) en base locale SQLite : affiche quand meme les articles a
    # l'impression. Teste les types 6/7/8 comme _print_sage_lines.
    if not lignes and numero:
        lignes = _print_sage_lines(numero, "avoir" if is_avoir else "vente")

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
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    limit = 100

    ticket_rows = ""
    total_rows = stats.get("total", 0)
    pages = max(1, (total_rows + limit - 1) // limit)
    if page > pages:
        page = pages
    result = pos_engine.list_tickets(limit=limit, offset=(page - 1) * limit)
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
        ticket_rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{}</td><td>{}</td><td>{}</td><td>{}</td><td><a href='/pos/ticket/{}/print' class='btn btn-sm' target='_blank' rel='noopener noreferrer'>Voir</a></td></tr>".format(
            _esc(t.get("numero", "")), _esc((t.get("date_ticket") or "")[:16]),
            _esc(t.get("tiers_nom", "")), _safe_amount(t.get("montant_ttc", 0), 2),
            _esc(t.get("mode_paiement", "")), _esc(vendeur_txt), sfec_cell, t.get("id", "")
        )

    vendeurs = pos_engine.list_vendeurs()
    vendeur_options = '<option value="">Tous</option>'
    for v in vendeurs:
        vendeur_options += '<option value="{}">{} {}</option>'.format(
            v["id"], _esc(v.get("prenom", "")), _esc(v.get("nom", "")))

    return _page(render_template("directory/sales.html",
        total=stats.get("total", 0), tickets_jour=stats.get("tickets_jour", 0),
        ca_jour=stats.get("ca_jour", 0), ca_total=stats.get("ca_total", 0),
        count=result.get("total", 0), rows=ticket_rows,
        vendeur_opts=vendeur_options, page=page, pages=pages,
        has_prev=page > 1, has_next=page < pages,
        offset=(page - 1) * limit, shown=len(result.get("tickets", []))
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
        "IMPRIMANTE_ABSENTE": ("badge-err", "Imprimante absente"),
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
def _sqlite_alive():
    try:
        with sqlite_db.get_cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            return bool(cur.fetchone())
    except Exception:
        return False


@bp.route("/health", methods=["GET"])
def health():
    """Supervision locale : detail reserve a la boucle locale (proposition 09)."""
    from app.core.health import collect_health, is_loopback

    allow_remote = os.environ.get("TCONNECTOR_HEALTH_ALLOW_REMOTE") == "1"
    if not allow_remote and not is_loopback(request.remote_addr):
        return jsonify({"error": "not found"}), 404
    payload = collect_health()
    return jsonify(payload), 200 if payload.get("ok") else 503


@bp.route("/ready", methods=["GET"])
def ready():
    db_ok = _sqlite_alive()
    cfg = get_config()
    sfec_cfg = cfg.get("sfec", {}) or {}
    sfec_state = "disabled"
    if sfec_cfg.get("enabled"):
        sfec_state = "configured" if sfec_cfg.get("api_key") else "missing_api_key"
    cache = get_cache()
    payload = {
        "ready": bool(db_ok),
        "checks": {
            "sqlite": "up" if db_ok else "down",
            "config": "loaded" if cfg else "empty",
            "sage_cache": "populated" if cache.get("db_connected") else "empty",
            "sfec": sfec_state,
        },
    }
    return jsonify(payload), 200 if db_ok else 503

@bp.route("/sales/export", methods=["GET"])
@_login_required
def sales_export():
    """Export CSV (max 5000 lignes) des tickets POS avec les memes filtres que la liste."""
    def _clean(name, default):
        v = request.args.get(name, "") or None
        return v or default
    res = pos_engine.list_tickets(
        date_from=_clean("date_from", None),
        date_to=_clean("date_to", None),
        vendeur_id=_clean("vendeur_id", None),
        search=_clean("search", None),
        limit=5000, offset=0)
    rows = []
    for t in res.get("tickets", []):
        vendeur = "{} {}".format(t.get("vendeur_prenom", "") or "",
                                 t.get("vendeur_nom", "") or "").strip()
        rows.append([
            t.get("numero", ""), (t.get("date_ticket") or "")[:16],
            t.get("tiers_nom", ""), t.get("mode_paiement", ""),
            t.get("montant_ttc", ""), vendeur,
            t.get("invoice_numero", ""), t.get("sfec_statut", ""),
        ])
    header = ["ticket", "date", "client", "paiement", "montant TTC",
              "vendeur", "facture", "statut SFEC"]
    return _csv_response("ventes-pos.csv", header, rows)
