"""Blueprint billing — Facturation : /billing* /api/invoices*.

Converti depuis app/web/parking/billing.py (étape A6). Corps des fonctions
strictement inchangés ; les routes sont décorées @bp.route avec les mêmes
URL / méthodes que l'ancien app.add_url_rule() de dashboard.py, et le
wrapping _login_required est reproduit à l'identique.
"""

import functools
from flask import Blueprint, jsonify, render_template, request, send_file
from app.domain import invoices as invoice_engine, pdf as pdf_generator, pos as pos_engine
from app.integration.sage.database import reset_sfec_monitor
from app.storage import db as sqlite_db
from app.sync import bidirectional as sync_bidirectional
from app.web.auth import user_auth
from app.web.auth.security import _current_identity, _login_required
from app.web.common import _csv_response, _esc, _page

bp = Blueprint("billing", __name__)




@bp.route("/billing")
@_login_required
def billing_page():
    stats = invoice_engine.count_invoices()
    bi_stats = sync_bidirectional.get_sync_stats()

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
        type_doc = request.args.get("type_doc", "") or None,
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
    filters = {
        "type_doc": request.args.get("type_doc", "") or None,
        "statut": request.args.get("statut") or None,
        "source": request.args.get("source") or None,
        "search": request.args.get("search") or None,
        "date_from": request.args.get("date_from") or None,
        "date_to": request.args.get("date_to") or None,
    }
    stats = invoice_engine.filtered_invoices_stats(**filters)
    return jsonify({"invoices": res.get("invoices", []), "total": total,
                    "page": page, "pages": pages, "limit": limit,
                    "stats": stats})


