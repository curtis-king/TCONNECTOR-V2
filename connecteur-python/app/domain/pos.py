import hashlib
import logging
import math
import re
import sqlite3
from datetime import datetime
from app.storage.db import (
    get_cursor, generate_ticket_number, generate_invoice_number,
    recalc_ticket_totals, calc_line_ticket_totals,
    log_sync, row_to_dict, rows_to_list, get_setting, compute_cent_tax
)
from app.config.manager import get_config, get_sfec_api_key
from app.core.utils import is_counter_customer

_MODE_PAIEMENT_TO_SFEC = {
    "especes": "cash",
    "carte": "card",
    "cheque": "cheque",
    "virement": "bank_transfer",
    "mobile_money": "mobile_money",
}

def _map_payment_method(mode_paiement):
    return _MODE_PAIEMENT_TO_SFEC.get((mode_paiement or "").strip().lower(), "bank_transfer")


_ALLOWED_TVA = (0.0, 5.0, 18.0)
_MODES_PAIEMENT = {"especes", "carte", "cheque", "virement", "mobile_money"}
_TIERS_BUSINESS = {"business", "entreprise", "government", "gouvernement"}


def _as_float(value, field, default=None):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("{} invalide".format(field))
    if not math.isfinite(number):
        raise ValueError("{} invalide".format(field))
    return number


def _validate_ticket_lines(lignes):
    """Normalise et valide les lignes avant toute ecriture.

    Regles conservatrices : quantites > 0, prix >= 0, TVA 0/5/18,
    remises bornees. Retourne (lignes_normalisees, total_ttc, erreur).
    """
    if not isinstance(lignes, list) or not lignes:
        return [], 0.0, "Aucune ligne dans le ticket"
    clean = []
    total_ttc = 0.0
    for index, ligne in enumerate(lignes, start=1):
        if not isinstance(ligne, dict):
            return [], 0.0, "Ligne {} invalide".format(index)
        designation = str(ligne.get("designation", "") or "").strip()
        if not designation:
            return [], 0.0, "Ligne {} : designation obligatoire".format(index)
        try:
            quantite = _as_float(ligne.get("quantite", 1), "quantite")
            prix = _as_float(ligne.get("prix_unitaire", 0), "prix unitaire")
            taux = _as_float(ligne.get("taux_tva", 18), "taux TVA")
            remise_pct = _as_float(ligne.get("remise_pct", 0) or 0, "remise %")
            remise_montant = _as_float(ligne.get("remise_montant", 0) or 0, "remise montant")
        except ValueError as exc:
            return [], 0.0, "Ligne {} : {}".format(index, exc)
        if quantite <= 0:
            return [], 0.0, "Ligne {} : la quantite doit etre strictement positive".format(index)
        if prix < 0:
            return [], 0.0, "Ligne {} : le prix unitaire ne peut pas etre negatif".format(index)
        if taux not in _ALLOWED_TVA:
            return [], 0.0, "Ligne {} : taux de TVA non supporte ({})".format(index, taux)
        if remise_pct < 0 or remise_pct > 100:
            return [], 0.0, "Ligne {} : remise % hors bornes (0-100)".format(index)
        subtotal = round(quantite * prix, 2)
        if remise_montant < 0 or remise_montant > subtotal:
            return [], 0.0, "Ligne {} : remise montant hors bornes".format(index)
        product_id = ligne.get("product_id")
        if product_id in ("", None):
            product_id = None
        else:
            try:
                product_id = int(product_id)
            except (TypeError, ValueError):
                return [], 0.0, "Ligne {} : article invalide".format(index)
        net_ht, montant_tva, montant_ttc, _ = calc_line_ticket_totals(
            quantite, prix, remise_pct, remise_montant, taux)
        total_ttc += montant_ttc
        item = dict(ligne)
        item.update({
            "numero_ligne": index,
            "designation": designation,
            "quantite": quantite,
            "prix_unitaire": prix,
            "taux_tva": taux,
            "remise_pct": remise_pct,
            "remise_montant": remise_montant,
            "product_id": product_id,
            "montant_ht": net_ht,
            "montant_tva": montant_tva,
            "montant_ttc": montant_ttc,
        })
        clean.append(item)
    return clean, round(total_ttc, 2), ""


