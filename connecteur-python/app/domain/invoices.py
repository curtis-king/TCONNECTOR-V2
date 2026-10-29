import logging
import math
import re
from datetime import datetime
from app.storage.db import (
    get_cursor, generate_invoice_number, calc_line_totals,
    recalc_invoice_totals, log_sync, row_to_dict, rows_to_list
)

class InvoiceIntegrityError(ValueError):
    """Levée quand les règles d'intégrité SFEC pour un avoir ne sont pas respectées."""
    pass


def avoir_existe_pour(facture_numero, exclude_invoice_id=None):
    """True si un avoir existe pour la facture de vente donnee (regle 3, §2.5)."""
    with get_cursor() as cur:
        if exclude_invoice_id is not None:
            cur.execute("""
                SELECT 1 FROM invoices
                WHERE type_doc = 'avoir' AND reference_invoice_id = ? AND id != ?
                LIMIT 1
            """, (facture_numero, exclude_invoice_id))
        else:
            cur.execute("""
                SELECT 1 FROM invoices
                WHERE type_doc = 'avoir' AND reference_invoice_id = ?
                LIMIT 1
            """, (facture_numero,))
        return cur.fetchone() is not None

def _validate_avoir(reference_invoice_id, exclude_invoice_id=None):
    """Applique les 3 règles d'intégrité d'un avoir (§2.5 du plan)."""
    if not reference_invoice_id:
        raise InvoiceIntegrityError(
            "reference_invoice_id est obligatoire pour un avoir (type_doc='avoir')."
        )

    facture_origine = get_invoice_by_numero(reference_invoice_id)
    if not facture_origine:
        raise InvoiceIntegrityError(
            "La facture de vente '{}' referencee par l'avoir est introuvable.".format(
                reference_invoice_id
            )
        )

    if facture_origine.get("sfec_statut") not in ("CERTIFIE", "DEJA_CERTIFIE"):
        raise InvoiceIntegrityError(
            "La facture '{}' doit etre certifiee avant de recevoir un avoir "
            "(statut actuel: {}).".format(
                reference_invoice_id, facture_origine.get("sfec_statut") or "non certifie"
            )
        )

    if avoir_existe_pour(reference_invoice_id, exclude_invoice_id=exclude_invoice_id):
        raise InvoiceIntegrityError(
            "Un avoir existe deja pour la facture '{}'.".format(reference_invoice_id)
        )

logger = logging.getLogger("t-connector.invoice")

_ALLOWED_TVA = (0.0, 5.0, 18.0)


def _as_float(value, field):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InvoiceIntegrityError("{} invalide".format(field))
    if not math.isfinite(number):
        raise InvoiceIntegrityError("{} invalide".format(field))
    return number


def _validate_invoice_lines(lignes):
    """Valide les lignes avant ecriture (meme socle que le POS)."""
    if not isinstance(lignes, list) or not lignes:
        raise InvoiceIntegrityError("Au moins une ligne est obligatoire")
    clean = []
    for index, ligne in enumerate(lignes, start=1):
        if not isinstance(ligne, dict):
            raise InvoiceIntegrityError("Ligne {} invalide".format(index))
        designation = str(ligne.get("designation", "") or "").strip()
        if not designation:
            raise InvoiceIntegrityError("Ligne {} : designation obligatoire".format(index))
        quantite = _as_float(ligne.get("quantite", 1), "quantite")
        prix = _as_float(ligne.get("prix_unitaire", 0), "prix unitaire")
        remise_pct = _as_float(ligne.get("remise_pct", 0) or 0, "remise %")
        taux = _as_float(ligne.get("taux_tva", 18), "taux TVA")
        if quantite <= 0:
            raise InvoiceIntegrityError("Ligne {} : la quantite doit etre strictement positive".format(index))
        if prix < 0:
            raise InvoiceIntegrityError("Ligne {} : le prix unitaire ne peut pas etre negatif".format(index))
        if remise_pct < 0 or remise_pct > 100:
            raise InvoiceIntegrityError("Ligne {} : remise % hors bornes (0-100)".format(index))
        if taux not in _ALLOWED_TVA:
            raise InvoiceIntegrityError("Ligne {} : taux de TVA non supporte ({})".format(index, taux))
        item = dict(ligne)
        item.update({
            "numero_ligne": ligne.get("numero_ligne", index),
            "designation": designation,
            "quantite": quantite,
            "prix_unitaire": prix,
            "remise_pct": remise_pct,
            "taux_tva": taux,
        })
        clean.append(item)
    return clean


