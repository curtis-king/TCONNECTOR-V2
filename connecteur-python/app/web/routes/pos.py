"""Blueprint pos — Point de vente : /pos* /api/products* /api/pos*.

Converti depuis app/web/parking/pos.py (étape A6). Corps des fonctions
strictement inchangés ; les routes sont décorées @bp.route avec les mêmes
URL / méthodes que l'ancien app.add_url_rule() de dashboard.py, et le
wrapping _login_required est reproduit à l'identique.
"""

import functools
import json
import logging
import os
from flask import Blueprint, jsonify, render_template, request, send_file, session
from app.config.manager import get_config, save_config
from app.core.utils import is_counter_customer
from app.domain import invoices as invoice_engine, pdf as pdf_generator, pos as pos_engine
from app.storage import db as sqlite_db
from app.web.common import _esc, _page
from app.web.auth.security import _login_required, _current_identity
from app.web.auth import user_auth

logger = logging.getLogger("t-connector.web.pos")

bp = Blueprint("pos", __name__)




@bp.route("/api/products", methods=["GET"])
@_login_required
def api_list_products():
    products = pos_engine.list_products(
        type_filter=request.args.get("type"),
        search=request.args.get("search"),
        limit=min(int(request.args.get("limit", 200)), 500)
    )
    return jsonify({"products": products, "total": len(products)})


