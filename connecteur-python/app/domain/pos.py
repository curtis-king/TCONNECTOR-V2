import logging
from datetime import datetime
from app.storage.db import (
    get_cursor, generate_ticket_number, generate_invoice_number,
    recalc_ticket_totals, calc_line_ticket_totals,
    log_sync, row_to_dict, rows_to_list
)
from app.config.manager import get_config

logger = logging.getLogger("t-connector.pos")


def stock_control_enabled():
    try:
        return bool(get_config().get("pos", {}).get("stock_control", True))
    except Exception:
        return True


def create_ticket(data):
    numero = data.get("numero") or generate_ticket_number()
    date_ticket = data.get("date_ticket") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    contact_id = data.get("contact_id")
    tiers_nom = data.get("tiers_nom", "Client comptoir")
    mode_paiement = data.get("mode_paiement", "especes")
    montant_recu = float(data.get("montant_recu", 0))
    vendeur_id = data.get("vendeur_id")
    notes = data.get("notes", "")
    caissier = data.get("caissier", "") or ""
    remise_globale_pct = float(data.get("remise_globale_pct", 0) or 0)
    remise_globale_montant = float(data.get("remise_globale_montant", 0) or 0)

    lignes = data.get("lignes", [])
    if not lignes:
        return {"success": False, "error": "Aucune ligne dans le ticket"}

    stock_check = check_stock_available(lignes)
    if not stock_check["ok"]:
        manque = stock_check["manque"]
        detail = ", ".join(
            "{} (stock {}{}, demande {})".format(m["designation"], m.get("stock", 0), "", m["qte"])
            for m in manque
        )
        return {"success": False, "error": "Stock insuffisant: " + detail}

    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO pos_tickets (
                numero, date_ticket, contact_id, tiers_nom,
                mode_paiement, vendeur_id, notes, caissier,
                remise_globale_pct, remise_globale_montant
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (numero, date_ticket, contact_id, tiers_nom, mode_paiement,
              vendeur_id, notes, caissier, remise_globale_pct, remise_globale_montant))
        ticket_id = cur.lastrowid

    _save_ticket_lines(ticket_id, lignes)
    totals = recalc_ticket_totals(ticket_id)
    montant_ttc = totals["montant_ttc"]

    monnaie_rendue = max(montant_recu - montant_ttc, 0)
    with get_cursor() as cur:
        cur.execute("""
            UPDATE pos_tickets SET montant_recu = ?, monnaie_rendue = ? WHERE id = ?
        """, (montant_recu, monnaie_rendue, ticket_id))

    _decrement_stock(lignes)

    invoice_result = _create_invoice_for_ticket(ticket_id, data, totals)

    log_sync("pos_tickets", ticket_id, numero, "create", "pos")
    logger.info("Ticket cree: %s (id=%d)", numero, ticket_id)

    result = {"success": True, "id": ticket_id, "numero": numero,
              "montant_ttc": montant_ttc, "monnaie_rendue": monnaie_rendue}
    result.update(invoice_result)
    return result