def _normalise_tiers_type(raw, default="individual"):
    value = str(raw or "").strip().lower()
    if value in ("business", "entreprise", "societe", "company"):
        return "business"
    if value in ("government", "gouvernement", "public"):
        return "government"
    if value in ("foreign", "etranger", "étranger"):
        return "foreign"
    if value in ("individual", "particulier", "individu", "personne"):
        return "individual"
    if value in ("client", "fournisseur", "supplier", "customer"):
        return "business"
    return default


def _resolve_ticket_tiers(data):
    """Hydrate les champs fiscaux depuis le contact en base (source fiable)."""
    contact_id = data.get("contact_id")
    code = str(data.get("tiers_code", "") or "").strip()
    try:
        sfec_cfg = get_config().get("sfec", {})
    except Exception:
        sfec_cfg = {}
    contact = None
    with get_cursor() as cur:
        if contact_id:
            try:
                cur.execute("SELECT * FROM contacts WHERE id = ?", (int(contact_id),))
            except (TypeError, ValueError):
                cur.execute("SELECT * FROM contacts WHERE code = ?", (code,))
            contact = cur.fetchone()
        elif code and code != "CLI-CPT":
            cur.execute("SELECT * FROM contacts WHERE code = ?", (code,))
            contact = cur.fetchone()

    if contact:
        contact = row_to_dict(contact)
        tiers_type = _normalise_tiers_type(
            contact.get("type_raw"), _normalise_tiers_type(contact.get("type")))
        if is_counter_customer(
                contact.get("code"), contact.get("nom"), sfec_cfg):
            tiers_type = "individual"
        return {
            "contact_id": contact["id"],
            "tiers_code": contact.get("code", ""),
            "tiers_nom": contact.get("nom", "") or "Client comptoir",
            "tiers_niu": (contact.get("niu", "") or "").strip().upper(),
            "recipient_rccm": (contact.get("rccm", "") or "").strip(),
            "tiers_email": contact.get("email", "") or "",
            "tiers_telephone": contact.get("telephone", "") or "",
            "tiers_adresse": contact.get("adresse", "") or "",
            "tiers_type": tiers_type,
            "is_recipient_taxable": 1 if contact.get("is_taxable", 1) else 0,
        }, ""

    if contact_id:
        return None, "Contact introuvable"

    tiers_type = _normalise_tiers_type(data.get("tiers_type"), "individual")
    if is_counter_customer(code, data.get("tiers_nom"), sfec_cfg):
        tiers_type = "individual"
    return {
        "contact_id": None,
        "tiers_code": code,
        "tiers_nom": str(data.get("tiers_nom", "") or "Client comptoir").strip(),
        "tiers_niu": str(data.get("tiers_niu", "") or "").strip().upper(),
        "recipient_rccm": str(data.get("tiers_rccm", "") or "").strip(),
        "tiers_email": data.get("tiers_email", "") or "",
        "tiers_telephone": data.get("tiers_telephone", "") or "",
        "tiers_adresse": data.get("tiers_adresse", "") or "",
        "tiers_type": tiers_type,
        "is_recipient_taxable": 1 if data.get("is_recipient_taxable", 1) else 0,
    }, ""


logger = logging.getLogger("t-connector.pos")


def stock_control_enabled():
    try:
        return bool(get_config().get("pos", {}).get("stock_control", True))
    except Exception:
        return True