@bp.route("/api/products", methods=["POST"])
@_login_required
def api_create_product():
    data = request.get_json(silent=True) or {}
    try:
        result = pos_engine.create_product(data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/pos")
@_login_required
def pos_page():
    stats = pos_engine.count_tickets()
    tickets_list = pos_engine.list_tickets(limit=8)
    current_vendeur_id = session.get("vendeur_id")
    vendeurs = pos_engine.list_vendeurs()
    top_products = pos_engine.top_selling_products(limit=10)

    def _pos_tile(p, rank=None):
        sold = int(p.get("total_vendu", 0) or 0)
        badge = '<span class="badge-sold">TOP {}</span>'.format(rank) if rank and sold > 0 else ""
        stock_val = int(round(p.get("stock_reel", 0) or 0))
        stock_cls = "out" if stock_val <= 0 else ("low" if stock_val < 5 else "")
        return ('<div class="pos-tile" data-id="{pid}" data-ref="{ref}" data-barcode="{bc}" '
                'data-des="{des}" data-prix="{prix}" data-tva="{tva}" '
                'onclick="addToCartFromTile(this)">{badge}'
                '<div class="pos-tile-nom">{des2}</div>'
                '<div class="pos-tile-ref">{ref2}</div>'
                '<div class="pos-tile-foot">'
                '<span class="pos-tile-prix">{prix2}</span>'
                '<span class="pos-tile-stock {stock_cls}">{stock}</span>'
                '</div>'
                '</div>').format(
                    pid=str(p["id"]), ref=_esc(p.get("ref", "")),
                    bc=_esc(p.get("barcode", "") or ""),
                    des=_esc(p.get("designation", "")),
                    prix=p.get("prix_vente", 0) or 0,
                    tva=_esc(p.get("tva_code", "18") or "18"),
                    badge=badge,
                    des2=_esc(p.get("designation", "")),
                    ref2=_esc(p.get("ref", "")),
                    prix2="{:,}".format(int(round(p.get("prix_vente", 0) or 0))),
                    stock_cls=stock_cls,
                    stock="{:,}".format(stock_val))

    top10_tiles = "".join(_pos_tile(p, i + 1) for i, p in enumerate(top_products))
    if not top10_tiles:
        top10_tiles = ('<div class="pos-results-empty">Aucun article disponible. '
                       'Utilisez <b>Sync articles</b> pour importer le catalogue Sage.</div>')

    ticket_rows_html = ""
    for t in tickets_list["tickets"]:
        ticket_rows_html += ('<div class="pos-last-row"><span class="num">{num}</span>'
                              '<span class="time">{time}</span>'
                              '<span class="amt">{amt} FCFA</span>'
                              '<span class="pay">{pay}</span></div>').format(
            num=_esc(t.get("numero", "")), time=_esc(t.get("date_ticket", "")[11:19] or ""),
            amt="{:,.0f}".format(t.get("montant_ttc", 0)),
            pay=_esc((t.get("mode_paiement", "") or "").replace("_", " "))
        )
    if not ticket_rows_html:
        ticket_rows_html = '<div class="pos-results-empty">Aucun ticket pour le moment</div>'

    vendeur_opts_html = '<option value="">Sans vendeur</option>'
    for v in vendeurs:
        label = "{} {}".format(v.get("prenom", "") or "", v.get("nom", "") or "").strip()
        sel = ' selected' if current_vendeur_id and v["id"] == current_vendeur_id else ""
        vendeur_opts_html += '<option value="{}"{}>{}</option>'.format(
            v["id"], sel, _esc(label))

    try:
        sfec_cfg = get_config().get("sfec", {})
    except Exception:
        sfec_cfg = {}

    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT id, code, nom, niu, rccm, is_taxable, type, type_raw FROM contacts WHERE type = 'client' AND est_actif = 1 ORDER BY nom LIMIT 1000")
        pos_clients = sqlite_db.rows_to_list(cur.fetchall())
        pos_clients_data = [
            {"id": c["id"], "code": c["code"], "nom": c["nom"],
             "niu": c.get("niu", ""), "rccm": c.get("rccm", ""),
             "type": c.get("type", ""),
             "type_raw": ("individual" if is_counter_customer(
                 c.get("code"), c.get("nom"), sfec_cfg)
                 else c.get("type_raw", "")),
             "is_taxable": c.get("is_taxable", 1)} for c in pos_clients
        ]

    pos_clients_html = '<option value="CLI-CPT" selected>Client comptoir</option>'
    for c in pos_clients:
        pos_clients_html += '<option value="{}">{}</option>'.format(
            _esc(c["code"]), _esc("{} - {}".format(c["code"], c["nom"])))

    stats_html = ('<div class="pos-stat-pill"><b>{tj}</b><span>Tickets aujourd&#39;hui</span></div>'
                  '<div class="pos-stat-pill"><b>{cj} FCFA</b><span>CA du jour</span></div>'
                  '<div class="pos-stat-pill"><b>{ct} FCFA</b><span>CA total</span></div>').format(
                      tj=str(stats.get("tickets_jour", 0)),
                      cj="{:,}".format(int(round(stats.get("ca_jour", 0)))),
                      ct="{:,}".format(int(round(stats.get("ca_total", 0)))))

    body = render_template("pos/index.html",
        stats_html=stats_html,
        top10_tiles=top10_tiles,
        vendeur_opts_html=vendeur_opts_html,
        pos_clients_html=pos_clients_html,
        pos_clients_data=pos_clients_data,
        ticket_rows_html=ticket_rows_html,
        stock_ctl_js="true" if pos_engine.stock_control_enabled() else "false",
    )
    return _page(body)


@bp.route("/pos/ticket/<int:ticket_id>/print")
@_login_required
def pos_print_ticket(ticket_id):
    ticket = pos_engine.get_ticket(ticket_id)
    if not ticket:
        return "<h1>Ticket non trouve</h1>", 404

    v = pdf_generator.ticket_view(ticket)
    qr = v["sfec_qr"]
    sfec_qr_src = qr if qr.startswith(("data:", "http")) and len(qr) < 40000 else ""

    auto = request.args.get("auto") == "1"
    return render_template("pos/ticket_print.html",
        v=v,
        stars=pdf_generator.TICKET_STARS,
        logo_src=pdf_generator.image_data_uri(v["logo_path"]),
        sfec_qr_src=sfec_qr_src,
        ticket_id=ticket["id"],
        auto_print="true" if auto else "false",
    )


@bp.route("/api/pos/ticket/<int:ticket_id>/print-silent", methods=["POST"])
@_login_required
def api_ticket_print_silent(ticket_id):
    """Impression silencieuse du ticket 80 mm sur l'imprimante du site actif."""
    ticket = pos_engine.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"success": False, "error": "Ticket non trouve"}), 404

    if not pdf_generator.HAS_REPORTLAB:
        return jsonify({"success": False, "error": "reportlab non installe"}), 500

    from app.print_hijack import auto_printer, sites as sites_mod

    site, reason = sites_mod.resolve_active_site()
    printer = sites_mod.site_printer(site, "ticket") if site else ""
    if not printer:
        # Repli legacy : imprimante A4 config si aucun site ne matche pas de ticket.
        if site is None and "aucun site configure" in reason:
            printer = ""
        if not printer:
            return jsonify({
                "success": False,
                "error": "Aucune imprimante ticket (80 mm) definie pour le site actif ({})".format(reason),
            }), 409

    if not sites_mod.printer_connected(printer):
        return jsonify({
            "success": False,
            "error": "Imprimante ticket '{}' absente/disconnectee".format(printer),
        }), 409

    if sites_mod.is_paused():
        return jsonify({"success": False, "error": "Impressions en PAUSE (kill-switch)"}), 409

    try:
        pdf_bytes = pdf_generator.generate_ticket_pdf(ticket)
        if not pdf_bytes:
            return jsonify({"success": False, "error": "Generation PDF echouee"}), 500

        import tempfile
        out_dir = os.path.join(tempfile.gettempdir(), "tconnector", "pos")
        os.makedirs(out_dir, exist_ok=True)
        numero = (ticket.get("numero") or "ticket").replace("/", "-")
        out_path = os.path.join(out_dir, "{}_{}.pdf".format(numero, auto_printer.timestamp_tag()))
        with open(out_path, "wb") as f:
            f.write(pdf_bytes)

        dry_run = sites_mod.is_dry_run()
        ok, err = auto_printer.print_pdf(out_path, printer, "", dry_run=dry_run)
        if not ok:
            return jsonify({"success": False, "error": err or "impression echouee"}), 500

        logger.info("Ticket %s imprime sur '%s' (dry_run=%s)", numero, printer, dry_run)
        return jsonify({"success": True, "printer": printer, "dry_run": dry_run})
    except Exception as e:  # noqa: BLE001
        logger.exception("Impression ticket %s impossible: %s", ticket_id, e)
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/pos/config", methods=["GET", "POST"])
@_login_required
def api_pos_config():
    cfg = get_config()
    if request.method == "GET":
        return jsonify({"success": True, "stock_control": pos_engine.stock_control_enabled()})
    data = request.get_json(silent=True) or {}
    try:
        pos = cfg.get("pos", {}) or {}
        if "stock_control" in data:
            pos["stock_control"] = bool(data["stock_control"])
        cfg["pos"] = pos
        save_config(cfg)
        user_auth.audit("pos.stock_control", "Controle de stock: {}".format(
            "actif" if pos.get("stock_control", True) else "inactif"),
            email=(_current_identity() or {}).get("email", ""))
        return jsonify({"success": True, "stock_control": bool(pos.get("stock_control", True))})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/api/pos/ticket", methods=["POST"])