def create_invoice(data):
    numero = str(data.get("numero") or "").strip() or generate_invoice_number()
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,49}$", numero):
        raise InvoiceIntegrityError("Numero de facture invalide")
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
    payment_method = data.get("payment_method") or "bank_transfer"
    devise = data.get("devise") or "XAF"
    recipient_rccm = data.get("recipient_rccm") or ""
    is_recipient_taxable = data.get("is_recipient_taxable", 1)
    if is_recipient_taxable is None:
        is_recipient_taxable = 1
    reference_invoice_id = data.get("reference_invoice_id") or ""
    credit_note_on_account = 1 if data.get("credit_note_on_account") else 0
    montant_ht_brut = data.get("montant_ht_brut")

    if type_doc == 'avoir' and not credit_note_on_account:
        _validate_avoir(reference_invoice_id)

    lignes = _validate_invoice_lines(data.get("lignes", []))

    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO invoices (
                numero, date_facture, date_echeance, reference,
                contact_id, tiers_code, tiers_nom, tiers_niu,
                tiers_email, tiers_telephone, tiers_adresse, tiers_type,
                statut, type_doc, source, notes, payment_method, devise, recipient_rccm, is_recipient_taxable, reference_invoice_id, credit_note_on_account, montant_ht_brut
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            numero, date_facture, date_echeance, reference,
            contact_id, tiers_code, tiers_nom, tiers_niu,
            tiers_email, tiers_telephone, tiers_adresse, tiers_type,
            statut, type_doc, source, notes, payment_method,
devise,
recipient_rccm,
is_recipient_taxable,
reference_invoice_id,
credit_note_on_account,
montant_ht_brut
        ))
        invoice_id = cur.lastrowid

    _save_lines(invoice_id, lignes)
    recalc_invoice_totals(invoice_id)

    log_sync("invoices", invoice_id, numero, "create", source)
    logger.info("Facture creee: %s (id=%d, source=%s)", numero, invoice_id, source)

    return {"id": invoice_id, "numero": numero}


def _is_invoice_locked(invoice):
    """Une facture certifiee SFEC ou ecrite dans Sage est immuable."""
    return bool(
        invoice.get("sfec_statut") in ("CERTIFIE", "DEJA_CERTIFIE")
        or invoice.get("synced_sage")
        or invoice.get("sage_piece")
        or invoice.get("sfec_num_certif")
    )


def is_invoice_locked(invoice):
    return _is_invoice_locked(invoice)


def is_sfec_certified(invoice):
    """Vrai si une preuve de certification SFEC existe localement.

    Ce test est volontairement distinct de `is_invoice_locked` : une facture
    synchronisee avec Sage reste immuable pour l'edition, mais peut encore
    avoir besoin d'etre certifiee SFEC.
    """
    return bool(
        invoice.get("sfec_statut") in ("CERTIFIE", "DEJA_CERTIFIE")
        or str(invoice.get("sfec_num_certif") or "").strip()
    )


def is_sage_synced(invoice):
    """Vrai si la facture provient de Sage ou y a deja ete ecrite."""
    return bool(invoice.get("synced_sage") or invoice.get("sage_piece"))


