import logging
from datetime import datetime
from sqlite_db import (
    get_cursor, generate_invoice_number, calc_line_totals,
    recalc_invoice_totals, log_sync, row_to_dict, rows_to_list
)

logger = logging.getLogger("t-connector.invoice")


def create_invoice(data):
    numero = data.get("numero") or generate_invoice_number()
    date_facture = data.get("date_facture") or datetime.utcnow().strftime("%Y-%m-%d")
    date_echeance = data.get("date_echeance", "")
    reference = data.get("reference", "")
    contact_id = data.get("contact_id")
    tiers_code = data.get("tiers_code", "")
    tiers_nom = data.get("tiers_nom", "")
    tiers_niu = data.get("tiers_niu", "")
    tiers_email = data.get("tiers_email", "")
    tiers_telephone = data.get("tiers_telephone", "")
    tiers_adresse = data.get("tiers_adresse", "")
    tiers_type = data.get("tiers_type", "business")
    type_doc = data.get("type_doc", "vente")
    source = data.get("source", "web")
    notes = data.get("notes", "")
    statut = data.get("statut", "brouillon")

    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO invoices (
                numero, date_facture, date_echeance, reference,
                contact_id, tiers_code, tiers_nom, tiers_niu,
                tiers_email, tiers_telephone, tiers_adresse, tiers_type,
                statut, type_doc, source, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            numero, date_facture, date_echeance, reference,
            contact_id, tiers_code, tiers_nom, tiers_niu,
            tiers_email, tiers_telephone, tiers_adresse, tiers_type,
            statut, type_doc, source, notes
        ))
        invoice_id = cur.lastrowid

    lignes = data.get("lignes", [])
    _save_lines(invoice_id, lignes)
    recalc_invoice_totals(invoice_id)

    log_sync("invoices", invoice_id, numero, "create", source)
    logger.info("Facture creee: %s (id=%d, source=%s)", numero, invoice_id, source)

    return {"id": invoice_id, "numero": numero}


def update_invoice(invoice_id, data):
    invoice = get_invoice(invoice_id)
    if not invoice:
        return None

    fields = []
    values = []
    updatable = [
        "date_facture", "date_echeance", "reference", "contact_id",
        "tiers_code", "tiers_nom", "tiers_niu", "tiers_email",
        "tiers_telephone", "tiers_adresse", "tiers_type",
        "statut", "valide", "notes", "sfec_statut", "sfec_num_certif",
        "sfec_signature", "sfec_qr_code", "sfec_date_certif", "sfec_id"
    ]

    for field in updatable:
        if field in data:
            fields.append("{} = ?".format(field))
            values.append(data[field])

    if not fields and "lignes" not in data:
        return invoice

    fields.append("updated_at = datetime('now')")
    values.append(invoice_id)

    with get_cursor() as cur:
        if fields:
            sql = "UPDATE invoices SET {} WHERE id = ?".format(", ".join(fields))
            cur.execute(sql, values)

    if "lignes" in data:
        _delete_lines(invoice_id)
        _save_lines(invoice_id, data["lignes"])
        recalc_invoice_totals(invoice_id)

    log_sync("invoices", invoice_id, invoice["numero"], "update", "web")
    logger.info("Facture mise a jour: %s (id=%d)", invoice["numero"], invoice_id)

    return get_invoice(invoice_id)


def delete_invoice(invoice_id):
    invoice = get_invoice(invoice_id)
    if not invoice:
        return False

    with get_cursor() as cur:
        cur.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))
        cur.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))

    log_sync("invoices", invoice_id, invoice["numero"], "delete", "web")
    logger.info("Facture supprimee: %s (id=%d)", invoice["numero"], invoice_id)
    return True


def get_invoice(invoice_id):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
        row = cur.fetchone()
        if not row:
            return None
        inv = row_to_dict(row)
        cur.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY numero_ligne", (invoice_id,))
        inv["lignes"] = rows_to_list(cur.fetchall())
        return inv


def get_invoice_by_numero(numero):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM invoices WHERE numero = ?", (numero,))
        row = cur.fetchone()
        if not row:
            return None
        inv = row_to_dict(row)
        cur.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY numero_ligne", (inv["id"],))
        inv["lignes"] = rows_to_list(cur.fetchall())
        return inv