def _create_invoice_for_ticket(ticket_id, data, totals):
    """Cree une facture associee au ticket POS et la pousse vers Sage
    (comportement du module de saisie de caisse Sage)."""
    try:
        numero = generate_invoice_number()
        date_ticket = data.get("date_ticket") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        date_facture = date_ticket[:10]
        tiers_nom = data.get("tiers_nom", "Client comptoir")
        mode_paiement = data.get("mode_paiement", "especes")

        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO invoices (
                    numero, date_facture, reference, contact_id,
                    tiers_code, tiers_nom, tiers_type, montant_ht, montant_tva,
                    montant_ttc, montant_restant, statut, valide,
                    type_doc, source, vendeur_id, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'a_comptabiliser', 1, 'vente', 'pos', ?, ?)
            """, (
                numero, date_facture, "PAIEMENT " + mode_paiement,
                data.get("contact_id"), data.get("tiers_code", ""), tiers_nom,
                data.get("tiers_type", "particulier"),
                totals.get("montant_ht", 0), totals.get("montant_tva", 0),
                totals.get("montant_ttc", 0), 0,
                data.get("vendeur_id"), ""
            ))
            invoice_id = cur.lastrowid

        ligne_infos = []
        with get_cursor() as cur:
            for i, ligne in enumerate(data.get("lignes", [])):
                quantite = float(ligne.get("quantite", 1))
                prix_unitaire = float(ligne.get("prix_unitaire", 0))
                taux_tva = float(ligne.get("taux_tva", 18))
                net_ht, montant_tva, montant_ttc, _ = calc_line_ticket_totals(
                    quantite, prix_unitaire,
                    float(ligne.get("remise_pct", 0) or 0),
                    float(ligne.get("remise_montant", 0) or 0),
                    taux_tva
                )
                cur.execute("""
                    INSERT INTO invoice_lines (
                        invoice_id, numero_ligne, designation, quantite, prix_unitaire,
                        montant_ht, taux_tva, montant_tva, montant_ttc,
                        code_article, code_compte, famille, product_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    invoice_id, i + 1, ligne.get("designation", ""), quantite,
                    prix_unitaire, net_ht, taux_tva, montant_tva, montant_ttc,
                    ligne.get("code_article", ""), ligne.get("code_compte", ""),
                    ligne.get("famille", ""), ligne.get("product_id")
                ))
                ligne_infos.append({
                    "numero_ligne": i + 1, "designation": ligne.get("designation", ""),
                    "quantite": quantite, "prix_unitaire": prix_unitaire,
                    "montant_ht": net_ht, "taux_tva": taux_tva,
                    "montant_tva": montant_tva, "montant_ttc": montant_ttc,
                    "code_article": ligne.get("code_article", ""),
                    "code_compte": ligne.get("code_compte", ""),
                    "famille": ligne.get("famille", ""),
                    "product_id": ligne.get("product_id")
                })

        with get_cursor() as cur:
            cur.execute("UPDATE pos_tickets SET invoice_id = ? WHERE id = ?", (invoice_id, ticket_id))

        log_sync("invoices", invoice_id, numero, "create_from_pos", "pos")

        sfec_ok = False
        sfec_error = ""
        sfec_num = ""
        sfec_qr = ""
        sfec_date = ""
        try:
            from app.config.manager import get_config
            from app.sync.connectivity import is_online
            from app.integration.sfec.endpoints import certify_sqlite_invoice
            sfec_cfg = get_config().get("sfec", {})
            if sfec_cfg.get("enabled") and sfec_cfg.get("api_key") and is_online():
                cert_inv = {
                    "id": invoice_id, "numero": numero,
                    "reference": "PAIEMENT " + mode_paiement,
                    "date_facture": date_facture, "date_echeance": date_facture,
                    "montant_ht": totals.get("montant_ht", 0),
                    "montant_tva": totals.get("montant_tva", 0),
                    "montant_ttc": totals.get("montant_ttc", 0),
                    "tiers_nom": tiers_nom,
                    "tiers_type": data.get("tiers_type", "particulier"),
                    "tiers_niu": data.get("tiers_niu", ""),
                    "tiers_telephone": data.get("tiers_telephone", ""),
                    "tiers_email": data.get("tiers_email", ""),
                    "tiers_adresse": data.get("tiers_adresse", ""),
                    "lignes": ligne_infos,
                }
                cert_res = certify_sqlite_invoice(cert_inv)
                sfec_num = cert_res.get("certification_number", "") or ""
                sfec_qr = cert_res.get("qr_code", "") or ""
                sfec_date = cert_res.get("certification_date", "") or ""
                sfec_ok = bool(sfec_num)
                if not sfec_ok:
                    sfec_error = "Certification SFEC pas finalisee (id {})".format(cert_res.get("identifier", ""))
                    with get_cursor() as cur:
                        cur.execute("UPDATE invoices SET sfec_statut = 'EN_COURS' WHERE id = ?", (invoice_id,))
            else:
                sfec_error = "SFEC desactivee, cle manquante ou hors ligne"
                with get_cursor() as cur:
                    cur.execute("""
                        UPDATE invoices SET sfec_statut = 'EN_COURS',
                            notes = COALESCE(NULLIF(notes, ''), '') || 'En attente de certification SFEC. '
                        WHERE id = ?
                    """, (invoice_id,))
        except Exception as e:
            sfec_error = str(e)[:200]
            try:
                with get_cursor() as cur:
                    cur.execute("""
                        UPDATE invoices SET sfec_statut = 'ERREUR',
                            notes = COALESCE(NULLIF(notes, ''), '') || ?
                        WHERE id = ?
                    """, ("Erreur SFEC: {}. ".format(str(e)[:120]), invoice_id))
            except Exception:
                pass

        sage_ok = False
        sage_error = ""
        if sfec_ok:
            try:
                from app.integration.sage.writer import write_invoice_to_sage
                inv = {
                    "id": invoice_id, "numero": numero,
                    "date_facture": date_facture,
                    "tiers_code": data.get("tiers_code", ""),
                    "reference": "PAIEMENT " + mode_paiement,
                    "montant_ht": totals.get("montant_ht", 0),
                    "montant_tva": totals.get("montant_tva", 0),
                    "montant_ttc": totals.get("montant_ttc", 0),
                    "lignes": ligne_infos,
                }
                sage_res = write_invoice_to_sage(inv)
                sage_ok = sage_res.get("success", False)
                if not sage_ok:
                    sage_error = sage_res.get("error", "")
            except Exception as e:
                sage_error = str(e)[:200]
        else:
            sage_error = sfec_error or "Certification SFEC requise avant ecriture Sage"

        logger.info("Invoice POS creee: %s (ticket=%s, sfec_ok=%s, sage_ok=%s)",
                    numero, ticket_id, sfec_ok, sage_ok)
        return {"invoice_id": invoice_id, "invoice_numero": numero,
                "sfec_ok": sfec_ok, "sfec_num_certif": sfec_num,
                "sfec_qr_code": sfec_qr, "sfec_date_certif": sfec_date,
                "sfec_error": sfec_error,
                "sage_ok": sage_ok, "sage_error": sage_error}
    except Exception as e:
        logger.error("Creation invoice POS echouee pour ticket %s: %s", ticket_id, e)
        return {"invoice_id": None, "invoice_numero": None,
                "sage_ok": False, "sage_error": str(e)[:200]}