def create_ticket(data):
    if not isinstance(data, dict):
        return {"success": False, "error": "Donnees ticket invalides"}

    # Idempotence (PO-25/PO-63) : un retry reseau ne duplique ni le ticket,
    # ni la facture, ni la certification SFEC.
    idem_key = str(data.get("idempotency_key") or "").strip()
    if idem_key:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$", idem_key):
            return {"success": False, "error": "Cle d'idempotence invalide"}
        with get_cursor() as cur:
            cur.execute(
                "SELECT p.ticket_id AS tid, t.numero AS tnum, t.montant_ttc AS ttc,"
                " t.monnaie_rendue AS monnaie, t.mode_paiement AS mode,"
                " t.invoice_id AS iid, i.numero AS inum"
                " FROM pos_idempotency p"
                " JOIN pos_tickets t ON t.id = p.ticket_id"
                " LEFT JOIN invoices i ON i.id = t.invoice_id"
                " WHERE p.key = ?", (idem_key,))
            dup = cur.fetchone()
        if dup:
            logger.info("Idempotence POS : cle %s deja traitee (ticket %s)",
                        idem_key, dup["tnum"])
            return {"success": True, "duplicate": True,
                    "id": dup["tid"], "numero": dup["tnum"] or "",
                    "mode_paiement": dup["mode"] or "",
                    "montant_ttc": dup["ttc"] or 0,
                    "monnaie_rendue": dup["monnaie"] or 0,
                    "invoice_id": dup["iid"],
                    "invoice_numero": dup["inum"] or ""}

    lignes, total_preview, err = _validate_ticket_lines(data.get("lignes"))
    if err:
        return {"success": False, "error": err}

    mode_paiement = str(data.get("mode_paiement", "especes") or "especes").strip().lower()
    if mode_paiement not in _MODES_PAIEMENT:
        return {"success": False, "error": "Mode de paiement invalide"}
    try:
        montant_recu = _as_float(data.get("montant_recu", 0) or 0, "montant recu")
        remise_globale_pct = _as_float(data.get("remise_globale_pct", 0) or 0, "remise globale %")
        remise_globale_montant = _as_float(data.get("remise_globale_montant", 0) or 0, "remise globale")
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if montant_recu < 0:
        return {"success": False, "error": "Le montant recu ne peut pas etre negatif"}
    if remise_globale_pct < 0 or remise_globale_pct > 100:
        return {"success": False, "error": "Remise globale % hors bornes (0-100)"}
    if remise_globale_montant < 0 or remise_globale_montant > total_preview:
        return {"success": False, "error": "Remise globale hors bornes"}
    remise_prevue = round(total_preview * remise_globale_pct / 100 + remise_globale_montant, 2)
    total_apres_remise = max(round(total_preview - remise_prevue, 2), 0.0)
    if mode_paiement == "especes" and montant_recu + 0.001 < total_apres_remise:
        return {"success": False, "error": "Montant recu insuffisant pour un paiement en especes"}

    tiers, tiers_err = _resolve_ticket_tiers(data)
    if tiers_err:
        return {"success": False, "error": tiers_err}
    if tiers["tiers_type"] in ("business", "government"):
        niu = tiers.get("tiers_niu", "")
        if not niu:
            return {"success": False,
                    "error": "NIU obligatoire pour un client entreprise/gouvernement"}
        if not (16 <= len(niu) <= 17) or not niu.isalnum():
            return {"success": False,
                    "error": "NIU invalide (16 a 17 caracteres alphanumeriques attendus)"}

    data = dict(data)
    data.update(tiers)
    data["lignes"] = lignes
    data["mode_paiement"] = mode_paiement
    data["montant_recu"] = montant_recu
    data["remise_globale_pct"] = remise_globale_pct
    data["remise_globale_montant"] = remise_globale_montant

    stock_check = check_stock_available(lignes)
    if not stock_check["ok"]:
        manque = stock_check["manque"]
        detail = ", ".join(
            "{} (stock {}, demande {})".format(m["designation"], m.get("stock", 0), m["qte"])
            for m in manque
        )
        return {"success": False, "error": "Stock insuffisant: " + detail}

    try:
        numero = data.get("numero") or generate_ticket_number()
        date_ticket = data.get("date_ticket") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        vendeur_id = data.get("vendeur_id")
        if vendeur_id in ("", None):
            vendeur_id = None
        else:
            vendeur_id = int(vendeur_id)
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO pos_tickets (
                    numero, date_ticket, contact_id, tiers_nom,
                    mode_paiement, vendeur_id, notes, caissier,
                    remise_globale_pct, remise_globale_montant
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (numero, date_ticket, data["contact_id"], data["tiers_nom"],
                  mode_paiement, vendeur_id, data.get("notes", ""),
                  data.get("caissier", "") or "", remise_globale_pct,
                  remise_globale_montant))
            ticket_id = cur.lastrowid

        _save_ticket_lines(ticket_id, lignes)
        totals = recalc_ticket_totals(ticket_id)
        montant_ttc = totals["montant_ttc"]

        monnaie_rendue = max(montant_recu - montant_ttc, 0)
        with get_cursor() as cur:
            cur.execute("""
                UPDATE pos_tickets SET montant_recu = ?, monnaie_rendue = ? WHERE id = ?
            """, (montant_recu, monnaie_rendue, ticket_id))

        decremented = []
        _decrement_stock(lignes, decremented)

        invoice_result = _create_invoice_for_ticket(ticket_id, data, totals)
        if not invoice_result.get("invoice_id"):
            _rollback_ticket(ticket_id, decremented)
            return {
                "success": False,
                "error": "Facture non creee: {}".format(
                    invoice_result.get("sage_error") or invoice_result.get("sfec_error") or "erreur interne"),
            }

        log_sync("pos_tickets", ticket_id, numero, "create", "pos")
        logger.info("Ticket cree: %s (id=%d)", numero, ticket_id)
    except Exception as exc:
        logger.error("Creation ticket echouee: %s", exc)
        _rollback_ticket(locals().get("ticket_id"), locals().get("decremented") or [])
        return {"success": False, "error": "Creation du ticket impossible: {}".format(str(exc)[:200])}

    result = {"success": True, "id": ticket_id, "numero": numero,
              "mode_paiement": mode_paiement,
              "montant_ttc": montant_ttc, "monnaie_rendue": monnaie_rendue}
    result.update(invoice_result)
    if idem_key:
        try:
            with get_cursor() as cur:
                cur.execute(
                    "INSERT OR IGNORE INTO pos_idempotency (key, ticket_id) VALUES (?, ?)",
                    (idem_key, ticket_id))
        except Exception as e:
            logger.warning("Enregistrement idempotence impossible: %s", e)
    return result


