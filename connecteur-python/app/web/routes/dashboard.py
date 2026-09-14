"""Blueprint dashboard — Pages tableau de bord : / /invoices /pending
/certified /certified/<id>/print /sales /ready /health.

Migré depuis app/web/parking/dashboard_pages.py (étape A4). Les handlers
sont strictement inchangés ; les URL / méthodes HTTP sont identiques aux
app.add_url_rule() historiques de app/web/dashboard.py. Le wrapping @_login_required est reproduit à l'identique, désormais via
un import direct de app.web.auth.security (aucun import circulaire : la
sécurité ne dépend d'aucun module de routes).
"""

import functools
from app.core.utils import fmt_money
from flask import Blueprint, jsonify, render_template, request
from app.config.manager import get_config
from app.domain import pos as pos_engine
from app.integration.sfec.client import SfecClient
from app.integration.sfec.endpoints import check_health
from app.sync.connectivity import get_status
from app.sync.engine import get_cache, get_metrics, get_retry_queue
from app.web.common import _esc, _page
from app.web.auth.security import _login_required, logger


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
    invoices = cache.get("purchase_invoices" if inv_type == "purchase" else "sales_invoices", [])
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
        rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.2f}</td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td><span class='badge {}'>{}</span></td><td>{}</td></tr>".format(
            _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")), _esc(inv.get("nom_tiers", "")),
            inv.get("montant_ttc", 0), sb, _esc(s), val_cls, val_txt, sfb, _esc(sftxt), _esc(cert_num or "-")
        )
    sale_cls = "btn-primary" if inv_type == "sale" else ""
    purchase_cls = "btn-primary" if inv_type == "purchase" else ""
    empty = '<tr><td colspan="8" style="text-align:center;color:#64748b">Aucune facture</td></tr>' if not rows else ""
    return _page(render_template("dashboard/invoices.html",
        inv_type=inv_type, sale_cls=sale_cls, purchase_cls=purchase_cls,
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
        rows += "<tr><td>{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{:,.2f}</td><td><button class='btn btn-sm btn-success' onclick=\"certifySingle('{}')\">Certifier</button> <button class='btn btn-sm' style='background:#4a1d96;color:#fff' onclick=\"toMonitor('{}')\">Surveiller</button></td></tr>".format(
            _esc(inv.get("numero", "")), _esc(inv.get("date_facture", "")), _esc(inv.get("nom_tiers", "")),
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
    qr_code = inv.get("qr_code") or inv.get("certification_qr_code") or ""
    is_image = qr_code.startswith("data:")
    if qr_code:
        qr_display = '<img src="{}" style="max-width:200px;max-height:200px">'.format(qr_code) if is_image else '<pre style="font-size:10px;word-break:break-all">{}</pre>'.format(qr_code)
    else:
        qr_display = '<p style="color:#999">QR Code non disponible</p>'
 
    items = inv.get("items_json") or []
    item_rows = ""
    for item in items:
        item_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td></tr>".format(
            _esc(item.get("designation", "")),
            fmt_money(item.get("quantity", 0)),
            fmt_money(item.get("unit_price", 0)),
            _esc(item.get("tax_rate", "0")),
            fmt_money(item.get("net_amount", 0), 2),
            )

    return render_template("dashboard/print.html",
        doc_label=_esc(doc_label),
        invoice_type=_esc(inv_type),
        recipient_type=_esc(inv.get("recipient_type", "")),
        reference_invoice_id=_esc(inv.get("reference_invoice_id", "") or ""),
        discount_amount=_esc(fmt_money(inv.get("discount_amount", "0"))),
        total_exempt=_esc(fmt_money(inv.get("total_exempt_amount", "0"))),
        additional_cent_tax=_esc(fmt_money(inv.get("additional_cent_tax", "0"))),
        electronic_stamp_duty=_esc(fmt_money(inv.get("electronic_stamp_duty", "0"))),
        invoice_number=_esc(inv.get("invoice_number", "")),
        invoice_date=_esc((inv.get("invoice_date") or "")[:10]),
        currency=_esc(inv.get("currency", "XAF")),
        seller_name=_esc(inv.get("seller_name", company.get("name", ""))),
        seller_addr=_esc(inv.get("seller_address", company.get("address", ""))),
        seller_niu=_esc(inv.get("seller_niu", company.get("tax_number", ""))),
        seller_rccm=_esc(inv.get("seller_rccm", "")),
        buyer_name=_esc(inv.get("buyer_name", "")),
        buyer_niu=_esc(inv.get("buyer_niu", "")),
        buyer_addr=_esc(inv.get("buyer_address", "")),
        buyer_phone=_esc(inv.get("buyer_phone", "")),
        payment_method=_esc(inv.get("payment_method", "")),
        amount_due=_esc(fmt_money(inv.get("amount_due", "0"))),
        total_ht=_esc(fmt_money(inv.get("total_ht", "0"))),
        total_tax18=_esc(fmt_money(inv.get("total_tax18", "0"))),
        total_tax5=_esc(fmt_money(inv.get("total_tax5", "0"))),
        total_ttc=_esc(fmt_money(inv.get("total_ttc", "0"))),
        item_rows=item_rows,
        cert_status=_esc(inv.get("certification_status", "")),
        short_sig=_esc(inv.get("certification_short_signature", "")),
        signature=_esc(inv.get("certification_signature", "")),
        cert_date=_esc((inv.get("certification_date") or "")[:19].replace("T", " ")),
        qr_display=qr_display,
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


@bp.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@bp.route("/ready", methods=["GET"])
def ready():
    return jsonify({"ready": True})



