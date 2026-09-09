import logging
from datetime import datetime
from sqlite_db import (
    get_cursor, generate_doc_number, calc_line_totals, calc_line_ticket_totals,
    recalc_invoice_totals, log_sync, row_to_dict, rows_to_list
)

logger = logging.getLogger("t-connector.commercial")

# Libelles des types de documents commerciaux (style Sage 100)
DOC_TYPES = {
    "vente":      "Facture",
    "devis":      "Devis",
    "commande":   "Bon de commande",
    "livraison":  "Bon de livraison",
    "avoir":      "Bon d'avoir",
    "achat":      "Facture fournisseur",
    "cmd_fourn":  "Commande fournisseur",
    "reception":  "Bon de reception",
}


def list_docs(type_doc=None, statut=None, tiers_type=None, search=None,
              date_from=None, date_to=None, limit=200, offset=0,
              sort_by="date_facture", sort_dir="DESC"):
    """Liste les documents commerciaux (Devis, Cmd, BL, Avoirs, Factures...)."""
    where_clauses = []
    params = []

    if type_doc:
        where_clauses.append("type_doc = ?")
        params.append(type_doc)
    if statut:
        where_clauses.append("statut = ?")
        params.append(statut)
    if tiers_type:
        # tiers_type dans la table invoices = business/individual; ici on filtre
        # le type de contact source (client vs fournisseur) via contact
        pass
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

    allowed_sorts = {"date_facture", "statut", "numero", "montant_ttc", "id", "tiers_nom"}
    if sort_by not in allowed_sorts:
        sort_by = "date_facture"
    sort_dir = "ASC" if (sort_dir or "DESC").upper() == "ASC" else "DESC"

    params.extend([limit, offset])

    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM invoices WHERE {} ORDER BY {} {} LIMIT ? OFFSET ?".format(where_sql, sort_by, sort_dir),
            params
        )
        docs = rows_to_list(cur.fetchall())
        cur.execute(
            "SELECT COUNT(*) as cnt FROM invoices WHERE {}".format(where_sql),
            params[:-2]
        )
        total = cur.fetchone()["cnt"]

    return {"docs": docs, "total": total, "limit": limit, "offset": offset}


def get_doc(doc_id):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM invoices WHERE id = ?", (doc_id,))
        row = cur.fetchone()
        if not row:
            return None
        doc = row_to_dict(row)
        cur.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY numero_ligne", (doc_id,))
        doc["lignes"] = rows_to_list(cur.fetchall())
        return doc


def create_doc(data):
    """Cree un document commercial (devis, commande, livraison, avoir, facture...)."""
    type_doc = data.get("type_doc", "vente")
    numero = data.get("numero") or generate_doc_number(type_doc)
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
    source = data.get("source", "commercial")
    notes = data.get("notes", "")
    statut = data.get("statut", "brouillon")

    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO invoices (
                numero, date_facture, date_echeance, reference,
                contact_id, tiers_code, tiers_nom, tiers_niu,
                tiers_email, tiers_telephone, tiers_adresse, tiers_type,
                statut, type_doc, source, notes, sfec_statut
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            numero, date_facture, date_echeance, reference,
            contact_id, tiers_code, tiers_nom, tiers_niu,
            tiers_email, tiers_telephone, tiers_adresse, tiers_type,
            statut, type_doc, source, notes, data.get("sfec_statut", "")
        ))
        doc_id = cur.lastrowid

    lignes = data.get("lignes", [])
    negative = type_doc == "avoir"
    _save_doc_lines(doc_id, lignes, negative)
    recalc_invoice_totals(doc_id)

    # Marquer 'valide' par defaut pour les documents hors brouillon
    log_sync("invoices", doc_id, numero, "create", source)
    logger.info("Document cree: %s (%s, id=%d)", numero, type_doc, doc_id)
    return {"id": doc_id, "numero": numero, "type_doc": type_doc}


def update_doc(doc_id, data):
    doc = get_doc(doc_id)
    if not doc:
        return None

    fields = []
    values = []
    updatable = [
        "date_facture", "date_echeance", "reference", "contact_id",
        "tiers_code", "tiers_nom", "tiers_niu", "tiers_email",
        "tiers_telephone", "tiers_adresse", "tiers_type",
        "statut", "valide", "notes", "type_doc"
    ]
    for field in updatable:
        if field in data:
            fields.append("{} = ?".format(field))
            values.append(data[field])

    if fields:
        fields.append("updated_at = datetime('now')")
        values.append(doc_id)
        with get_cursor() as cur:
            cur.execute("UPDATE invoices SET {} WHERE id = ?".format(", ".join(fields)), values)

    if "lignes" in data:
        _delete_doc_lines(doc_id)
        negative = doc.get("type_doc") == "avoir"
        _save_doc_lines(doc_id, data["lignes"], negative)
        recalc_invoice_totals(doc_id)

    log_sync("invoices", doc_id, doc["numero"], "update", "commercial")
    return get_doc(doc_id)


def delete_doc(doc_id):
    doc = get_doc(doc_id)
    if not doc:
        return False
    with get_cursor() as cur:
        cur.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (doc_id,))
        cur.execute("DELETE FROM invoices WHERE id = ?", (doc_id,))
    log_sync("invoices", doc_id, doc["numero"], "delete", "commercial")
    return True


