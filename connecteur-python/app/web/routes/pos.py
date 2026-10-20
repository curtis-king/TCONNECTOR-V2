"""Blueprint pos — Point de vente : /pos* /api/products* /api/pos*.

Converti depuis app/web/parking/pos.py (étape A6). Corps des fonctions
strictement inchangés ; les routes sont décorées @bp.route avec les mêmes
URL / méthodes que l'ancien app.add_url_rule() de dashboard.py, et le
wrapping _login_required est reproduit à l'identique.
"""

import functools
import json
from flask import Blueprint, jsonify, render_template, request, send_file, session
from app.config.manager import get_config, save_config
from app.domain import invoices as invoice_engine, pdf as pdf_generator, pos as pos_engine
from app.storage import db as sqlite_db
from app.web.common import _esc, _page
from app.web.auth.security import _login_required

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
        vendeur_opts_html += '<option value="{}"{}>{}</option>'.format(v["id"], sel, label)

    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT id, code, nom, niu, rccm, is_taxable, type FROM contacts WHERE type = 'client' AND est_actif = 1 ORDER BY nom LIMIT 1000")
        pos_clients = sqlite_db.rows_to_list(cur.fetchall())
        pos_clients_js = json.dumps([
        {"id": c["id"], "code": c["code"], "nom": c["nom"],
         "niu": c.get("niu", ""), "rccm": c.get("rccm", ""),
         "is_taxable": c.get("is_taxable", 1)} for c in pos_clients
    ], ensure_ascii=False)

    pos_clients_html = '<option value="|CLI-CPT|Client comptoir" selected>Client comptoir</option>'
    for c in pos_clients:
        pos_clients_html += '<option value="|{}|{}">{}</option>'.format(
            _esc(c["code"]), _esc(c["nom"]), _esc("{} - {}".format(c["code"], c["nom"])))

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
        pos_clients_js=pos_clients_js,
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

    return render_template("pos/ticket_print.html",
        v=v,
        stars=pdf_generator.TICKET_STARS,
        logo_src=pdf_generator.image_data_uri(v["logo_path"]),
        sfec_qr_src=sfec_qr_src,
        ticket_id=ticket["id"],
    )


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
        return jsonify({"success": True, "stock_control": bool(pos.get("stock_control", True))})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/api/pos/ticket", methods=["POST"])
@_login_required
def api_create_ticket():
    data = request.get_json(silent=True) or {}
    try:
        result = pos_engine.create_ticket(data)
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/api/pos/tickets")
@_login_required
def api_list_tickets():
    result = pos_engine.list_tickets(
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        vendeur_id=request.args.get("vendeur_id"),
        search=request.args.get("search"),
        limit=min(int(request.args.get("limit", 100)), 500)
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