def _rollback_ticket(ticket_id, lignes):
    """Compensation si la facture locale n'a pas pu etre creee."""
    if not ticket_id:
        return
    try:
        _restore_stock(lignes)
        with get_cursor() as cur:
            cur.execute("DELETE FROM pos_ticket_lines WHERE ticket_id = ?", (ticket_id,))
            cur.execute("DELETE FROM pos_tickets WHERE id = ?", (ticket_id,))
    except Exception as exc:
        logger.error("Rollback ticket %s impossible: %s", ticket_id, exc)


def _create_invoice_for_ticket(ticket_id, data, totals):
    """Cree une facture associee au ticket POS et la pousse vers Sage
    (comportement du module de saisie de caisse Sage)."""
    try:
        numero = generate_invoice_number()
        date_ticket = data.get("date_ticket") or datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        date_facture = date_ticket[:10]
        tiers_nom = data.get("tiers_nom", "Client comptoir")
        mode_paiement = data.get("mode_paiement", "especes")
        payment_method = _map_payment_method(mode_paiement)
        devise = data.get("devise", "XAF") or "XAF"
        tiers_niu = data.get("tiers_niu", "") or ""
        recipient_rccm = data.get("recipient_rccm", "") or ""
        is_recipient_taxable = 1 if data.get("is_recipient_taxable", 1) else 0

        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO invoices (
                    numero, date_facture, reference, contact_id,
                    tiers_code, tiers_nom, tiers_niu, tiers_type, tiers_email,
                    tiers_telephone, tiers_adresse, montant_ht, montant_tva,
                    montant_ttc, montant_restant, statut, valide,
                    type_doc, source, vendeur_id, notes,
                    payment_method, devise, recipient_rccm, is_recipient_taxable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'a_comptabiliser', 1,
                          'vente', 'pos', ?, ?, ?, ?, ?, ?)
            """, (
                numero, date_facture, "PAIEMENT " + mode_paiement,
                data.get("contact_id"), data.get("tiers_code", ""), tiers_nom,
                tiers_niu, data.get("tiers_type", "individual"),
                data.get("tiers_email", ""), data.get("tiers_telephone", ""),
                data.get("tiers_adresse", ""),
                totals.get("montant_ht", 0), totals.get("montant_tva", 0),
                totals.get("montant_ttc", 0), 0,
                data.get("vendeur_id"), "",
                payment_method, devise, recipient_rccm, is_recipient_taxable,
            ))
            invoice_id = cur.lastrowid

        ligne_infos = []
        montant_ht_brut = 0.0
        total_line_discount_amount = 0.0
        total_tax_t_amount = 0.0
        total_tax_r_amount = 0.0
        total_exempt_amount = 0.0
        sum_line_net_ht = 0.0

        with get_cursor() as cur:
            for i, ligne in enumerate(data.get("lignes", [])):
                quantite = float(ligne.get("quantite", 1))
                prix_unitaire = float(ligne.get("prix_unitaire", 0))
                taux_tva = float(ligne.get("taux_tva", 18))
                remise_pct = float(ligne.get("remise_pct", 0) or 0)
                remise_montant_saisie = float(ligne.get("remise_montant", 0) or 0)

                net_ht, montant_tva, montant_ttc, remise_total = calc_line_ticket_totals(
                    quantite, prix_unitaire, remise_pct, remise_montant_saisie, taux_tva
                )

                subtotal = round(quantite * prix_unitaire, 2)
                discount_type = "percentage" if remise_pct else "fixed"
                type_article = ligne.get("type_article", "product")
                classification_code = ligne.get("classification_code", ligne.get("famille", ""))

                cur.execute("""
                    INSERT INTO invoice_lines (
                        invoice_id, numero_ligne, designation, quantite, prix_unitaire,
                        remise_pct, remise_montant, montant_ht, taux_tva, montant_tva, montant_ttc,
                        code_article, code_compte, famille, product_id,
                        subtotal, discount_type, type_article, classification_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    invoice_id, i + 1, ligne.get("designation", ""), quantite,
                    prix_unitaire, remise_pct, remise_total, net_ht, taux_tva, montant_tva, montant_ttc,
                    ligne.get("code_article", ""), ligne.get("code_compte", ""),
                    ligne.get("famille", ""), ligne.get("product_id"),
                    subtotal, discount_type, type_article, classification_code
                ))

                montant_ht_brut += subtotal
                total_line_discount_amount += remise_total
                sum_line_net_ht += net_ht
                if taux_tva == 5:
                    total_tax_r_amount += montant_tva
                elif taux_tva == 0:
                    total_exempt_amount += net_ht
                else:
                    total_tax_t_amount += montant_tva

                ligne_infos.append({
                    "numero_ligne": i + 1, "designation": ligne.get("designation", ""),
                    "quantite": quantite, "prix_unitaire": prix_unitaire,
                    "montant_ht": net_ht, "taux_tva": taux_tva,
                    "montant_tva": montant_tva, "montant_ttc": montant_ttc,
                    "code_article": ligne.get("code_article", ""),
                    "code_compte": ligne.get("code_compte", ""),
                    "famille": ligne.get("famille", ""),
                    "product_id": ligne.get("product_id"),
                    "remise_montant": remise_total,
                    "discount_type": discount_type,
                    "type_article": type_article,
                    "classification_code": classification_code,
                    "subtotal": subtotal,
                })

        # Remise globale du ticket (non repartie par ligne) : ecart entre la somme
        # des net_ht par ligne et le montant_ht final du ticket, deja reduit par
        # recalc_ticket_totals via remise_globale_pct / remise_globale_montant.
        discount_amount = round(max(sum_line_net_ht - totals.get("montant_ht", sum_line_net_ht), 0), 2)
        # Centimes additionnels : 5 % de la TVA COLLECTEE (toutes tranches,
        # decret n° 2023-1737 art. 22 : « base = montant de la TVA collectee »).
        # Taux configurable via le setting cent_tax_rate (defaut 5).
        additional_cent_tax = compute_cent_tax(total_tax_t_amount + total_tax_r_amount)

        with get_cursor() as cur:
            cur.execute("""
                UPDATE invoices SET
                    montant_ht_brut = ?, total_tax_t_amount = ?, total_tax_r_amount = ?,
                    total_exempt_amount = ?, total_line_discount_amount = ?,
                    discount_amount = ?, additional_cent_tax = ?
                WHERE id = ?
            """, (
                round(montant_ht_brut, 2), round(total_tax_t_amount, 2), round(total_tax_r_amount, 2),
                round(total_exempt_amount, 2), round(total_line_discount_amount, 2),
                discount_amount, additional_cent_tax,
                invoice_id
            ))

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
            if sfec_cfg.get("enabled") and get_sfec_api_key(sfec_cfg) and is_online():
                cert_inv = {
                    "id": invoice_id, "numero": numero,
                    "reference": "PAIEMENT " + mode_paiement,
                    "date_facture": date_facture, "date_echeance": date_facture,
                    "montant_ht": totals.get("montant_ht", 0),
                    "montant_tva": totals.get("montant_tva", 0),
                    "montant_ttc": totals.get("montant_ttc", 0),
                    "montant_restant": 0,
                    "tiers_nom": tiers_nom,
                    "tiers_type": data.get("tiers_type", "individual"),
                    "tiers_niu": tiers_niu,
                    "tiers_telephone": data.get("tiers_telephone", ""),
                    "tiers_email": data.get("tiers_email", ""),
                    "tiers_adresse": data.get("tiers_adresse", ""),
                    "recipient_rccm": recipient_rccm,
                    "is_recipient_taxable": is_recipient_taxable,
                    "type_doc": "vente",
                    "payment_method": payment_method,
                    "devise": devise,
                    "discount_amount": discount_amount,
                    "additional_cent_tax": additional_cent_tax,
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
        where_clauses.append("date(t.date_ticket) <= date(?)")
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
        cur.execute("""
            SELECT COALESCE(SUM(CASE WHEN t.statut = 'valide' THEN t.montant_ttc ELSE 0 END), 0) AS ca
            FROM pos_tickets t
            LEFT JOIN vendeurs v ON t.vendeur_id = v.id
            WHERE {where}
        """.format(where=where_sql), count_params)
        ca_total = cur.fetchone()["ca"]

    return {"tickets": tickets, "total": total, "ca_total": ca_total}


def count_tickets():
    with get_cursor() as cur:
        stats = {}
        cur.execute("SELECT COUNT(*) as cnt FROM pos_tickets")
        stats["total"] = cur.fetchone()["cnt"]
        cur.execute("SELECT COALESCE(SUM(montant_ttc), 0) as total FROM pos_tickets WHERE statut = 'valide'")
        stats["ca_total"] = cur.fetchone()["total"]
        cur.execute("""
            SELECT COALESCE(SUM(montant_ttc), 0) as total FROM pos_tickets
            WHERE statut = 'valide' AND date(date_ticket, 'localtime') = date('now', 'localtime')
        """)
        stats["ca_jour"] = cur.fetchone()["total"]
        cur.execute("SELECT COUNT(*) as cnt FROM pos_tickets WHERE statut = 'valide' AND date(date_ticket, 'localtime') = date('now', 'localtime')")
        stats["tickets_jour"] = cur.fetchone()["cnt"]
        cur.execute("""
            SELECT v.nom, v.prenom, COUNT(*) as nb_ventes, COALESCE(SUM(t.montant_ttc), 0) as ca
            FROM pos_tickets t
            JOIN vendeurs v ON t.vendeur_id = v.id
            WHERE t.statut = 'valide' AND date(t.date_ticket, 'localtime') = date('now', 'localtime')
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


def _decrement_stock(lignes, done=None):
    """Decremente le stock, sans jamais perdre un deficit (pas de MAX(0))."""
    if done is None:
        done = []
    control = stock_control_enabled()
    for ligne in lignes:
        product_id = ligne.get("product_id")
        if not product_id:
            continue
        qte = float(ligne.get("quantite", 1))
        with get_cursor() as cur:
            if control:
                cur.execute(
                    "UPDATE products SET stock_reel = stock_reel - ?, updated_at = datetime('now') "
                    "WHERE id = ? AND stock_reel >= ?",
                    (qte, product_id, qte)
                )
                if cur.rowcount != 1:
                    raise ValueError("Stock insuffisant pour '{}'".format(ligne.get("designation", "")))
            else:
                cur.execute(
                    "UPDATE products SET stock_reel = stock_reel - ?, updated_at = datetime('now') WHERE id = ?",
                    (qte, product_id)
                )
        done.append(ligne)
    return done


def _restore_stock(lignes):
    for ligne in lignes:
        product_id = ligne.get("product_id")
        if not product_id:
            continue
        qte = float(ligne.get("quantite", 1))
        with get_cursor() as cur:
            cur.execute(
                "UPDATE products SET stock_reel = stock_reel + ?, updated_at = datetime('now') WHERE id = ?",
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


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+() .-]{6,20}$")
_NIU_RE = re.compile(r"^[A-Z0-9]{16,17}$")


def _normalise_contact_fields(data):
    """Valide les champs annuaire et retourne (valeurs, erreur)."""
    values = dict(data)
    for key in ("code", "nom", "email", "telephone", "niu", "rccm", "ville", "pays"):
        if key in values and values[key] is not None:
            values[key] = str(values[key]).strip()
    if values.get("niu"):
        values["niu"] = values["niu"].upper().replace(" ", "")
        if not _NIU_RE.match(values["niu"]):
            return None, "NIU invalide (16 a 17 caracteres alphanumeriques)"
    if values.get("email") and not _EMAIL_RE.match(values["email"]):
        return None, "Adresse email invalide"
    if values.get("telephone") and not _PHONE_RE.match(values["telephone"]):
        return None, "Numero de telephone invalide"
    if values.get("type") and values["type"] not in ("client", "fournisseur"):
        return None, "Type de contact invalide"
    return values, ""


def _hash_pin(pin):
    return hashlib.sha256(str(pin).encode("utf-8")).hexdigest() if pin else ""


def create_contact(data):
    data, err = _normalise_contact_fields(data or {})
    if err:
        return {"success": False, "error": err}
    code = data.get("code", "")
    nom = data.get("nom", "")
    if not code or not nom:
        return {"success": False, "error": "Code et nom requis"}

    niu = data.get("niu", "")
    if niu:
        with get_cursor() as cur:
            cur.execute("SELECT id FROM contacts WHERE niu = ?", (niu,))
            existing = cur.fetchone()
        if existing:
            return {
                "success": False,
                "error": "Un contact existe deja avec ce NIU",
                "field": "niu",
                "existing_value": niu,
                "existing_contact_id": existing["id"],
            }

    try:
        with get_cursor() as cur:
            cur.execute("""
                INSERT INTO contacts (
                    code, nom, type, email, telephone, niu, adresse, ville, pays,
                    rccm, is_taxable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                code, nom,
                data.get("type", "client"),
                data.get("email", ""),
                data.get("telephone", ""),
                niu,
                data.get("adresse", ""),
                data.get("ville", ""),
                data.get("pays", "CG"),
                data.get("rccm", ""),
                int(data.get("is_taxable", 1)),
            ))
            contact_id = cur.lastrowid
    except sqlite3.IntegrityError as e:
        logger.warning("Contact creation - doublon NIU (fallback IntegrityError): %s", e)
        return {
            "success": False,
            "error": "Un contact existe deja avec ce NIU",
            "field": "niu",
            "existing_value": niu,
        }

    logger.info("Contact cree: %s - %s (id=%d)", code, nom, contact_id)
    return {"success": True, "id": contact_id}
def _contacts_where(type_filter=None, search=None):
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
    return where_sql, params


def list_contacts(type_filter=None, search=None, limit=200, offset=0):
    where_sql, params = _contacts_where(type_filter, search)
    params.extend([limit, max(0, int(offset or 0))])
    with get_cursor() as cur:
        cur.execute(
            "SELECT * FROM contacts WHERE {} ORDER BY nom LIMIT ? OFFSET ?".format(where_sql),
            params
        )
        return rows_to_list(cur.fetchall())


def count_contacts(type_filter=None, search=None):
    where_sql, params = _contacts_where(type_filter, search)
    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) as cnt FROM contacts WHERE {}".format(where_sql), params)
        return cur.fetchone()["cnt"]


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
    code = str(data.get("code", "") or "").strip()
    nom = str(data.get("nom", "") or "").strip()
    if not code or not nom:
        return {"success": False, "error": "Code et nom requis"}
    if not re.match(r"^[A-Za-z0-9_-]{2,20}$", code):
        return {"success": False, "error": "Code vendeur invalide (2 a 20 caracteres)"}
    email = str(data.get("email", "") or "").strip()
    if email and not _EMAIL_RE.match(email):
        return {"success": False, "error": "Adresse email invalide"}
    pin = str(data.get("pin", "") or "")
    if pin and not re.match(r"^[0-9]{4,8}$", pin):
        return {"success": False, "error": "PIN invalide (4 a 8 chiffres)"}

    with get_cursor() as cur:
        cur.execute("SELECT id FROM vendeurs WHERE code = ?", (code,))
        if cur.fetchone():
            return {"success": False, "error": "Code vendeur deja utilise"}
        cur.execute("""
            INSERT INTO vendeurs (code, nom, prenom, email, telephone, role, pin)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            code, nom,
            data.get("prenom", ""),
            email,
            data.get("telephone", ""),
            data.get("role", "vendeur"),
            _hash_pin(pin)
        ))
        vendeur_id = cur.lastrowid

    logger.info("Vendeur cree: %s %s (id=%d)", data.get("prenom", ""), nom, vendeur_id)
    return {"success": True, "id": vendeur_id}


def update_vendeur(vendeur_id, data):
    with get_cursor() as cur:
        cur.execute("SELECT id FROM vendeurs WHERE id = ?", (vendeur_id,))
        if not cur.fetchone():
            return {"success": False, "error": "Vendeur non trouve"}

        email = str(data.get("email", "") or "").strip()
        if email and not _EMAIL_RE.match(email):
            return {"success": False, "error": "Adresse email invalide"}
        fields = []
        params = []
        for key in ("nom", "prenom", "email", "telephone", "role", "est_actif"):
            if key in data:
                fields.append("{} = ?".format(key))
                params.append(data[key])
        if data.get("pin"):
            pin = str(data["pin"])
            if not re.match(r"^[0-9]{4,8}$", pin):
                return {"success": False, "error": "PIN invalide (4 a 8 chiffres)"}
            fields.append("pin = ?")
            params.append(_hash_pin(pin))
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


def list_vendeurs(active_only=True, limit=200, offset=0):
    with get_cursor() as cur:
        where = "WHERE est_actif = 1" if active_only else ""
        cur.execute("SELECT * FROM vendeurs {} ORDER BY nom LIMIT ? OFFSET ?".format(where),
                    (limit, max(0, int(offset or 0))))
        return rows_to_list(cur.fetchall())


def count_vendeurs(active_only=False):
    with get_cursor() as cur:
        where = "WHERE est_actif = 1" if active_only else ""
        cur.execute("SELECT COUNT(*) AS cnt FROM vendeurs {}".format(where))
        return cur.fetchone()["cnt"]


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
                   COALESCE(SUM(CASE WHEN date(t.date_ticket, 'localtime') = date('now', 'localtime')
                                     THEN t.montant_ttc ELSE 0 END), 0) as ca_jour,
                   COALESCE(AVG(t.montant_ttc), 0) as panier_moyen
            FROM pos_tickets t
            JOIN vendeurs v ON t.vendeur_id = v.id
            WHERE {where}
            GROUP BY t.vendeur_id
            ORDER BY ca_total DESC
        """.format(where=where_sql), params)
        return rows_to_list(cur.fetchall())