@bp.route("/api/invoices/export", methods=["GET"])
@_login_required
def api_invoices_export():
    """Export CSV (max 5000 lignes) appliquant les memes filtres que la liste."""
    res = invoice_engine.list_invoices(
        type_doc=request.args.get("type_doc", "") or None,
        statut=request.args.get("statut") or None,
        source=request.args.get("source") or None,
        search=request.args.get("search") or None,
        date_from=request.args.get("date_from") or None,
        date_to=request.args.get("date_to") or None,
        limit=5000, offset=0)
    rows = [
        [r.get(k, "") for k in ("numero", "type_doc", "date_facture", "tiers_nom",
                                "statut", "sfec_statut", "montant_ttc",
                                "montant_restant", "devise")]
        for r in res.get("invoices", [])
    ]
    header = ["numero", "type", "date", "client", "statut", "statut SFEC",
              "montant TTC", "reste a payer", "devise"]
    return _csv_response("factures.csv", header, rows)


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
        sfec_html = '<tr><td>SFEC</td><td><span class="badge {}">{}</span></td></tr><tr><td>N Certif</td><td>{}</td></tr>'.format(
            sfb, _esc(ss), _esc((inv.get("sfec_num_certif") or "")[:30] or "-"))
    statut = (inv.get("statut") or "").lower()
    statut_class = "badge-ok" if statut in ("valide", "a_comptabilise", "comptabilise") else (
        "badge-err" if statut in ("annule", "annulee") else "badge-warn")
    locked = invoice_engine.is_invoice_locked(inv)
    can_certify = (not locked) and statut not in ("", "brouillon", "draft")
    return _page(render_template("billing/detail.html",
        montant_ht_brut=inv.get("montant_ht_brut") or 0,
        montant_restant=inv.get("montant_restant") or 0,
        total_line_discount_amount=inv.get("total_line_discount_amount") or 0,
        discount_amount=inv.get("discount_amount") or 0,
        total_exempt_amount=inv.get("total_exempt_amount") or 0,
        total_tax_t_amount=inv.get("total_tax_t_amount") or 0,
        total_tax_r_amount=inv.get("total_tax_r_amount") or 0,
        additional_cent_tax=inv.get("additional_cent_tax") or 0,
        meta_rows=meta_rows,
        numero=_esc(inv.get("numero", "")),
        inv_id=inv["id"],
        date_facture=_esc(inv.get("date_facture", "")),
        reference=_esc(inv.get("reference", "") or "-"),
        tiers_nom=_esc(inv.get("tiers_nom", "") or "N/A"),
        tiers_code=_esc(inv.get("tiers_code", "") or ""),
        statut=_esc(inv.get("statut", "")),
        statut_class=statut_class,
        locked=locked,
        can_certify=can_certify,
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
    if invoice_engine.is_invoice_locked(inv):
        return ("<h1>Facture immuable</h1><p>Cette facture est certifiee SFEC ou deja "
                "synchronisee avec Sage et ne peut plus etre modifiee.</p>"
                "<p><a href='/billing/invoice/{}'>Retour</a></p>".format(invoice_id)), 403
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
    is_recipient_taxable = inv.get("is_recipient_taxable", 1) if is_edit else 1
    is_recipient_taxable = 1 if is_recipient_taxable is None else int(bool(is_recipient_taxable))

    # Cibles possibles d'un avoir : ventes certifiées non déjà avoirées
    current_ref = reference_invoice_id if is_edit and reference_invoice_id else ""
    with sqlite_db.get_cursor() as cur:
        cur.execute("""
            SELECT i.id, i.numero, i.date_facture, i.montant_ttc, i.tiers_nom
            FROM invoices i
            WHERE i.type_doc = 'vente'
              AND i.sfec_statut IN ('CERTIFIE', 'DEJA_CERTIFIE')
              AND (NOT EXISTS (
                  SELECT 1 FROM invoices a
                  WHERE a.type_doc = 'avoir' AND a.reference_invoice_id = i.numero
              ) OR i.numero = ?)
            ORDER BY i.date_facture DESC LIMIT 200
        """, (current_ref,))
        certified_sales = sqlite_db.rows_to_list(cur.fetchall())

    lignes = inv.get("lignes", []) if is_edit else []
    contacts = pos_engine.list_contacts(limit=200)
    products = pos_engine.list_products(limit=200)
    tax_rates = pos_engine.list_tax_rates()

    return _page(render_template("billing/form.html",
        type_doc=type_doc, payment_method=payment_method, devise=devise,
        reference_invoice_id=reference_invoice_id, recipient_rccm=recipient_rccm,
        certified_sales=certified_sales,
        d_vente="selected" if type_doc == "vente" else "",
        d_avoir="selected" if type_doc == "avoir" else "",
        title=title, numero=_esc(numero), date_facture=_esc(date_facture), date_echeance=_esc(date_echeance),
        reference=_esc(reference), tiers_code=_esc(tiers_code), tiers_nom=_esc(tiers_nom),
        tiers_niu=_esc(tiers_niu), tiers_email=_esc(tiers_email), tiers_telephone=_esc(tiers_telephone),
        tiers_adresse=_esc(tiers_adresse), notes=_esc(notes),
        s_brouillon="selected" if statut == "brouillon" else "",
        s_valide="selected" if statut == "valide" else "",
        s_acompt="selected" if statut == "a_comptabiliser" else "",
        s_acomptise="selected" if statut == "a_comptabilise" else "",
        s_comptabilise="selected" if statut == "comptabilise" else "",
        p_bank="selected" if payment_method == "bank_transfer" else "",
        p_cash="selected" if payment_method == "cash" else "",
        p_card="selected" if payment_method == "card" else "",
        p_mobile="selected" if payment_method == "mobile_money" else "",
        p_cheque="selected" if payment_method == "cheque" else "",
        dev_xaf="selected" if devise == "XAF" else "",
        dev_usd="selected" if devise == "USD" else "",
        dev_eur="selected" if devise == "EUR" else "",
        tax_yes="selected" if is_recipient_taxable else "",
        tax_no="" if is_recipient_taxable else "selected",
        t_bus="selected" if tiers_type == "business" else "",
        t_ind="selected" if tiers_type == "individual" else "",
        t_gov="selected" if tiers_type == "government" else "",
        t_for="selected" if tiers_type == "foreign" else "",
        readonly="readonly" if is_edit else "",
        contacts=contacts, products=products, tax_rates=tax_rates,
        is_recipient_taxable=is_recipient_taxable,
        lignes=lignes, is_edit_js="true" if is_edit else "false",
        inv_id_js=inv["id"] if is_edit else "null",
        btn_text="Modifier" if is_edit else "Enregistrer"
    ) + "\n")


def _bounded_int_arg(name, default, low, high):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


@bp.route("/api/invoices", methods=["GET"])
@_login_required
def api_list_invoices():
    result = invoice_engine.list_invoices(
        type_doc=request.args.get("type_doc") or request.args.get("type"),
        statut=request.args.get("statut"),
        source=request.args.get("source"),
        search=request.args.get("search"),
        date_from=request.args.get("date_from"),
        date_to=request.args.get("date_to"),
        limit=_bounded_int_arg("limit", 200, 1, 500),
        offset=_bounded_int_arg("offset", 0, 0, 1000000),
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
                       or "certifi" in low or "ligne" in low
                       or "quantite" in low or "tva" in low) else 400
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
                       or "certifi" in low or "ligne" in low
                       or "quantite" in low or "tva" in low) else 400
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


@bp.route("/api/invoices/<path:numero>/link-parent", methods=["POST"])
@_login_required
def api_invoice_link_parent(numero):
    """Lien manuel avoir -> facture d'origine (filet de securite).

    La reference Sage (DO_Ref) est souvent du texte libre. Quand elle n'est
    pas resolvable automatiquement, l'operateur peut enregistrer le parent
    ici : la table `sfec_invoice_links` est consultee en priorite par la
    certification (proposition 05).
    """
    data = request.get_json(silent=True) or {}
    numero = str(numero or "").strip()
    parent = str(
        data.get("parent_numero")
        or data.get("reference_invoice_id")
        or ""
    ).strip()

    if not numero:
        return jsonify({"success": False, "error": "numero d'avoir requis"}), 422
    if not parent:
        return jsonify({"success": False, "error": "parent_numero requis"}), 422
    if parent == numero:
        return jsonify({"success": False,
                        "error": "Un avoir ne peut pas referencer lui-meme"}), 422

    parent_ttc = None
    with sqlite_db.get_cursor() as cur:
        cur.execute(
            "SELECT montant_ttc FROM invoices WHERE numero = ?", (parent,)
        )
        row = cur.fetchone()
    if row is not None:
        parent_ttc = row["montant_ttc"]

    if parent_ttc is None:
        # Le miroir ne contient pas encore le parent : on tente le cache Sage.
        try:
            from app.sync.engine import get_cache
            for inv in get_cache().get("sales_invoices", []):
                if str(inv.get("numero", "")) == parent:
                    parent_ttc = inv.get("montant_ttc", 0)
                    break
        except Exception:
            pass

    if parent_ttc is None:
        return jsonify({"success": False,
                        "error": "Facture d'origine '{}' introuvable".format(parent)}), 404
    try:
        if float(parent_ttc or 0) < 0:
            return jsonify({"success": False,
                            "error": "La facture d'origine '{}' est un avoir".format(parent)}), 422
    except (TypeError, ValueError):
        pass

    sqlite_db.set_sfec_invoice_link(numero, parent)

    # Un avoir deja marque A_SURVEILLER doit pouvoir etre rejoue apres le
    # lien manuel. On ne touche jamais a ERREUR ni a CERTIFIE.
    try:
        reset_sfec_monitor(numero)
    except Exception:
        pass
    try:
        with sqlite_db.get_cursor() as cur:
            cur.execute("""
                UPDATE invoices SET sfec_statut = ''
                WHERE numero = ? AND sfec_statut = 'A_SURVEILLER'
            """, (numero,))
    except Exception:
        pass

    who = (_current_identity() or {}).get("email", "")
    user_auth.audit(
        "avoir.lie_parent",
        "Avoir {} lie a la facture {}".format(numero, parent),
        email=who,
    )
    return jsonify({"success": True, "numero": numero, "parent_numero": parent})


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
        # Facture certifiee : 1er exemplaire "Original", les suivants "Duplicata"
        mention = ""
        if inv.get("sfec_num_certif"):
            from app.domain import print_copies
            mention = print_copies.next_mention(inv.get("numero", ""))
        pdf_bytes = pdf_generator.generate_invoice_pdf(inv, mention=mention)
        if not pdf_bytes:
            return jsonify({"error": "Erreur generation PDF"}), 500
        if mention:
            print_copies.register_print(inv.get("numero", ""), "pdf")

        from flask import send_file
        buf = __import__("io").BytesIO(pdf_bytes)
        numero = inv.get("numero", "facture").replace("/", "-")
        return send_file(buf, as_attachment=True,
                         download_name="{}.pdf".format(numero),
                         mimetype="application/pdf")
    except Exception as e:
        return jsonify({"error": str(e)}), 500