def update_invoice(invoice_id, data):
    invoice = get_invoice(invoice_id)
    if not invoice:
        return None
    if _is_invoice_locked(invoice):
        raise InvoiceIntegrityError(
            "Facture '{}' certifiee ou synchronisee Sage : modification interdite.".format(
                invoice.get("numero", invoice_id)))

    # Alignement edit/create : gérer type de document + origine d'un avoir
    if ("type_doc" in data or "reference_invoice_id" in data
            or "credit_note_on_account" in data):
        data = dict(data)
        new_type_doc = data.get("type_doc", invoice.get("type_doc", "vente"))
        new_on_account = bool(data.get(
            "credit_note_on_account", invoice.get("credit_note_on_account", 0)))
        if new_type_doc == "avoir":
            ref = data.get("reference_invoice_id") or invoice.get("reference_invoice_id") or ""
            data["reference_invoice_id"] = ref
            data["credit_note_on_account"] = 1 if new_on_account else 0
            if not new_on_account:
                _validate_avoir(ref, exclude_invoice_id=invoice_id)
        else:
            data["reference_invoice_id"] = ""
            data["credit_note_on_account"] = 0

    fields = []
    values = []
    updatable = [
        "date_facture", "date_echeance", "reference", "contact_id",
        "tiers_code", "tiers_nom", "tiers_niu", "tiers_email",
        "tiers_telephone", "tiers_adresse", "tiers_type",
        "statut", "valide", "notes", "payment_method", "devise", "recipient_rccm",
        "is_recipient_taxable", "payment_date",
        "type_doc", "reference_invoice_id", "credit_note_on_account",
        "montant_ht_brut",
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
        lignes = _validate_invoice_lines(data["lignes"])
        _delete_lines(invoice_id)
        _save_lines(invoice_id, lignes)
        recalc_invoice_totals(invoice_id)

    log_sync("invoices", invoice_id, invoice["numero"], "update", "web")
    logger.info("Facture mise a jour: %s (id=%d)", invoice["numero"], invoice_id)

    return get_invoice(invoice_id)

def delete_invoice(invoice_id):
    invoice = get_invoice(invoice_id)
    if not invoice:
        return False
    if _is_invoice_locked(invoice):
        raise InvoiceIntegrityError(
            "Facture '{}' certifiee ou synchronisee Sage : suppression interdite.".format(
                invoice.get("numero", invoice_id)))

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


def _invoices_where(type_doc=None, statut=None, source=None, search=None,
                    date_from=None, date_to=None):
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
    return where_sql, params


def list_invoices(type_doc=None, statut=None, source=None, search=None,
                  date_from=None, date_to=None, limit=200, offset=0,
                  sort_by="date_facture", sort_dir="DESC"):
    where_sql, params = _invoices_where(
        type_doc=type_doc, statut=statut, source=source, search=search,
        date_from=date_from, date_to=date_to)

    allowed_sorts = {"date_facture", "statut", "source", "numero", "montant_ttc", "id", "tiers_nom"}
    if sort_by not in allowed_sorts:
        sort_by = "date_facture"
    sort_dir = "ASC" if (sort_dir or "DESC").upper() == "ASC" else "DESC"

    params.extend([limit, offset])

    with get_cursor() as cur:
        cur.execute(
            "SELECT id, numero, date_facture, date_echeance, reference, contact_id, tiers_code, tiers_nom, tiers_niu, tiers_email, tiers_telephone, tiers_adresse, tiers_type, montant_ht, montant_tva, montant_ttc, montant_restant, statut, valide, type_doc, source, sfec_statut, sfec_num_certif, sfec_date_certif, sfec_id, synced_sage, sage_domaine, sage_type, sage_piece, vendeur_id, payment_method, devise, montant_ht_brut, total_tax_t_amount, total_tax_r_amount, total_exempt_amount, discount_amount, total_line_discount_amount, additional_cent_tax, electronic_stamp_duty, is_recipient_taxable, recipient_rccm, reference_invoice_id, payment_date, created_at, updated_at FROM invoices WHERE {} ORDER BY {} {} LIMIT ? OFFSET ?".format(where_sql, sort_by, sort_dir),
            params
        )
        invoices = rows_to_list(cur.fetchall())

        cur.execute(
            "SELECT COUNT(*) as cnt FROM invoices WHERE {}".format(where_sql),
            params[:-2]
        )
        total = cur.fetchone()["cnt"]

    return {"invoices": invoices, "total": total, "limit": limit, "offset": offset}


def filtered_invoices_stats(type_doc=None, statut=None, source=None, search=None,
                            date_from=None, date_to=None):
    """Cartes de stats recalculees sur le perimetre filtre (BL-03)."""
    where_sql, params = _invoices_where(
        type_doc=type_doc, statut=statut, source=source, search=search,
        date_from=date_from, date_to=date_to)
    with get_cursor() as cur:
        cur.execute("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN statut = 'brouillon' THEN 1 ELSE 0 END), 0) AS brouillons,
                COALESCE(SUM(CASE WHEN statut = 'valide' THEN 1 ELSE 0 END), 0) AS validees,
                COALESCE(SUM(CASE WHEN sfec_statut IN ('CERTIFIE', 'DEJA_CERTIFIE') THEN 1 ELSE 0 END), 0) AS certifiees,
                COALESCE(SUM(CASE WHEN sfec_statut = 'EN_COURS' THEN 1 ELSE 0 END), 0) AS certif_en_cours,
                COALESCE(SUM(CASE WHEN sfec_statut = 'ERREUR' THEN 1 ELSE 0 END), 0) AS certif_erreur,
                COALESCE(SUM(CASE WHEN statut != 'brouillon'
                                  THEN CASE WHEN type_doc = 'avoir' THEN -montant_ttc ELSE montant_ttc END
                                  ELSE 0 END), 0) AS ca,
                COALESCE(SUM(CASE WHEN statut != 'brouillon' AND montant_restant > 0
                                  THEN montant_restant ELSE 0 END), 0) AS impayes,
                COALESCE(SUM(CASE WHEN source IN ('web', 'pos') AND synced_sage = 0
                                  AND statut != 'brouillon'
                                  AND (source != 'pos' OR (sfec_num_certif IS NOT NULL AND sfec_num_certif != ''))
                                  THEN 1 ELSE 0 END), 0) AS pending_sage
            FROM invoices WHERE {where}
        """.format(where=where_sql), params)
        row = cur.fetchone()
        return {k: row[k] for k in row.keys()}


def list_invoices_for_sfec(limit=500):
    with get_cursor() as cur:
        cur.execute("""
            SELECT * FROM invoices
            WHERE statut IN ('valide', 'a_comptabiliser')
              AND sfec_statut NOT IN ('CERTIFIE', 'DEJA_CERTIFIE', 'EN_COURS')
              AND sfec_statut != 'ERREUR'
            ORDER BY date_facture DESC  LIMIT ?
        """, (limit,))
        return rows_to_list(cur.fetchall())


def count_invoices():
    with get_cursor() as cur:
        cur.execute("""
            SELECT 
                COUNT(*) AS total,
                
                -- ASTUCE SQLITE : SUM(condition) remplace SUM(CASE WHEN...)
                SUM(source = 'web') AS from_web,
                SUM(source = 'sage') AS from_sage,
                SUM(statut = 'brouillon') AS brouillons,
                SUM(statut = 'valide') AS validees,
                SUM(sfec_statut IN ('CERTIFIE', 'DEJA_CERTIFIE')) AS certifiees,
                SUM(sfec_statut = 'EN_COURS') AS certif_en_cours,
                SUM(sfec_statut = 'EN_COURS') AS en_cours_certif,
                SUM(sfec_statut = 'ERREUR') AS certif_erreur,
                SUM(statut != 'brouillon' AND sfec_statut NOT IN ('CERTIFIE', 'DEJA_CERTIFIE')) AS non_certifiees,
                SUM(type_doc = 'avoir') AS avoirs,
                SUM(type_doc = 'vente') AS ventes,
                
                -- Pour les montants, on garde le CASE WHEN (ou on multiplie par la condition)
                -- Les avoirs sont soustraits du CA (regle metier, cf. test_ca_soustrait_les_avoirs)
                COALESCE(SUM(CASE WHEN statut != 'brouillon'
                                  THEN CASE WHEN type_doc = 'avoir' THEN -montant_ttc ELSE montant_ttc END
                                  ELSE 0 END), 0) AS ca_total,
                
                SUM(source IN ('web', 'pos') AND synced_sage = 0 AND statut != 'brouillon' 
                    AND (source != 'pos' OR (sfec_num_certif IS NOT NULL AND sfec_num_certif != ''))) AS pending_sage,
                
                COALESCE(SUM(CASE WHEN statut != 'brouillon' AND montant_restant > 0 THEN montant_restant ELSE 0 END), 0) AS impayes_total,
                SUM(statut != 'brouillon' AND montant_restant > 0) AS impayes_nb,
                
                SUM(statut != 'brouillon' AND date_facture >= strftime('%Y-%m-01', 'now') 
                    AND date_facture < strftime('%Y-%m-01', 'now', '+1 month')) AS mois_nb,
                    
                COALESCE(SUM(CASE WHEN statut != 'brouillon' AND date_facture >= strftime('%Y-%m-01', 'now') 
                    AND date_facture < strftime('%Y-%m-01', 'now', '+1 month')
                    THEN CASE WHEN type_doc = 'avoir' THEN -montant_ht ELSE montant_ht END ELSE 0 END), 0) AS mois_ht,
                    
                COALESCE(SUM(CASE WHEN statut != 'brouillon' AND date_facture >= strftime('%Y-%m-01', 'now') 
                    AND date_facture < strftime('%Y-%m-01', 'now', '+1 month')
                    THEN CASE WHEN type_doc = 'avoir' THEN -montant_tva ELSE montant_tva END ELSE 0 END), 0) AS mois_tva,
                    
                COALESCE(SUM(CASE WHEN statut != 'brouillon' AND date_facture >= strftime('%Y-%m-01', 'now') 
                    AND date_facture < strftime('%Y-%m-01', 'now', '+1 month')
                    THEN CASE WHEN type_doc = 'avoir' THEN -montant_ttc ELSE montant_ttc END ELSE 0 END), 0) AS mois_ttc

            FROM invoices
        """)
        
        # SQLite renvoie un objet dict-like si tu utilises sqlite3.Row,
        # dict(r) te permet de tout convertir d'un coup très proprement.
        r = cur.fetchone()
        
        # On s'assure qu'aucune valeur n'est None (remplacé par 0)
        return {k: (v if v is not None else 0) for k, v in dict(r).items()}


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
            subtotal = ligne.get("subtotal") or 0.0
            discount_type = ligne.get("discount_type") or "fixed"
            type_article = ligne.get("type_article") or "product"
            classification_code = ligne.get("classification_code") or ""
            try:
                nb_colis = float(ligne.get("nb_colis") or 0)
            except (TypeError, ValueError):
                nb_colis = 0.0
            conditionnement = ligne.get("conditionnement") or ""
            try:
                poids_kg = float(ligne.get("poids_kg") or 0)
            except (TypeError, ValueError):
                poids_kg = 0.0

            montant_ht, montant_tva, montant_ttc = calc_line_totals(
                quantite, prix_unitaire, remise_pct, taux_tva
            )
            remise_montant = round(quantite * prix_unitaire * remise_pct / 100, 2) if remise_pct else 0.0

            cur.execute("""
                INSERT INTO invoice_lines (
                    invoice_id, numero_ligne, designation, quantite, prix_unitaire,
                    remise_pct, remise_montant, montant_ht, taux_tva,
                    montant_tva, montant_ttc, code_article, code_compte,
                    famille, unite, product_id, subtotal,
                    discount_type,
                    type_article,
                    classification_code,
                    nb_colis,
                    conditionnement,
                    poids_kg
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                invoice_id, numero_ligne, designation, quantite, prix_unitaire,
                remise_pct, remise_montant, montant_ht, taux_tva,
                montant_tva, montant_ttc, code_article, code_compte,
                famille, unite, product_id, subtotal,
                discount_type,
                type_article,
                classification_code,
                nb_colis,
                conditionnement,
                poids_kg,
            ))


def _delete_lines(invoice_id):
    with get_cursor() as cur:
        cur.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))


