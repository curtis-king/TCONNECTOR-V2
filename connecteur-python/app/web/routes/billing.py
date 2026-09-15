"""Blueprint billing — Facturation : /billing* /api/invoices*.

Converti depuis app/web/parking/billing.py (étape A6). Corps des fonctions
strictement inchangés ; les routes sont décorées @bp.route avec les mêmes
URL / méthodes que l'ancien app.add_url_rule() de dashboard.py, et le
wrapping _login_required est reproduit à l'identique.
"""

import functools
import json
from flask import Blueprint, jsonify, render_template, request, send_file
from app.domain import invoices as invoice_engine, pdf as pdf_generator, pos as pos_engine
from app.storage import db as sqlite_db
from app.sync import bidirectional as sync_bidirectional
from app.web.auth import user_auth
from app.web.auth.security import _current_identity, _login_required
from app.web.common import _esc, _page

bp = Blueprint("billing", __name__)




@bp.route("/billing")
@_login_required
def billing_page():
    stats = invoice_engine.count_invoices()
    bi_stats = sync_bidirectional.get_sync_stats()

    with sqlite_db.get_cursor() as cur:
        cur.execute("SELECT COUNT(*) c FROM invoices WHERE sfec_statut IN ('CERTIFIE', 'DEJA_CERTIFIE')")
        stats["certifiees"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM invoices WHERE sfec_statut = 'EN_COURS'")
        stats["certif_en_cours"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM invoices WHERE sfec_statut = 'ERREUR'")
        stats["certif_erreur"] = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) c FROM invoices WHERE statut != 'brouillon' AND sfec_statut NOT IN ('CERTIFIE', 'DEJA_CERTIFIE')")
        stats["non_certifiees"] = cur.fetchone()["c"]
        cur.execute("""
            SELECT COUNT(*) c FROM invoices
            WHERE source IN ('web', 'pos') AND synced_sage = 0 AND statut != 'brouillon'
              AND (source != 'pos' OR (sfec_num_certif IS NOT NULL AND sfec_num_certif != ''))
        """)
        stats["pending_sage"] = cur.fetchone()["c"]
        cur.execute("SELECT COALESCE(SUM(montant_restant), 0) t, COUNT(*) c FROM invoices WHERE statut != 'brouillon' AND montant_restant > 0")
        m = cur.fetchone()
        stats["impayes_total"] = m["t"]
        stats["impayes_nb"] = m["c"]
        cur.execute("""
            SELECT COUNT(*) c, COALESCE(SUM(montant_ht), 0) ht, COALESCE(SUM(montant_tva), 0) tva,
                   COALESCE(SUM(montant_ttc), 0) ttc
            FROM invoices
            WHERE statut != 'brouillon' AND strftime('%Y-%m', substr(date_facture, 1, 10)) = strftime('%Y-%m', 'now')
        """)
        m = cur.fetchone()
        stats["mois_nb"] = m["c"]
        stats["mois_ht"] = m["ht"]
        stats["mois_tva"] = m["tva"]
        stats["mois_ttc"] = m["ttc"]

    statut_opts = ('<option value="">Tous</option><option value="brouillon">Brouillon</option>'
                   '<option value="valide">Validee</option><option value="a_comptabiliser">A comptabiliser</option>'
                   '<option value="a_comptabilise">A comptabilise</option>')
    source_opts = ('<option value="">Toutes</option><option value="web">Web</option>'
                   '<option value="pos">POS</option><option value="sage">Sage</option>')

    return _page(render_template("billing/list.html",
        total=stats.get("total", 0), brouillons=stats.get("brouillons", 0),
        validees=stats.get("validees", 0), certifiees=stats.get("certifiees", 0),
        ca=stats.get("ca_total", 0), pushed=bi_stats.get("pushed", 0),
        certif_en_cours=stats.get("certif_en_cours", 0), certif_erreur=stats.get("certif_erreur", 0),
        nb_imp=stats.get("impayes_total", 0), pending_sage=stats.get("pending_sage", 0),
        mois_nb=stats.get("mois_nb", 0), mois_ht=stats.get("mois_ht", 0),
        mois_tva=stats.get("mois_tva", 0), mois_ttc=stats.get("mois_ttc", 0),
        statut_opts=statut_opts, source_opts=source_opts,
        last_sync=bi_stats.get("last_sync", "jamais")[:19].replace("T", " ") if bi_stats.get("last_sync") else "jamais",
    ))


@bp.route("/api/invoices/list")
@_login_required
def api_invoices_list():
    page = max(1, request.args.get("page", 1, type=int))
    limit = max(1, min(request.args.get("limit", 25, type=int), 200))
    res = invoice_engine.list_invoices(
        type_doc=request.args.get("type") or None,
        statut=request.args.get("statut") or None,
        source=request.args.get("source") or None,
        search=request.args.get("search") or None,
        date_from=request.args.get("date_from") or None,
        date_to=request.args.get("date_to") or None,
        limit=limit, offset=(page - 1) * limit,
        sort_by=request.args.get("sort_by") or "date_facture",
        sort_dir=request.args.get("sort_dir") or "DESC"
    )
    total = res.get("total", 0)
    pages = max(1, (total + limit - 1) // limit)
    return jsonify({"invoices": res.get("invoices", []), "total": total,
                    "page": page, "pages": pages, "limit": limit})


@bp.route("/billing/invoice/new")
@_login_required
def invoice_new_page():
    return _invoice_form_page(None)


@bp.route("/billing/invoice/<int:invoice_id>")
@_login_required
def invoice_detail_page(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return "<h1>Facture non trouvee</h1><p><a href='/billing'>Retour</a></p>", 404
    lignes_rows = ""
    for l in inv.get("lignes", []):
        lignes_rows += "<tr><td>{}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.2f}</td><td style='text-align:right'>{}</td><td style='text-align:right'>{:,.0f}</td><td style='text-align:right'>{:,.0f}</td><td style='text-align:right'>{:,.0f}</td></tr>".format(
            _esc(l.get("designation", "")), l.get("quantite", 1), l.get("prix_unitaire", 0),
            _esc("{}%".format(l.get("taux_tva", 18))), l.get("montant_ht", 0), l.get("montant_tva", 0), l.get("montant_ttc", 0)
        )
    ss = inv.get("sfec_statut", "")
    sfec_html = ""
    type_doc = inv.get("type_doc", "vente")
    type_badge = ('<span class="badge badge-warn">AVOIR</span>' if type_doc == "avoir"
                  else '<span class="badge badge-ok">VENTE</span>')
    meta_rows = "<tr><td>Type</td><td>{}</td></tr>".format(type_badge)
    meta_rows += "<tr><td>Mode paiement</td><td>{}</td></tr>".format(_esc(inv.get("payment_method", "") or "-"))
    meta_rows += "<tr><td>Devise</td><td>{}</td></tr>".format(_esc(inv.get("devise", "XAF")))
    if type_doc == "avoir":
        meta_rows += "<tr><td>Facture d'origine</td><td>{}</td></tr>".format(
            _esc(inv.get("reference_invoice_id", "") or "-"))    
    if ss:
        sfb = "badge-ok" if ss in ("CERTIFIE", "DEJA_CERTIFIE") else "badge-err" if ss == "ERREUR" else "badge-warn"
        sfec_html = '<tr><td>SFEC</td><td><span class="badge {}">{}</span></td></tr><tr><td>N Certif</td><td>{}</td></tr>'.format(sfb, _esc(ss), _esc(inv.get("sfec_num_certif", "")[:30] or "-"))
    return _page(render_template("billing/detail.html",
        meta_rows=meta_rows,
        numero=_esc(inv.get("numero", "")),
        inv_id=inv["id"],
        date_facture=_esc(inv.get("date_facture", "")),
        reference=_esc(inv.get("reference", "") or "-"),
        tiers_nom=_esc(inv.get("tiers_nom", "") or "N/A"),
        tiers_code=_esc(inv.get("tiers_code", "") or ""),
        statut=_esc(inv.get("statut", "")),
        source=_esc(inv.get("source", "web")),
        sfec_html=sfec_html,
        notes=_esc(inv.get("notes", "") or "-"),
        lignes_rows=lignes_rows,
        montant_ht=inv.get("montant_ht", 0),
        montant_tva=inv.get("montant_tva", 0),
        montant_ttc=inv.get("montant_ttc", 0),
    ))


@bp.route("/billing/invoice/<int:invoice_id>/edit")
@_login_required
def invoice_edit_page(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return "<h1>Facture non trouvee</h1><p><a href='/billing'>Retour</a></p>", 404
    return _invoice_form_page(inv)


def _invoice_form_page(inv):
    is_edit = inv is not None
    title = "Modifier Facture" if is_edit else "Nouvelle Facture"
    numero = inv.get("numero", "") if is_edit else ""
    date_facture = inv.get("date_facture", "") if is_edit else ""
    date_echeance = inv.get("date_echeance", "") if is_edit else ""
    reference = inv.get("reference", "") if is_edit else ""
    tiers_code = inv.get("tiers_code", "") if is_edit else ""
    tiers_nom = inv.get("tiers_nom", "") if is_edit else ""
    tiers_niu = inv.get("tiers_niu", "") if is_edit else ""
    tiers_email = inv.get("tiers_email", "") if is_edit else ""
    tiers_telephone = inv.get("tiers_telephone", "") if is_edit else ""
    tiers_adresse = inv.get("tiers_adresse", "") if is_edit else ""
    tiers_type = inv.get("tiers_type", "business") if is_edit else "business"
    notes = inv.get("notes", "") if is_edit else ""
    statut = inv.get("statut", "brouillon") if is_edit else "brouillon"
    type_doc = inv.get("type_doc", "vente") if is_edit else "vente"
    payment_method = inv.get("payment_method", "bank_transfer") if is_edit else "bank_transfer"
    devise = inv.get("devise", "XAF") if is_edit else "XAF"
    reference_invoice_id = inv.get("reference_invoice_id", "") if is_edit else ""
    recipient_rccm = inv.get("recipient_rccm", "") if is_edit else ""

    # Cibles possibles d'un avoir : ventes certifiées non déjà avoirées
    with sqlite_db.get_cursor() as cur:
        cur.execute("""
            SELECT i.id, i.numero, i.date_facture, i.montant_ttc
            FROM invoices i
            WHERE i.type_doc = 'vente'
              AND i.sfec_statut IN ('CERTIFIE', 'DEJA_CERTIFIE')
              AND NOT EXISTS (
                  SELECT 1 FROM invoices a
                  WHERE a.type_doc = 'avoir' AND a.reference_invoice_id = i.numero
              )
            ORDER BY i.date_facture DESC LIMIT 200
        """)

    certified_sales_json = json.dumps(sqlite_db.rows_to_list(cur.fetchall()), ensure_ascii=False)
    lignes_json = json.dumps(inv.get("lignes", [])) if is_edit else "[]"
    contacts_json = json.dumps(pos_engine.list_contacts(limit=200))
    products_json = json.dumps(pos_engine.list_products(limit=200))
    tax_rates_json = json.dumps(pos_engine.list_tax_rates())

    return _page(render_template("billing/form.html",
        type_doc=type_doc, payment_method=payment_method, devise=devise,
        reference_invoice_id=reference_invoice_id, recipient_rccm=recipient_rccm,
        certified_sales_json=certified_sales_json,
        d_vente="selected" if type_doc == "vente" else "",
        d_avoir="selected" if type_doc == "avoir" else "",
        title=title, numero=_esc(numero), date_facture=_esc(date_facture), date_echeance=_esc(date_echeance),
        reference=_esc(reference), tiers_code=_esc(tiers_code), tiers_nom=_esc(tiers_nom),
        tiers_niu=_esc(tiers_niu), tiers_email=_esc(tiers_email), tiers_telephone=_esc(tiers_telephone),
        tiers_adresse=_esc(tiers_adresse), notes=_esc(notes),
        s_brouillon="selected" if statut == "brouillon" else "",
        s_valide="selected" if statut == "valide" else "",
        s_acompt="selected" if statut == "a_comptabiliser" else "",
        t_bus="selected" if tiers_type == "business" else "",
        t_ind="selected" if tiers_type == "individual" else "",
        t_gov="selected" if tiers_type == "government" else "",
        t_for="selected" if tiers_type == "foreign" else "",
        readonly="readonly" if is_edit else "",
        contacts_json=contacts_json, products_json=products_json, tax_rates_json=tax_rates_json,
        lignes_json=lignes_json, is_edit_js="true" if is_edit else "false",
        inv_id_js=inv["id"] if is_edit else "null",
        btn_text="Modifier" if is_edit else "Enregistrer"
    ) + "\n")


@bp.route("/api/invoices", methods=["GET"])
@_login_required
def api_list_invoices():
    result = invoice_engine.list_invoices(
        type_doc=request.args.get("type"),
        statut=request.args.get("statut"),
        source=request.args.get("source"),
        search=request.args.get("search"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        limit=min(int(request.args.get("limit", 200)), 500),
        offset=int(request.args.get("offset", 0))
    )
    return jsonify(result)


@bp.route("/api/invoices", methods=["POST"])
@_login_required
def api_create_invoice():
    data = request.get_json(silent=True) or {}
    try:
        result = invoice_engine.create_invoice(data)
        who = (_current_identity() or {}).get("email", "")
        user_auth.audit("facture.creee", "Facture {} creee".format(result.get("numero", "")), email=who)
        return jsonify({"success": True, "id": result["id"], "numero": result["numero"]})
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        code = 422 if ("avoir" in low or "reference_invoice_id" in low
                       or "certifi" in low) else 400
        return jsonify({"success": False, "error": msg}), code


@bp.route("/api/invoices/<int:invoice_id>", methods=["GET"])
@_login_required
def api_get_invoice(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return jsonify({"error": "Non trouvee"}), 404
    return jsonify(inv)


@bp.route("/api/invoices/<int:invoice_id>", methods=["PUT"])
@_login_required
def api_update_invoice(invoice_id):
    data = request.get_json(silent=True) or {}
    try:
        result = invoice_engine.update_invoice(invoice_id, data)
        if not result:
            return jsonify({"error": "Non trouvee"}), 404
        who = (_current_identity() or {}).get("email", "")
        user_auth.audit("facture.modifiee", "Facture {} modifiee".format(result.get("numero", "")), email=who)
        return jsonify({"success": True, "id": result["id"], "numero": result["numero"]})
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        code = 422 if ("avoir" in low or "reference_invoice_id" in low
                       or "certifi" in low) else 400
        return jsonify({"success": False, "error": msg}), code


@bp.route("/api/invoices/<int:invoice_id>", methods=["DELETE"])
@_login_required
def api_delete_invoice(invoice_id):
    try:
        numero = ""
        inv = invoice_engine.get_invoice(invoice_id)
        if inv:
            numero = inv.get("numero", "")
        ok = invoice_engine.delete_invoice(invoice_id)
        if not ok:
            return jsonify({"error": "Non trouvee"}), 404
        who = (_current_identity() or {}).get("email", "")
        user_auth.audit("facture.supprimee", "Facture {} supprimee".format(numero), email=who)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@bp.route("/api/invoices/<int:invoice_id>/push-sage", methods=["POST"])
@_login_required
def api_invoice_push_sage(invoice_id):
    try:
        result = sync_bidirectional.push_invoice_to_sage(invoice_id)
        code = 200 if result.get("success") else 400
        if result.get("success"):
            who = (_current_identity() or {}).get("email", "")
            user_auth.audit("facture.poussee_sage", "Facture #{} poussee vers Sage".format(invoice_id), email=who)
        return jsonify(result), code
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/api/invoices/stats")
@_login_required
def api_invoice_stats():
    return jsonify(invoice_engine.count_invoices())


@bp.route("/api/invoices/<int:invoice_id>/pdf")
@_login_required
def api_invoice_pdf(invoice_id):
    inv = invoice_engine.get_invoice(invoice_id)
    if not inv:
        return jsonify({"error": "Facture non trouvee"}), 404

    if not pdf_generator.HAS_REPORTLAB:
        return jsonify({"error": "reportlab non installe - pip install reportlab"}), 500

    try:
        pdf_bytes = pdf_generator.generate_invoice_pdf(inv)
        if not pdf_bytes:
            return jsonify({"error": "Erreur generation PDF"}), 500

        from flask import send_file
        buf = __import__("io").BytesIO(pdf_bytes)
        numero = inv.get("numero", "facture").replace("/", "-")
        return send_file(buf, as_attachment=True,
                         download_name="{}.pdf".format(numero),
                         mimetype="application/pdf")
    except Exception as e:
        return jsonify({"error": str(e)}), 500