def create_avoir_from_invoice(source_invoice_id, data=None):
    """Cree un avoir a partir d'une facture existante (retour marchandise).
    Copie les lignes avec montants negatifs, option retour de stock."""
    source = get_doc(source_invoice_id)
    if not source or source.get("type_doc") != "vente":
        return None

    data = data or {}
    numero = data.get("numero") or generate_doc_number("avoir")
    date_facture = data.get("date_facture") or datetime.utcnow().strftime("%Y-%m-%d")
    contact_id = data.get("contact_id") or source.get("contact_id")
    tiers_code = data.get("tiers_code") or source.get("tiers_code", "")
    tiers_nom = data.get("tiers_nom") or source.get("tiers_nom", "")
    tiers_niu = data.get("tiers_niu") or source.get("tiers_niu", "")
    tiers_email = data.get("tiers_email") or source.get("tiers_email", "")
    tiers_telephone = data.get("tiers_telephone") or source.get("tiers_telephone", "")
    tiers_adresse = data.get("tiers_adresse") or source.get("tiers_adresse", "")
    tiers_type = source.get("tiers_type", "business")
    notes = data.get("notes", "Avoir lie a la facture {}".format(source.get("numero", "")))

    # Lignes copiees depuis la facture source (montants negatifs)
    lignes = []
    for l in source.get("lignes", []):
        lignes.append({
            "designation": l.get("designation", ""),
            "quantite": l.get("quantite", 0),
            "prix_unitaire": l.get("prix_unitaire", 0),
            "remise_pct": l.get("remise_pct", 0),
            "taux_tva": l.get("taux_tva", 18),
            "code_article": l.get("code_article", ""),
            "code_compte": l.get("code_compte", ""),
            "famille": l.get("famille", ""),
            "unite": l.get("unite", "U"),
            "product_id": l.get("product_id"),
        })

    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO invoices (
                numero, date_facture, date_echeance, reference,
                contact_id, tiers_code, tiers_nom, tiers_niu,
                tiers_email, tiers_telephone, tiers_adresse, tiers_type,
                statut, type_doc, source, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            numero, date_facture, data.get("date_echeance", ""),
            "AVOIR DE " + source.get("numero", ""),
            contact_id, tiers_code, tiers_nom, tiers_niu,
            tiers_email, tiers_telephone, tiers_adresse, tiers_type,
            data.get("statut", "valide"), "avoir", "commercial", notes
        ))
        doc_id = cur.lastrowid

    _save_doc_lines(doc_id, lignes, negative=True)
    recalc_invoice_totals(doc_id)

    # Retour de stock si demande
    if data.get("retour_stock", False):
        for l in lignes:
            if l.get("product_id"):
                with get_cursor() as cur:
                    cur.execute(
                        "UPDATE products SET stock_reel = stock_reel + ? WHERE id = ?",
                        (l.get("quantite", 0), l.get("product_id"))
                    )

    log_sync("invoices", doc_id, numero, "create", "commercial")
    logger.info("Avoir cree: %s (facture source %s, id=%d)", numero, source.get("numero", ""), doc_id)
    return {"id": doc_id, "numero": numero, "type_doc": "avoir"}


def _save_doc_lines(doc_id, lignes, negative=False):
    if not lignes:
        return
    with get_cursor() as cur:
        for i, ligne in enumerate(lignes):
            numero_ligne = ligne.get("numero_ligne", i + 1)
            designation = ligne.get("designation", "")
            quantite = float(ligne.get("quantite", 1)) * (-1 if negative else 1)
            prix_unitaire = float(ligne.get("prix_unitaire", 0)) * (-1 if negative else 1)
            remise_pct = float(ligne.get("remise_pct", 0) or 0)
            taux_tva = float(ligne.get("taux_tva", 18))
            code_article = ligne.get("code_article", "")
            code_compte = ligne.get("code_compte", "")
            famille = ligne.get("famille", "")
            unite = ligne.get("unite", "U")
            product_id = ligne.get("product_id")

            montant_ht, montant_tva, montant_ttc = calc_line_totals(
                abs(quantite), abs(prix_unitaire), remise_pct, taux_tva
            )
            if negative:
                montant_ht, montant_tva, montant_ttc = -montant_ht, -montant_tva, -montant_ttc
            remise_montant = round(abs(quantite) * abs(prix_unitaire) * remise_pct / 100, 2) if remise_pct else 0.0
            if negative:
                remise_montant = -remise_montant

            cur.execute("""
                INSERT INTO invoice_lines (
                    invoice_id, numero_ligne, designation, quantite, prix_unitaire,
                    remise_pct, remise_montant, montant_ht, taux_tva,
                    montant_tva, montant_ttc, code_article, code_compte,
                    famille, unite, product_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                doc_id, numero_ligne, designation, quantite, prix_unitaire,
                remise_pct, remise_montant, montant_ht, taux_tva,
                montant_tva, montant_ttc, code_article, code_compte,
                famille, unite, product_id
            ))


def _delete_doc_lines(doc_id):
    with get_cursor() as cur:
        cur.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (doc_id,))


def doc_stats():
    """Statistiques par type de document pour le hub Sage."""
    stats = {}
    with get_cursor() as cur:
        for td in DOC_TYPES:
            cur.execute("SELECT COUNT(*) c, COALESCE(SUM(montant_ttc), 0) t FROM invoices WHERE type_doc = ?", (td,))
            r = cur.fetchone()
            stats[td] = {"nb": r["c"], "montant": abs(r["t"])}
        cur.execute("SELECT COUNT(*) c FROM invoices WHERE type_doc = 'avoir' AND statut = 'valide'")
        stats["avoirs_valides"] = cur.fetchone()["c"]
    return stats