def get_ticket(ticket_id):
    with get_cursor() as cur:
        cur.execute("""
            SELECT t.*, v.nom as vendeur_nom, v.prenom as vendeur_prenom
            FROM pos_tickets t
            LEFT JOIN vendeurs v ON t.vendeur_id = v.id
            WHERE t.id = ?
        """, (ticket_id,))
        row = cur.fetchone()
        if not row:
            return None
        ticket = row_to_dict(row)
        cur.execute("SELECT * FROM pos_ticket_lines WHERE ticket_id = ? ORDER BY numero_ligne", (ticket_id,))
        ticket["lignes"] = rows_to_list(cur.fetchall())
        cur.execute("""
            SELECT taux_tva, COALESCE(SUM(montant_ht), 0) as ht, COALESCE(SUM(montant_tva), 0) as tva
            FROM pos_ticket_lines WHERE ticket_id = ?
            GROUP BY taux_tva ORDER BY taux_tva
        """, (ticket_id,))
        ticket["tax_breakdown"] = rows_to_list(cur.fetchall())
        return ticket


def get_ticket_by_numero(numero):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM pos_tickets WHERE numero = ?", (numero,))
        row = cur.fetchone()
        if not row:
            return None
        ticket = row_to_dict(row)
        cur.execute("SELECT * FROM pos_ticket_lines WHERE ticket_id = ? ORDER BY numero_ligne", (ticket["id"],))
        ticket["lignes"] = rows_to_list(cur.fetchall())
        cur.execute("""
            SELECT taux_tva, COALESCE(SUM(montant_ht), 0) as ht, COALESCE(SUM(montant_tva), 0) as tva
            FROM pos_ticket_lines WHERE ticket_id = ?
            GROUP BY taux_tva ORDER BY taux_tva
        """, (ticket["id"],))
        ticket["tax_breakdown"] = rows_to_list(cur.fetchall())
        return ticket