def update_invoice_sfec(invoice_id, sfec_data=None, **kwargs):
    """Ecriture reservee au flux de certification (contourne l'immuabilite)."""
    data = dict(sfec_data or {}) if isinstance(sfec_data, dict) else {}
    data.update(kwargs)
    fields = {
        "sfec_statut": data.get("sfec_statut", data.get("statut", "")),
        "sfec_num_certif": data.get("sfec_num_certif", data.get("certification_number", "")),
        "sfec_signature": data.get("sfec_signature", data.get("signature", "")),
        "sfec_qr_code": data.get("sfec_qr_code", data.get("qr_code", "")),
        "sfec_date_certif": data.get("sfec_date_certif", data.get("certification_date", "")),
        "sfec_id": data.get("sfec_id", ""),
    }
    with get_cursor() as cur:
        cur.execute("SELECT id, numero FROM invoices WHERE id = ?", (invoice_id,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("""
            UPDATE invoices SET
                sfec_statut = CASE WHEN ? <> '' THEN ? ELSE sfec_statut END,
                sfec_num_certif = CASE WHEN ? <> '' THEN ? ELSE sfec_num_certif END,
                sfec_signature = CASE WHEN ? <> '' THEN ? ELSE sfec_signature END,
                sfec_qr_code = CASE WHEN ? <> '' THEN ? ELSE sfec_qr_code END,
                sfec_date_certif = CASE WHEN ? <> '' THEN ? ELSE sfec_date_certif END,
                sfec_id = CASE WHEN ? <> '' THEN ? ELSE sfec_id END,
                updated_at = datetime('now')
            WHERE id = ?
        """, (
            fields["sfec_statut"], fields["sfec_statut"],
            fields["sfec_num_certif"], fields["sfec_num_certif"],
            fields["sfec_signature"], fields["sfec_signature"],
            fields["sfec_qr_code"], fields["sfec_qr_code"],
            fields["sfec_date_certif"], fields["sfec_date_certif"],
            fields["sfec_id"], fields["sfec_id"],
            invoice_id,
        ))
    log_sync("invoices", invoice_id, row["numero"], "certify", "sfec")
    return get_invoice(invoice_id)
