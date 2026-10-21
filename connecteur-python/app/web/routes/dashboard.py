"""Blueprint dashboard — Pages tableau de bord : / /invoices /pending
/certified /certified/<id>/print /sales /ready /health.

Migré depuis app/web/parking/dashboard_pages.py (étape A4). Les handlers
sont strictement inchangés ; les URL / méthodes HTTP sont identiques aux
app.add_url_rule() historiques de app/web/dashboard.py. Le wrapping @_login_required est reproduit à l'identique, désormais via
un import direct de app.web.auth.security (aucun import circulaire : la
sécurité ne dépend d'aucun module de routes).
"""

from datetime import datetime, timezone
from app.core.utils import fmt_money
from flask import Blueprint, jsonify, render_template, request
from app.config.manager import get_config
from app.domain import pos as pos_engine
from app.integration.sfec.client import SfecClient
from app.sync.connectivity import get_status
from app.sync.engine import get_cache, get_metrics, get_retry_queue, polling_active
from app.storage import db as sqlite_db
from app.web.common import _csv_response, _esc, _page
from app.web.auth.security import _login_required, logger


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
        type_badge_cls = "badge-warn" if inv.get("type_doc") == "avoir" else "badge-info"
        type_badge_txt = "Avoir" if inv.get("type_doc") == "avoir" else ("Achat" if inv_type == "purchase" else "Vente")
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
        badge_avoir = ' <span class="badge badge-warn">Avoir</span>' if inv.get("type_doc") == "avoir" else ""
        rows += "<tr><td>{}{}</td><td>{}</td><td>{}</td><td style='text-align:right'>{}</td><td><button class='btn btn-sm btn-success' onclick=\"certifySingle('{}')\">Certifier</button> <button class='btn btn-sm' style='background:#4a1d96;color:#fff' onclick=\"toMonitor('{}')\">Surveiller</button></td></tr>".format(
            _esc(inv.get("numero", "")), badge_avoir, _esc(inv.get("date_facture", "")),
            _esc(inv.get("nom_tiers", "")), _safe_amount(inv.get("montant_ttc", 0), 2),
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
    empty = '<tr><td colspan="5" style="text-align:center;color:#64748b">Toutes certifiees !</td></tr>' if not rows else ""
    return _page(render_template("dashboard/pending.html",
        count=total_pending,
        sfec_warn=sfec_warn,
        val_count=sum(1 for i in cache.get("sales_invoices", [])
                      if i.get("statut_code") == 2 and i.get("type_doc", "vente") != "avoir"),
        rows=rows, empty=empty, page=page, pages=pages,
        has_prev=page > 1, has_next=page < pages,
        offset=offset, shown=len(page_rows)
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
    is_image = (qr_code.startswith("data:image/")
                or qr_code.startswith("https://")
                or qr_code.startswith("http://")) and len(qr_code) < 40000
    if qr_code:
        qr_display = ('<img src="{}" style="max-width:200px;max-height:200px">'.format(_esc(qr_code))
                      if is_image else
                      '<pre style="font-size:10px;word-break:break-all">{}</pre>'.format(_esc(qr_code)))
    else:
        qr_display = '<p style="color:#999">QR Code non disponible</p>'
 
    items = inv.get("items") or inv.get("items_json") or []
    if isinstance(items, str):
        try:
            import json as _json
            items = _json.loads(items)
        except (ValueError, TypeError):
            items = []
    if not isinstance(items, (list, tuple)):
        items = []
    item_rows = ""
    for item in items:
        if not isinstance(item, dict):
            continue
        item_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{}</td></tr>".format(
            _esc(item.get("designation", "")),
            _safe_amount(item.get("quantity", 0), 2),
            _safe_amount(item.get("unit_price", 0), 2),
            _esc(item.get("tax_rate", "0")),
            _safe_amount(item.get("net_amount", 0), 2),
            )
    inv_type = inv.get("invoice_type", "")
    doc_label = "FACTURE D'AVOIR" if inv_type == "creditNote" else "FACTURE"
    avoir_banner = (
        '<div style="border:2px solid #b91c1c;color:#b91c1c;padding:8px 12px;'
        'margin:8px 0;font-weight:bold;text-align:center">FACTURE D\'AVOIR</div>'
        if inv_type == "creditNote" else ""
    )
    total_tax_t = (inv.get("total_tax_t_amount")
                   if inv.get("total_tax_t_amount") is not None else inv.get("total_tax18", 0))
    total_tax_r = (inv.get("total_tax_r_amount")
                   if inv.get("total_tax_r_amount") is not None else inv.get("total_tax5", 0))
    try:
        _disc_val = float(str(inv.get("discount_amount", "0")).replace(",", "."))
    except (ValueError, TypeError):
        _disc_val = 0
    return render_template("dashboard/print.html",
        doc_label=_esc(doc_label),
        avoir_banner=avoir_banner,
        seller_legal_form=_esc(company.get("legal_form", "")),
        seller_tax_regime=_esc(company.get("tax_regime", "")),
        seller_capital=_esc(company.get("capital", "")),
        invoice_type=_esc(inv_type),
        recipient_type=_esc(inv.get("recipient_type", "")),
        reference_invoice_id=_esc(inv.get("reference_invoice_id", "") or ""),
        discount_display=_esc(fmt_money(abs(_disc_val), 2)),
        total_exempt=_esc(fmt_money(inv.get("total_exempt_amount", "0"))),
        additional_cent_tax=_esc(fmt_money(inv.get("additional_cent_tax", "0"))),
        electronic_stamp_duty=_esc(fmt_money(inv.get("electronic_stamp_duty", "0"))),
        invoice_number=_esc(inv.get("invoice_number", "")),
        invoice_date=_esc((inv.get("invoice_date") or inv.get("date_facture")
                           or inv.get("certification_date") or "")[:10]),
        currency=_esc(inv.get("currency", "XAF")),
        seller_name=_esc(inv.get("seller_name", company.get("name", ""))),
        seller_addr=_esc(inv.get("seller_address", company.get("address", ""))),
        seller_niu=_esc(inv.get("seller_niu", company.get("tax_number", ""))),
        seller_rccm=_esc(inv.get("seller_rccm") or company.get("rc_number", "")),
        buyer_name=_esc(inv.get("buyer_name", "")),
        buyer_niu=_esc(inv.get("buyer_niu", "")),
        buyer_addr=_esc(inv.get("buyer_address", "")),
        buyer_phone=_esc(inv.get("buyer_phone", "")),
        payment_method=_esc(inv.get("payment_method", "")),
        amount_due=_esc(fmt_money(inv.get("amount_due", "0"))),
        total_ht=_esc(fmt_money(inv.get("total_ht", "0"))),
        total_tax18=_esc(fmt_money(total_tax_t)),
        total_tax5=_esc(fmt_money(total_tax_r)),
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


def _sqlite_alive():
    try:
        with sqlite_db.get_cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            return bool(cur.fetchone())
    except Exception:
        return False


@bp.route("/health", methods=["GET"])
def health():
    db_ok = _sqlite_alive()
    payload = {"status": "ok" if db_ok else "error",
               "checks": {"sqlite": "up" if db_ok else "down"}}
    return jsonify(payload), 200 if db_ok else 503


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