def list_tickets(date_from=None, date_to=None, vendeur_id=None, search=None,
                 limit=100, offset=0, sort_by="date_ticket", sort_dir="DESC"):
    where_clauses = []
    params = []

    if date_from:
        where_clauses.append("t.date_ticket >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("t.date_ticket <= ?")
        params.append(date_to)
    if vendeur_id:
        where_clauses.append("t.vendeur_id = ?")
        params.append(vendeur_id)
    if search:
        where_clauses.append("(t.numero LIKE ? OR t.tiers_nom LIKE ?)")
        s = "%{}%".format(search)
        params.extend([s, s])

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    allowed_sorts = {"date_ticket", "montant_ttc", "numero", "created_at"}
    if sort_by not in allowed_sorts:
        sort_by = "date_ticket"
    sort_dir = "ASC" if sort_dir.upper() == "ASC" else "DESC"

    params.extend([limit, offset])

    with get_cursor() as cur:
        cur.execute("""
            SELECT t.*, v.nom as vendeur_nom, v.prenom as vendeur_prenom,
                   i.numero as invoice_numero, i.sfec_statut as sfec_statut,
                   i.sfec_num_certif as sfec_num_certif
            FROM pos_tickets t
            LEFT JOIN vendeurs v ON t.vendeur_id = v.id
            LEFT JOIN invoices i ON t.invoice_id = i.id
            WHERE {where}
            ORDER BY t.{sort} {dir}
            LIMIT ? OFFSET ?
        """.format(where=where_sql, sort=sort_by, dir=sort_dir), params)
        tickets = rows_to_list(cur.fetchall())

        count_params = params[:-2]
        cur.execute("""
            SELECT COUNT(*) as cnt
            FROM pos_tickets t
            LEFT JOIN vendeurs v ON t.vendeur_id = v.id
            WHERE {where}
        """.format(where=where_sql), count_params)
        total = cur.fetchone()["cnt"]

    return {"tickets": tickets, "total": total}


def count_tickets():
    with get_cursor() as cur:
        stats = {}
        cur.execute("SELECT COUNT(*) as cnt FROM pos_tickets")
        stats["total"] = cur.fetchone()["cnt"]
        cur.execute("SELECT COALESCE(SUM(montant_ttc), 0) as total FROM pos_tickets WHERE statut = 'valide'")
        stats["ca_total"] = cur.fetchone()["total"]
        cur.execute("""
            SELECT COALESCE(SUM(montant_ttc), 0) as total FROM pos_tickets
            WHERE statut = 'valide' AND date(date_ticket) = date('now')
        """)
        stats["ca_jour"] = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as cnt FROM pos_tickets WHERE statut = 'valide' AND date(date_ticket) = date('now')")
        stats["tickets_jour"] = cur.fetchone()["cnt"]
        cur.execute("""
            SELECT v.nom, v.prenom, COUNT(*) as nb_ventes, COALESCE(SUM(t.montant_ttc), 0) as ca
            FROM pos_tickets t
            JOIN vendeurs v ON t.vendeur_id = v.id
            WHERE t.statut = 'valide' AND date(t.date_ticket) = date('now')
            GROUP BY t.vendeur_id
        """)
        stats["par_vendeur"] = rows_to_list(cur.fetchall())
    return stats


def search_products(query="", limit=20):
    with get_cursor() as cur:
        if query:
            q = "%{}%".format(query)
            cur.execute("""
                SELECT * FROM products
                WHERE est_actif = 1 AND (ref LIKE ? OR barcode LIKE ? OR designation LIKE ? OR famille LIKE ?)
                ORDER BY designation LIMIT ?
            """, (q, q, q, q, limit))
        else:
            cur.execute("""
                SELECT * FROM products WHERE est_actif = 1 ORDER BY designation LIMIT ?
            """, (limit,))
        return rows_to_list(cur.fetchall())


def find_product_by_barcode(barcode):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM products WHERE barcode = ? AND est_actif = 1", (barcode,))
        row = cur.fetchone()
        return row_to_dict(row) if row else None


def top_selling_products(limit=10):
    """Retourne les articles les plus vendus (via le ticket POS), complete
    par les autres articles actifs si le nombre est insuffisant."""
    with get_cursor() as cur:
        cur.execute("""
            SELECT p.*, COALESCE(SUM(pl.quantite), 0) AS total_vendu
            FROM products p
            LEFT JOIN pos_ticket_lines pl ON pl.product_id = p.id
            WHERE p.est_actif = 1
            GROUP BY p.id
            ORDER BY total_vendu DESC, p.designation ASC
            LIMIT ?
        """, (limit,))
        products = rows_to_list(cur.fetchall())

    remaining = limit - len(products)
    if remaining > 0:
        seen = [p["id"] for p in products]
        with get_cursor() as cur:
            if seen:
                placeholders = ",".join("?" for _ in seen)
                cur.execute("""
                    SELECT *, 0 AS total_vendu FROM products
                    WHERE est_actif = 1 AND id NOT IN ({})
                    ORDER BY designation LIMIT ?
                """.format(placeholders), seen + [remaining])
            else:
                cur.execute("""
                    SELECT *, 0 AS total_vendu FROM products
                    WHERE est_actif = 1
                    ORDER BY designation LIMIT ?
                """, (remaining,))
            products.extend(rows_to_list(cur.fetchall()))
    return products


def check_stock_available(lignes):
    if not stock_control_enabled():
        return {"ok": True, "manque": []}
    manque = []
    for ligne in lignes:
        product_id = ligne.get("product_id")
        if not product_id:
            continue
        qte = float(ligne.get("quantite", 1))
        with get_cursor() as cur:
            cur.execute("SELECT stock_reel as stock FROM products WHERE id = ?", (product_id,))
            row = cur.fetchone()
        stock = row["stock"] if row else 0
        if qte > stock:
            manque.append({
                "designation": ligne.get("designation", ""),
                "stock": stock, "qte": qte
            })
    return {"ok": len(manque) == 0, "manque": manque}


def _decrement_stock(lignes):
    for ligne in lignes:
        product_id = ligne.get("product_id")
        if not product_id:
            continue
        qte = float(ligne.get("quantite", 1))
        with get_cursor() as cur:
            cur.execute(
                "UPDATE products SET stock_reel = MAX(stock_reel - ?, 0), updated_at = datetime('now') WHERE id = ?",
                (qte, product_id)
            )


def get_ticket_tax_breakdown(ticket_id):
    with get_cursor() as cur:
        cur.execute("""
            SELECT taux_tva, COALESCE(SUM(montant_ht), 0) as ht, COALESCE(SUM(montant_tva), 0) as tva
            FROM pos_ticket_lines WHERE ticket_id = ?
            GROUP BY taux_tva ORDER BY taux_tva
        """, (ticket_id,))
        return rows_to_list(cur.fetchall())


def _save_ticket_lines(ticket_id, lignes):
    if not lignes:
        return
    with get_cursor() as cur:
        for i, ligne in enumerate(lignes):
            numero_ligne = ligne.get("numero_ligne", i + 1)
            designation = ligne.get("designation", "")
            quantite = float(ligne.get("quantite", 1))
            prix_unitaire = float(ligne.get("prix_unitaire", 0))
            taux_tva = float(ligne.get("taux_tva", 18))
            code_article = ligne.get("code_article", "")
            barcode = ligne.get("barcode", "")
            product_id = ligne.get("product_id")
            remise_pct = float(ligne.get("remise_pct", 0) or 0)
            remise_montant = float(ligne.get("remise_montant", 0) or 0)

            net_ht, montant_tva, montant_ttc, remise_total = calc_line_ticket_totals(
                quantite, prix_unitaire, remise_pct, remise_montant, taux_tva
            )

            cur.execute("""
                INSERT INTO pos_ticket_lines (
                    ticket_id, numero_ligne, designation, quantite, prix_unitaire,
                    montant_ht, taux_tva, montant_tva, montant_ttc,
                    remise_pct, remise_montant,
                    code_article, barcode, product_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ticket_id, numero_ligne, designation, quantite, prix_unitaire,
                net_ht, taux_tva, montant_tva, montant_ttc,
                remise_pct, remise_total,
                code_article, barcode, product_id
            ))


def create_product(data):
    ref = data.get("ref", "")
    designation = data.get("designation", "")
    if not ref or not designation:
        return {"success": False, "error": "Reference et designation requis"}

    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO products (ref, barcode, designation, famille, nature, prix_vente, prix_achat, tva_code, unite, stock_reel)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            ref, data.get("barcode", ""),
            designation,
            data.get("famille", ""),
            data.get("nature", 0),
            float(data.get("prix_vente", 0)),
            float(data.get("prix_achat", 0)),
            data.get("tva_code", "18"),
            data.get("unite", "U"),
            float(data.get("stock_reel", 0))
        ))
        product_id = cur.lastrowid

    logger.info("Produit cree: %s - %s (id=%d)", ref, designation, product_id)
    return {"success": True, "id": product_id}


def update_product(product_id, data):
    with get_cursor() as cur:
        cur.execute("SELECT id FROM products WHERE id = ?", (product_id,))
        if not cur.fetchone():
            return {"success": False, "error": "Produit non trouve"}

        fields = []
        params = []
        for key in ("ref", "barcode", "designation", "famille", "nature", "prix_vente",
                     "prix_achat", "tva_code", "unite", "stock_reel", "est_actif"):
            if key in data:
                fields.append("{} = ?".format(key))
                params.append(data[key])
        if fields:
            fields.append("updated_at = datetime('now')")
            params.append(product_id)
            cur.execute("UPDATE products SET {} WHERE id = ?".format(", ".join(fields)), params)
    return {"success": True, "id": product_id}


def create_contact(data):
    code = data.get("code", "")
    nom = data.get("nom", "")
    if not code or not nom:
        return {"success": False, "error": "Code et nom requis"}

    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO contacts (code, nom, type, email, telephone, niu, adresse, ville, pays)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            code, nom,
            data.get("type", "client"),
            data.get("email", ""),
            data.get("telephone", ""),
            data.get("niu", ""),
            data.get("adresse", ""),
            data.get("ville", ""),
            data.get("pays", "CG")
        ))
        contact_id = cur.lastrowid

    logger.info("Contact cree: %s - %s (id=%d)", code, nom, contact_id)
    return {"success": True, "id": contact_id}


def list_contacts(type_filter=None, search=None, limit=200):
    where_clauses = []
    params = []

    if type_filter:
        where_clauses.append("type = ?")
        params.append(type_filter)
    if search:
        where_clauses.append("(code LIKE ? OR nom LIKE ? OR email LIKE ?)")
        s = "%{}%".format(search)
        params.extend([s, s, s])

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
    params.append(limit)

    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM contacts WHERE {} ORDER BY nom LIMIT ?".format(where_sql),
            params
        )
        return rows_to_list(cur.fetchall())


def list_products(type_filter=None, search=None, limit=200):
    where_clauses = ["est_actif = 1"]
    params = []

    if type_filter:
        where_clauses.append("famille = ?")
        params.append(type_filter)
    if search:
        where_clauses.append("(ref LIKE ? OR barcode LIKE ? OR designation LIKE ?)")
        s = "%{}%".format(search)
        params.extend([s, s, s])

    where_sql = " AND ".join(where_clauses)
    params.append(limit)

    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM products WHERE {} ORDER BY designation LIMIT ?".format(where_sql),
            params
        )
        return rows_to_list(cur.fetchall())


def list_tax_rates():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM tax_rates WHERE est_actif = 1 ORDER BY taux")
        return rows_to_list(cur.fetchall())


# ══════════════════════════════════════════════════════════════
# VENDEURS
# ══════════════════════════════════════════════════════════════

def create_vendeur(data):
    code = data.get("code", "")
    nom = data.get("nom", "")
    if not code or not nom:
        return {"success": False, "error": "Code et nom requis"}

    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO vendeurs (code, nom, prenom, email, telephone, role, pin)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            code, nom,
            data.get("prenom", ""),
            data.get("email", ""),
            data.get("telephone", ""),
            data.get("role", "vendeur"),
            data.get("pin", "")
        ))
        vendeur_id = cur.lastrowid

    logger.info("Vendeur cree: %s %s (id=%d)", data.get("prenom", ""), nom, vendeur_id)
    return {"success": True, "id": vendeur_id}


def update_vendeur(vendeur_id, data):
    with get_cursor() as cur:
        cur.execute("SELECT id FROM vendeurs WHERE id = ?", (vendeur_id,))
        if not cur.fetchone():
            return {"success": False, "error": "Vendeur non trouve"}

        fields = []
        params = []
        for key in ("nom", "prenom", "email", "telephone", "role", "pin", "est_actif"):
            if key in data:
                fields.append("{} = ?".format(key))
                params.append(data[key])
        if fields:
            fields.append("updated_at = datetime('now')")
            params.append(vendeur_id)
            cur.execute("UPDATE vendeurs SET {} WHERE id = ?".format(", ".join(fields)), params)
    return {"success": True, "id": vendeur_id}


def get_vendeur(vendeur_id):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM vendeurs WHERE id = ?", (vendeur_id,))
        row = cur.fetchone()
        return row_to_dict(row) if row else None


def list_vendeurs(active_only=True, limit=200):
    with get_cursor() as cur:
        where = "WHERE est_actif = 1" if active_only else ""
        cur.execute("SELECT * FROM vendeurs {} ORDER BY nom LIMIT ?".format(where), (limit,))
        return rows_to_list(cur.fetchall())


def get_vendeur_stats(vendeur_id=None, date_from=None, date_to=None):
    with get_cursor() as cur:
        where_clauses = ["t.statut = 'valide'"]
        params = []

        if vendeur_id:
            where_clauses.append("t.vendeur_id = ?")
            params.append(vendeur_id)
        if date_from:
            where_clauses.append("t.date_ticket >= ?")
            params.append(date_from)
        if date_to:
            where_clauses.append("t.date_ticket <= ?")
            params.append(date_to)

        where_sql = " AND ".join(where_clauses)

        cur.execute("""
            SELECT v.id, v.nom, v.prenom, v.role,
                   COUNT(*) as nb_ventes,
                   COALESCE(SUM(t.montant_ttc), 0) as ca_total,
                   COALESCE(AVG(t.montant_ttc), 0) as panier_moyen
            FROM pos_tickets t
            JOIN vendeurs v ON t.vendeur_id = v.id
            WHERE {where}
            GROUP BY t.vendeur_id
            ORDER BY ca_total DESC
        """.format(where=where_sql), params)
        return rows_to_list(cur.fetchall())
