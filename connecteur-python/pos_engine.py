import logging
from datetime import datetime
from sqlite_db import (
    get_cursor, generate_ticket_number, recalc_ticket_totals,
    log_sync, row_to_dict, rows_to_list
)

logger = logging.getLogger("t-connector.pos")


def create_ticket(data):
    numero = data.get("numero") or generate_ticket_number()
    date_ticket = data.get("date_ticket") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    contact_id = data.get("contact_id")
    tiers_nom = data.get("tiers_nom", "Client comptoir")
    mode_paiement = data.get("mode_paiement", "especes")
    montant_recu = float(data.get("montant_recu", 0))
    vendeur_id = data.get("vendeur_id")
    notes = data.get("notes", "")

    lignes = data.get("lignes", [])
    if not lignes:
        return {"success": False, "error": "Aucune ligne dans le ticket"}

    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO pos_tickets (
                numero, date_ticket, contact_id, tiers_nom,
                mode_paiement, vendeur_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (numero, date_ticket, contact_id, tiers_nom, mode_paiement, vendeur_id, notes))
        ticket_id = cur.lastrowid

    _save_ticket_lines(ticket_id, lignes)
    recalc_ticket_totals(ticket_id)

    with get_cursor() as cur:
        cur.execute("SELECT montant_ttc FROM pos_tickets WHERE id = ?", (ticket_id,))
        row = cur.fetchone()
        montant_ttc = row["montant_ttc"] if row else 0

    monnaie_rendue = max(montant_recu - montant_ttc, 0)
    with get_cursor() as cur:
        cur.execute("""
            UPDATE pos_tickets SET montant_recu = ?, monnaie_rendue = ? WHERE id = ?
        """, (montant_recu, monnaie_rendue, ticket_id))

    log_sync("pos_tickets", ticket_id, numero, "create", "pos")
    logger.info("Ticket cree: %s (id=%d)", numero, ticket_id)

    return {"success": True, "id": ticket_id, "numero": numero,
            "montant_ttc": montant_ttc, "monnaie_rendue": monnaie_rendue}


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
            SELECT t.*, v.nom as vendeur_nom, v.prenom as vendeur_prenom
            FROM pos_tickets t
            LEFT JOIN vendeurs v ON t.vendeur_id = v.id
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

            subtotal = round(quantite * prix_unitaire, 2)
            montant_tva = round(subtotal * taux_tva / 100, 2)
            montant_ttc = round(subtotal + montant_tva, 2)

            cur.execute("""
                INSERT INTO pos_ticket_lines (
                    ticket_id, numero_ligne, designation, quantite, prix_unitaire,
                    montant_ht, taux_tva, montant_tva, montant_ttc,
                    code_article, barcode, product_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                ticket_id, numero_ligne, designation, quantite, prix_unitaire,
                subtotal, taux_tva, montant_tva, montant_ttc,
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