def list_invoices(type_doc=None, statut=None, source=None, search=None,
                  date_from=None, date_to=None, limit=200, offset=0,
                  sort_by="date_facture", sort_dir="DESC"):
    where_clauses = []
    params = []

    if type_doc:
        where_clauses.append("type_doc = ?")
        params.append(type_doc)
    if statut:
        where_clauses.append("statut = ?")
        params.append(statut)
    if source:
        where_clauses.append("source = ?")
        params.append(source)
    if search:
        where_clauses.append("(numero LIKE ? OR tiers_nom LIKE ? OR reference LIKE ?)")
        s = "%{}%".format(search)
        params.extend([s, s, s])
    if date_from:
        where_clauses.append("date_facture >= ?")
        params.append(date_from)
    if date_to:
        where_clauses.append("date_facture <= ?")
        params.append(date_to)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    allowed_sorts = {"date_facture", "statut", "source", "numero", "montant_ttc", "id", "tiers_nom"}
    if sort_by not in allowed_sorts:
        sort_by = "date_facture"
    sort_dir = "ASC" if (sort_dir or "DESC").upper() == "ASC" else "DESC"

    params.extend([limit, offset])

    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM invoices WHERE {} ORDER BY {} {} LIMIT ? OFFSET ?".format(where_sql, sort_by, sort_dir),
            params
        )
        invoices = rows_to_list(cur.fetchall())

        cur.execute(
            "SELECT COUNT(*) as cnt FROM invoices WHERE {}".format(where_sql),
            params[:-2]
        )
        total = cur.fetchone()["cnt"]

    return {"invoices": invoices, "total": total, "limit": limit, "offset": offset}


def list_invoices_for_sfec():
    with get_cursor() as cur:
        cur.execute("""
            SELECT * FROM invoices
            WHERE statut IN ('valide', 'a_comptabiliser')
              AND sfec_statut NOT IN ('CERTIFIE', 'DEJA_CERTIFIE', 'EN_COURS')
              AND sfec_statut != 'ERREUR'
            ORDER BY date_facture DESC
        """)
        return rows_to_list(cur.fetchall())


def count_invoices():
    with get_cursor() as cur:
        stats = {}
        cur.execute("SELECT COUNT(*) as cnt FROM invoices")
        stats["total"] = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE source = 'web'")
        stats["from_web"] = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE source = 'sage'")
        stats["from_sage"] = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE statut = 'brouillon'")
        stats["brouillons"] = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE statut = 'valide'")
        stats["validees"] = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE sfec_statut = 'CERTIFIE'")
        stats["certifiees"] = cur.fetchone()["cnt"]
        cur.execute("SELECT COUNT(*) as cnt FROM invoices WHERE sfec_statut = 'EN_COURS'")
        stats["en_cours_certif"] = cur.fetchone()["cnt"]
        cur.execute("SELECT COALESCE(SUM(montant_ttc), 0) as total FROM invoices WHERE statut != 'brouillon'")
        stats["ca_total"] = cur.fetchone()["total"]
    return stats


def _save_lines(invoice_id, lignes):
    if not lignes:
        return
    with get_cursor() as cur:
        for i, ligne in enumerate(lignes):
            numero_ligne = ligne.get("numero_ligne", i + 1)
            designation = ligne.get("designation", "")
            quantite = float(ligne.get("quantite", 1))
            prix_unitaire = float(ligne.get("prix_unitaire", 0))
            remise_pct = float(ligne.get("remise_pct", 0))
            taux_tva = float(ligne.get("taux_tva", 18))
            code_article = ligne.get("code_article", "")
            code_compte = ligne.get("code_compte", "")
            famille = ligne.get("famille", "")
            unite = ligne.get("unite", "U")
            product_id = ligne.get("product_id")

            montant_ht, montant_tva, montant_ttc = calc_line_totals(
                quantite, prix_unitaire, remise_pct, taux_tva
            )
            remise_montant = round(quantite * prix_unitaire * remise_pct / 100, 2) if remise_pct else 0.0

            cur.execute("""
                INSERT INTO invoice_lines (
                    invoice_id, numero_ligne, designation, quantite, prix_unitaire,
                    remise_pct, remise_montant, montant_ht, taux_tva,
                    montant_tva, montant_ttc, code_article, code_compte,
                    famille, unite, product_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                invoice_id, numero_ligne, designation, quantite, prix_unitaire,
                remise_pct, remise_montant, montant_ht, taux_tva,
                montant_tva, montant_ttc, code_article, code_compte,
                famille, unite, product_id
            ))


def _delete_lines(invoice_id):
    with get_cursor() as cur:
        cur.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))


def update_invoice_sfec(invoice_id, sfec_data):
    fields = {
        "sfec_statut": sfec_data.get("sfec_statut", ""),
        "sfec_num_certif": sfec_data.get("sfec_num_certif", ""),
        "sfec_signature": sfec_data.get("sfec_signature", ""),
        "sfec_qr_code": sfec_data.get("sfec_qr_code", ""),
        "sfec_date_certif": sfec_data.get("sfec_date_certif", ""),
        "sfec_id": sfec_data.get("sfec_id", ""),
    }
    return update_invoice(invoice_id, fields)