@_login_required
def api_create_ticket():
    data = request.get_json(silent=True) or {}
    try:
        result = pos_engine.create_ticket(data)
        if result.get("success"):
            user_auth.audit("pos.ticket", "Ticket {} cree ({} FCFA)".format(
                result.get("numero", ""), result.get("montant_ttc", 0)),
                email=(_current_identity() or {}).get("email", ""))
        return jsonify(result)
    except Exception:
        import logging
        logging.getLogger("t-connector.pos").exception("Creation ticket route echouee")
        return jsonify({"success": False, "error": "Erreur interne de creation du ticket"}), 400


@bp.route("/api/pos/tickets")
@_login_required
def api_list_tickets():
    try:
        limit = max(1, min(int(request.args.get("limit", 100)), 500))
    except (TypeError, ValueError):
        limit = 100
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    result = pos_engine.list_tickets(
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        vendeur_id=request.args.get("vendeur_id"),
        search=request.args.get("search"),
        limit=limit, offset=offset
    )
    return jsonify(result)


@bp.route("/api/pos/ticket/<int:ticket_id>")
@_login_required
def api_get_ticket(ticket_id):
    ticket = pos_engine.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Non trouve"}), 404
    return jsonify(ticket)


@bp.route("/api/pos/stats")
@_login_required
def api_pos_stats():
    return jsonify(pos_engine.count_tickets())


@bp.route("/api/pos/products/search")
@_login_required
def api_search_products():
    q = request.args.get("q", "")
    return jsonify(pos_engine.search_products(q, limit=20))


def get_setting(key, default=""):
    try:
        return sqlite_db.get_setting(key, default)
    except Exception:
        return default


@bp.route("/api/products/barcode")
@_login_required
def api_find_barcode():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"error": "code requis"}), 400
    p = pos_engine.find_product_by_barcode(code)
    if p:
        return jsonify({"found": True, "product": p})
    return jsonify({"found": False})


@bp.route("/api/pos/ticket/<int:ticket_id>/pdf")
@_login_required
def api_ticket_pdf(ticket_id):
    ticket = pos_engine.get_ticket(ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket non trouve"}), 404

    if not pdf_generator.HAS_REPORTLAB:
        return jsonify({"error": "reportlab non installe - pip install reportlab"}), 500

    try:
        pdf_bytes = pdf_generator.generate_ticket_pdf(ticket)
        if not pdf_bytes:
            return jsonify({"error": "Erreur generation PDF"}), 500

        from flask import send_file
        buf = __import__("io").BytesIO(pdf_bytes)
        numero = ticket.get("numero", "ticket").replace("/", "-")
        return send_file(buf, as_attachment=True,
                         download_name="{}.pdf".format(numero),
                         mimetype="application/pdf")
    except Exception as e:
        return jsonify({"error": str(e)}), 500
