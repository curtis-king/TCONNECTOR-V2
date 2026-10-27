import logging
import threading
import time
from datetime import datetime
from app.storage.db import compute_cent_tax, get_cursor, log_sync, row_to_dict, rows_to_list
from app.integration.sage.database import _safe_float
from app.integration.sage.writer import read_invoices_from_sage, sync_sage_invoice_to_sqlite, write_contact_to_sage, write_invoice_to_sage

logger = logging.getLogger("t-connector.sync.bi")

# Ratios TTC/HT du modele fiscal client (Congo) : 0 %, 5 % + centime,
# 18 % + centime, 18 % + centime + 3 %. Un ratio hors de cette liste est
# un suspect : le miroir ne doit pas inventer de ventilation.
_MIRROR_RATIOS = (
    (1.0, 0.0),
    (1.0525, 0.05),
    (1.189, 0.18),
    (1.219, 0.18),
)

# Ratios sans centime additionnel rencontres en dehors du modele client
# (notamment la base de test BIJOU a 20 %). On les reconnait pour ne pas
# inventer de centimes, mais ils restent des suspects : le modele fiscal
# Congo attend 18 % / 5 % + 5 % de centimes.
_MIRROR_NO_CENTIME_RATIOS = (
    (1.05, 0.05),
    (1.18, 0.18),
    (1.20, 0.20),
)


def _mirror_tolerance():
    try:
        from app.config.manager import get_config
        cfg = get_config().get("validation", {}) or {}
        return abs(_safe_float(cfg.get("ttc_sage_tolerance"), 1.0)) or 1.0
    except Exception:
        return 1.0


def _match_mirror_ratio(ratio):
    if ratio is None:
        return None
    for ref, rate in _MIRROR_RATIOS:
        if abs(ratio - ref) <= 0.001:
            return rate
    return None


def _match_no_centime_ratio(ratio):
    if ratio is None:
        return None
    for ref, rate in _MIRROR_NO_CENTIME_RATIOS:
        if abs(ratio - ref) <= 0.001:
            return rate
    return None


def _mirror_fields(sage_inv):
    """Ventile la TVA du miroir local depuis la facture Sage.

    Le miroir sert au dashboard et a l'impression de repli : il doit porter
    la TVA reelle, pas le delta TTC-HT qui inclut centimes et taxes
    additionnelles. Regle de prudence : on n'ecrit une ventilation que si
    les lignes recoupent l'en-tete, ou si le ratio correspond a un modele
    connu. Sinon `montant_tva` reprend le delta historique et
    `_suspect_reason` explique pourquoi la ligne doit etre controlee.
    """
    lignes = sage_inv.get("lignes") or []
    montant_ht = round(_safe_float(sage_inv.get("montant_ht")), 2)
    montant_ttc = round(_safe_float(sage_inv.get("montant_ttc")), 2)
    tol = _mirror_tolerance()

    try:
        sage_type = int(float(sage_inv.get("do_type_raw",
                                          sage_inv.get("sage_type", 6)) or 6))
    except (TypeError, ValueError):
        sage_type = 6
    try:
        sage_domaine = int(float(sage_inv.get("sage_domaine", 0) or 0))
    except (TypeError, ValueError):
        sage_domaine = 0
    type_doc = "avoir" if (
        sage_inv.get("type_doc") == "avoir"
        or sage_type == 7
        or montant_ttc < 0
    ) else "vente"

    tva_t = tva_r = exempt = brut = 0.0
    tva_additionnelle = 0.0
    taux_additionnels = set()
    for ligne in lignes:
        taux = abs(_safe_float(ligne.get("taux_tva")))
        tva = _safe_float(ligne.get("montant_tva"))
        if taux >= 17.5:
            tva_t += tva
        elif 4.5 <= taux <= 5.5:
            # Seule la TVA a taux reduit (5 %) entre dans total_tax_r.
            # Un taux 3 % ou 0,25 % est une taxe additionnelle : il ne doit
            # jamais etre absorbe dans les centimes ni dans la TVA reduite.
            tva_r += tva
        elif taux < 0.01:
            exempt += _safe_float(ligne.get("montant_ht"))
        else:
            tva_additionnelle += tva
            taux_additionnels.add(round(taux, 2))
            # Une ligne de taxe additionnelle n'est pas un article : son HT
            # ne doit pas gonfler le brut ni la remise.
            continue
        brut += (_safe_float(ligne.get("quantite")) *
                 _safe_float(ligne.get("prix_unitaire")))

    remise = round(max(0.0, brut - montant_ht), 2) if lignes else 0.0
    result = {
        "montant_tva": round(montant_ttc - montant_ht, 2),
        "montant_ht_brut": round(brut if lignes else montant_ht, 2),
        "total_tax_t_amount": 0.0,
        "total_tax_r_amount": 0.0,
        "total_exempt_amount": 0.0,
        "discount_amount": remise,
        "total_line_discount_amount": remise,
        "additional_cent_tax": 0.0,
        "electronic_stamp_duty": 0.0,
        "type_doc": type_doc,
        "sage_domaine": sage_domaine,
        "sage_type": sage_type,
        "sage_piece": sage_inv.get("sage_piece") or sage_inv.get("numero", ""),
        "_reliable": False,
        "_suspect_reason": "",
    }

    ratio = round(montant_ttc / montant_ht, 6) if montant_ht else None
    known_rate = _match_mirror_ratio(ratio)

    if lignes:
        total_t = round(tva_t + tva_r, 2)
        centimes = compute_cent_tax(total_t)
        extra = round(
            montant_ttc - montant_ht - total_t - centimes -
            tva_additionnelle, 2)
        if abs(extra) <= tol:
            result.update({
                "montant_tva": total_t,
                "montant_ht_brut": round(brut, 2),
                "total_tax_t_amount": round(tva_t, 2),
                "total_tax_r_amount": round(tva_r, 2),
                "total_exempt_amount": round(exempt, 2),
                "discount_amount": remise,
                "total_line_discount_amount": remise,
                "additional_cent_tax": centimes,
                "_reliable": abs(tva_additionnelle) <= 0.01,
            })
            if abs(tva_additionnelle) > 0.01:
                result["_suspect_reason"] = (
                    "taxe(s) additionnelle(s) {} % non stockee(s): {} FCFA"
                    .format(",".join("%g" % t for t in sorted(taux_additionnels)),
                            round(tva_additionnelle, 2)))
            return result

        if known_rate is not None:
            # Lignes incoherentes mais ratio connu : la ventilation par taux
            # reste fiable, l'ecart eventuel (3 %) est signale sans etre
            # absorbe dans les centimes.
            tva = round(montant_ht * known_rate, 2)
            if abs(total_t - tva) > tol:
                # Les lignes contredisent le ratio (ex. lignes a 20 % avec
                # un en-tete a 1,189) : ne pas ecraser la TVA des lignes.
                result["montant_tva"] = round(montant_ttc - montant_ht, 2)
                result["_suspect_reason"] = (
                    "lignes (TVA {}) incompatibles avec le ratio {} - "
                    "ventilation non ecrite".format(total_t, ratio))
                return result
            centimes = compute_cent_tax(tva)
            extra = round(montant_ttc - montant_ht - tva - centimes, 2)
            result.update({
                "montant_tva": tva,
                "total_tax_t_amount": tva if known_rate >= 0.18 else 0.0,
                "total_tax_r_amount": tva if 0 < known_rate < 0.18 else 0.0,
                "total_exempt_amount": montant_ht if known_rate == 0 else 0.0,
                "additional_cent_tax": centimes,
                "_reliable": abs(extra) <= tol and abs(tva_additionnelle) <= 0.01,
            })
            if abs(extra) > tol:
                result["_suspect_reason"] = (
                    "taxe additionnelle non stockee: {} FCFA".format(extra))
            elif abs(tva_additionnelle) > 0.01:
                result["_suspect_reason"] = (
                    "taxe(s) additionnelle(s) {} % non stockee(s): {} FCFA"
                    .format(",".join("%g" % t for t in sorted(taux_additionnels)),
                            round(tva_additionnelle, 2)))
            return result

        # Modele sans centime (ex. BIJOU a 20 %, ou 18 % sans centime) :
        # les lignes donnent la TVA exacte, mais le TTC ne contient pas les
        # 5 % de centimes. On ecrit la TVA sans inventer de centime.
        delta = round(montant_ttc - montant_ht, 2)
        if abs(delta - total_t - tva_additionnelle) <= tol:
            result.update({
                "montant_tva": total_t,
                "montant_ht_brut": round(brut, 2),
                "total_tax_t_amount": round(tva_t, 2),
                "total_tax_r_amount": round(tva_r, 2),
                "total_exempt_amount": round(exempt, 2),
                "discount_amount": remise,
                "total_line_discount_amount": remise,
                "additional_cent_tax": 0.0,
                "_reliable": False,
            })
            motif = "centime additionnel absent"
            if abs(tva_additionnelle) > 0.01:
                motif += "; taxe(s) additionnelle(s) {} % non stockee(s): {} FCFA".format(
                    ",".join("%g" % t for t in sorted(taux_additionnels)),
                    round(tva_additionnelle, 2))
            result["_suspect_reason"] = motif
            return result

        result["_suspect_reason"] = (
            "lignes incoherentes avec l'en-tete (ecart {} FCFA) - "
            "ventilation non ecrite".format(
                round(montant_ttc - montant_ht - total_t - centimes -
                      tva_additionnelle, 2)))
        return result

    if known_rate is not None:
        tva = round(montant_ht * known_rate, 2)
        centimes = compute_cent_tax(tva)
        extra = round(montant_ttc - montant_ht - tva - centimes, 2)
        result.update({
            "montant_tva": tva,
            "total_tax_t_amount": tva if known_rate >= 0.18 else 0.0,
            "total_tax_r_amount": tva if 0 < known_rate < 0.18 else 0.0,
            "total_exempt_amount": montant_ht if known_rate == 0 else 0.0,
            "additional_cent_tax": centimes,
            "_reliable": abs(extra) <= tol,
        })
        if abs(extra) > tol:
            result["_suspect_reason"] = (
                "taxe additionnelle non stockee: {} FCFA".format(extra))
        return result

    # Ratio simple sans centime : TVA exacte, mais modele hors client.
    simple_rate = _match_no_centime_ratio(ratio)
    if simple_rate is not None:
        tva = round(montant_ht * simple_rate, 2)
        result.update({
            "montant_tva": tva,
            "total_tax_t_amount": tva if simple_rate >= 0.18 else 0.0,
            "total_tax_r_amount": tva if 0 < simple_rate < 0.18 else 0.0,
            "total_exempt_amount": montant_ht if simple_rate == 0 else 0.0,
            "additional_cent_tax": 0.0,
            "_reliable": False,
        })
        result["_suspect_reason"] = (
            "ratio {} sans centime additionnel - modele fiscal a confirmer"
            .format(ratio))
        return result

    result["_suspect_reason"] = (
        "ratio TTC/HT inconnu: {} - ventilation non ecrite".format(ratio))
    return result

_last_sync = None
_sync_lock = threading.Lock()
_sync_thread = None
_manual_sync_thread = None
_stop_event = threading.Event()
_sync_stats = {"last_sync": None, "pushed": 0, "pulled": 0, "conflicts": 0, "errors": 0}
_sync_active = False
_manual_sync_state = {
    "running": False,
    "mode": "",
    "started_at": None,
    "finished_at": None,
    "last_result": None,
    "last_error": None,
}


def is_sync_running():
    with _sync_lock:
        return bool(_sync_active or _manual_sync_state["running"])


def get_sync_stats():
    with _sync_lock:
        stats = dict(_sync_stats)
        stats.update({
            "running": _manual_sync_state["running"],
            "mode": _manual_sync_state["mode"],
            "started_at": _manual_sync_state["started_at"],
            "finished_at": _manual_sync_state["finished_at"],
            "last_result": _manual_sync_state["last_result"],
            "last_error": _manual_sync_state["last_error"],
        })
        return stats


def start_manual_sync(force_all=False):
    """Lance une sync bidirectionnelle en arriere-plan.

    Le thread HTTP doit repondre immediatement (202) : le pull complet de
    plusieurs milliers de factures ne doit jamais bloquer la requete.
    """
    global _manual_sync_thread, _sync_active

    with _sync_lock:
        if _sync_active or _manual_sync_state["running"]:
            return {"success": False, "error": "Une synchronisation est deja en cours"}
        mode = "full" if force_all else "window"
        _sync_active = True
        _manual_sync_state.update({
            "running": True,
            "mode": mode,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "finished_at": None,
            "last_result": None,
            "last_error": None,
        })
        _manual_sync_thread = threading.Thread(
            target=_run_manual_sync,
            args=(force_all,),
            daemon=True,
            name="bi-sync-manual",
        )
        _manual_sync_thread.start()

    return {"success": True, "started": True, "mode": mode}


def _run_manual_sync(force_all):
    global _sync_active
    try:
        result = full_sync(force_all=force_all)
        with _sync_lock:
            _manual_sync_state["last_result"] = result
    except Exception as e:
        logger.error("Erreur sync manuelle: %s", e)
        with _sync_lock:
            _manual_sync_state["last_error"] = str(e)[:300]
    finally:
        with _sync_lock:
            _sync_active = False
            _manual_sync_state["running"] = False
            _manual_sync_state["finished_at"] = datetime.utcnow().isoformat() + "Z"


def push_invoice_to_sage(invoice_id):
    with get_cursor() as cur:
        cur.execute("SELECT id, numero, date_facture, date_echeance, reference, contact_id, tiers_code, tiers_nom, tiers_niu, tiers_email, tiers_telephone, tiers_adresse, tiers_type, montant_ht, montant_tva, montant_ttc, montant_restant, statut, valide, type_doc, source, sfec_statut, sfec_num_certif, sfec_date_certif, sfec_id, synced_sage, sage_domaine, sage_type, sage_piece, vendeur_id, payment_method, devise, montant_ht_brut, total_tax_t_amount, total_tax_r_amount, total_exempt_amount, discount_amount, total_line_discount_amount, additional_cent_tax, electronic_stamp_duty, is_recipient_taxable, recipient_rccm, reference_invoice_id, payment_date, created_at, updated_at FROM invoices WHERE id = ?", (invoice_id,))
        row = cur.fetchone()
        if not row:
            return {"success": False, "error": "Facture non trouvee"}
        inv = dict(row)
        if inv.get("synced_sage"):
            return {"success": True, "already": True}
        if inv.get("statut") == "brouillon":
            return {"success": False, "error": "Facture brouillon: valider avant de pousser"}
        if inv.get("source") == "pos" and not (inv.get("sfec_num_certif") or ""):
            return {"success": False, "error": "Certification SFEC requise avant ecriture Sage"}
        cur.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY numero_ligne", (invoice_id,))
        inv["lignes"] = rows_to_list(cur.fetchall())

    result = write_invoice_to_sage(inv)
    if result.get("success"):
        with _sync_lock:
            _sync_stats["pushed"] += 1
    else:
        with _sync_lock:
            _sync_stats["errors"] += 1
    return result


def push_contacts_to_sage():
    """Pousse les contacts non synchronises vers Sage F_COMPTET.

    DOIT tourner avant push_to_sage() : le trigger TG_INS_CPTAF_DOCENTETE
    (erreur 82019) refuse toute piece dont le tiers n'existe pas en F_COMPTET.
    """
    with get_cursor() as cur:
        cur.execute("SELECT * FROM contacts WHERE synced_sage = 0 ORDER BY code LIMIT 200")
        pending = rows_to_list(cur.fetchall())

    if not pending:
        return {"pushed": 0, "errors": 0}

    pushed = 0
    errors = 0
    for contact in pending:
        result = write_contact_to_sage(contact)
        if result.get("success"):
            pushed += 1
        else:
            errors += 1
            logger.warning("Push contact Sage echoue: %s: %s", contact.get("code"), result.get("error"))

    with _sync_lock:
        _sync_stats["pushed_contacts"] = _sync_stats.get("pushed_contacts", 0) + pushed
        _sync_stats["errors"] += errors

    logger.info("Push contacts Sage: %d ecrits, %d erreurs", pushed, errors)
    return {"pushed": pushed, "errors": errors}


def push_to_sage():
    with get_cursor() as cur:
        cur.execute("""
            SELECT id, numero, date_facture, date_echeance, reference, contact_id, tiers_code, tiers_nom, tiers_niu, tiers_email, tiers_telephone, tiers_adresse, tiers_type, montant_ht, montant_tva, montant_ttc, montant_restant, statut, valide, type_doc, source, sfec_statut, sfec_num_certif, sfec_date_certif, sfec_id, synced_sage, sage_domaine, sage_type, sage_piece, vendeur_id, payment_method, devise, montant_ht_brut, total_tax_t_amount, total_tax_r_amount, total_exempt_amount, discount_amount, total_line_discount_amount, additional_cent_tax, electronic_stamp_duty, is_recipient_taxable, recipient_rccm, reference_invoice_id, payment_date, created_at, updated_at FROM invoices
            WHERE source IN ('web', 'pos') AND synced_sage = 0 AND statut != 'brouillon'
              AND (source != 'pos' OR (sfec_num_certif IS NOT NULL AND sfec_num_certif != ''))
            ORDER BY date_facture ASC LIMIT 100
        """)
        unsynced = rows_to_list(cur.fetchall())

    if not unsynced:
        return {"pushed": 0, "errors": 0}

    pushed = 0
    errors = 0

    for inv in unsynced:
        with get_cursor() as cur:
            cur.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY numero_ligne", (inv["id"],))
            inv["lignes"] = rows_to_list(cur.fetchall())

        result = write_invoice_to_sage(inv)
        if result.get("success"):
            pushed += 1
        else:
            errors += 1

    with _sync_lock:
        _sync_stats["pushed"] += pushed
        _sync_stats["errors"] += errors

    if pushed > 0 or errors > 0:
        logger.info("Push Sage: %d ecrites, %d erreurs", pushed, errors)

    return {"pushed": pushed, "errors": errors}


def pull_from_sage(force_all=False):
    from app.config.manager import get_config

    db_cfg = get_config().get("db", {}) or {}
    raw_days = db_cfg.get("sage_pull_days", 7)
    try:
        pull_days = int(raw_days)
    except (TypeError, ValueError):
        pull_days = 7
    if pull_days < 0:
        pull_days = 7

    logger.info(
        "Pull Sage: force_all=%s, fenetre=%s",
        force_all,
        "complete" if force_all else "{} jours".format(pull_days),
    )
    sage_invoices = read_invoices_from_sage(
        sage_pull_days=pull_days,
        force_all=force_all,
    )
    if not sage_invoices:
        return {"pulled": 0, "new": 0, "updated": 0}

    pulled = 0
    new = 0
    updated = 0

    for sage_inv in sage_invoices:
        numero = sage_inv.get("numero", "")
        if not numero:
            continue

        with get_cursor() as cur:
            cur.execute("SELECT id FROM invoices WHERE numero = ?", (numero,))
            existing = cur.fetchone()

        if existing:
            updated += 1
            _update_from_sage(existing["id"], sage_inv)
        else:
            result = sync_sage_invoice_to_sqlite(sage_inv)
            if result:
                new += 1
            pulled += 1

    with _sync_lock:
        _sync_stats["pulled"] += pulled

    logger.info("Pull Sage: %d nouvelles, %d mises a jour", new, updated)
    return {"pulled": pulled, "new": new, "updated": updated}


def _update_from_sage(invoice_id, sage_inv):
    sfec_changed = False
    sfec_fields = {}

    with get_cursor() as cur:
        cur.execute("SELECT sfec_statut, sfec_num_certif FROM invoices WHERE id = ?", (invoice_id,))
        current = cur.fetchone()

        if current:
            new_sfec = sage_inv.get("sfec_statut", "")
            if new_sfec and new_sfec != current["sfec_statut"]:
                sfec_changed = True
                sfec_fields = {
                    "sfec_statut": new_sfec,
                    "sfec_num_certif": sage_inv.get("sfec_num_certif", ""),
                    "sfec_signature": sage_inv.get("sfec_signature", ""),
                    "sfec_qr_code": sage_inv.get("sfec_qr_code", ""),
                }

        montant_ht = sage_inv.get("montant_ht", 0) or 0
        montant_ttc = sage_inv.get("montant_ttc", 0) or 0
        m = _mirror_fields(sage_inv)
        if m.get("_suspect_reason"):
            # Un pull peut concerner des milliers de lignes : le detail part
            # en debug, le CSV de backfill reste la source d'audit.
            logger.debug("Miroir fiscal non fiable pour %s: %s",
                         sage_inv.get("numero", ""), m["_suspect_reason"])

        cur.execute("""
            UPDATE invoices SET
                montant_ht = ?, montant_tva = ?, montant_ttc = ?,
                montant_restant = ?, montant_ht_brut = ?,
                total_tax_t_amount = ?, total_tax_r_amount = ?,
                total_exempt_amount = ?, discount_amount = ?,
                total_line_discount_amount = ?, additional_cent_tax = ?,
                electronic_stamp_duty = ?, type_doc = ?,
                sage_domaine = ?, sage_type = ?, sage_piece = ?,
                synced_sage = 1, updated_at = datetime('now')
            WHERE id = ?
        """, (
            montant_ht, m["montant_tva"], montant_ttc,
            sage_inv.get("montant_restant", 0), m["montant_ht_brut"],
            m["total_tax_t_amount"], m["total_tax_r_amount"],
            m["total_exempt_amount"], m["discount_amount"],
            m["total_line_discount_amount"], m["additional_cent_tax"],
            m["electronic_stamp_duty"], m["type_doc"],
            m["sage_domaine"], m["sage_type"], m["sage_piece"],
            invoice_id
        ))

        if sfec_changed:
            cur.execute("""
                UPDATE invoices SET
                    sfec_statut = ?, sfec_num_certif = ?,
                    sfec_signature = ?, sfec_qr_code = ?
                WHERE id = ?
            """, (
                sfec_fields["sfec_statut"], sfec_fields["sfec_num_certif"],
                sfec_fields["sfec_signature"], sfec_fields["sfec_qr_code"],
                invoice_id
            ))


def certify_pending_pos_invoices():
    """Re-tente la certification SFEC des factures POS pas encore certifiees
    (vrai rejet / panne lors de la validation du ticket)."""
    try:
        from app.config.manager import get_config
        from app.integration.sfec.endpoints import certify_sqlite_invoice
        from app.sync.connectivity import is_online
    except Exception as e:
        logger.error("certify_pending_pos_invoices: imports: %s", e)
        return {"certified": 0, "errors": 0}

    cfg = get_config().get("sfec", {})
    if not cfg.get("enabled") or not cfg.get("api_key"):
        return {"certified": 0, "errors": 0}
    if not is_online():
        return {"certified": 0, "errors": 0}

    with get_cursor() as cur:
        cur.execute("""
            SELECT id, numero, date_facture, date_echeance, reference, contact_id, tiers_code, tiers_nom, tiers_niu, tiers_email, tiers_telephone, tiers_adresse, tiers_type, montant_ht, montant_tva, montant_ttc, montant_restant, statut, valide, type_doc, source, sfec_statut, sfec_num_certif, sfec_date_certif, sfec_id, synced_sage, sage_domaine, sage_type, sage_piece, vendeur_id, payment_method, devise, montant_ht_brut, total_tax_t_amount, total_tax_r_amount, total_exempt_amount, discount_amount, total_line_discount_amount, additional_cent_tax, electronic_stamp_duty, is_recipient_taxable, recipient_rccm, reference_invoice_id, payment_date, created_at, updated_at FROM invoices
            WHERE source = 'pos' AND synced_sage = 0
              AND (sfec_num_certif IS NULL OR sfec_num_certif = '' OR LENGTH(sfec_num_certif) > 40)
              AND (sfec_statut NOT IN ('CERTIFIE', 'DEJA_CERTIFIE') OR LENGTH(sfec_num_certif) > 40)
            ORDER BY date_facture ASC LIMIT 50
        """)
        pending = rows_to_list(cur.fetchall())

    certified = 0
    errors = 0
    for inv in pending:
        with get_cursor() as cur:
            cur.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY numero_ligne", (inv["id"],))
            inv["lignes"] = rows_to_list(cur.fetchall())
        try:
            certify_sqlite_invoice(inv)
            certified += 1
        except Exception as e:
            errors += 1
            logger.error("Certification SFEC POS (retry) %s: %s", inv.get("numero"), str(e)[:200])

    if pending:
        logger.info("Certification SFEC POS en attente: %d certifiees, %d erreurs", certified, errors)
    with _sync_lock:
        _sync_stats["certified"] = _sync_stats.get("certified", 0) + certified
    return {"certified": certified, "errors": errors}


def full_sync(force_all=False):
    logger.info("Demarrage sync bidirectionnelle...")
    cert_result = certify_pending_pos_invoices()
    contact_result = push_contacts_to_sage()
    push_result = push_to_sage()
    pull_result = pull_from_sage(force_all=force_all)

    with _sync_lock:
        _sync_stats["last_sync"] = datetime.utcnow().isoformat() + "Z"

    total = {
        "pushed": push_result["pushed"],
        "pushed_contacts": contact_result["pushed"],
        "pull_new": pull_result.get("new", 0),
        "pull_updated": pull_result.get("updated", 0),
        "certified": cert_result.get("certified", 0),
        "errors": push_result["errors"] + cert_result.get("errors", 0) + contact_result["errors"],
    }
    logger.info("Sync terminee: contacts=%d, push=%d, pull_new=%d, pull_updated=%d, certif=%d, errors=%d",
                total["pushed_contacts"], total["pushed"], total["pull_new"], total["pull_updated"],
                total["certified"], total["errors"])
    return total


def start_bi_sync(interval=60):
    global _sync_thread
    if _sync_thread and _sync_thread.is_alive():
        return

    _stop_event.clear()
    _sync_thread = threading.Thread(target=_bi_sync_loop, args=(interval,), daemon=True, name="bi-sync")
    _sync_thread.start()
    logger.info("Sync bidirectionnelle demarree (intervalle: %ds)", interval)


def stop_bi_sync():
    _stop_event.set()
    if _sync_thread and _sync_thread.is_alive():
        _sync_thread.join(timeout=5)


def _bi_sync_loop(interval):
    global _sync_active
    while not _stop_event.is_set():
        with _sync_lock:
            should_run = not (_sync_active or _manual_sync_state["running"])
            if should_run:
                _sync_active = True
        if should_run:
            try:
                full_sync()
            except Exception as e:
                logger.error("Erreur sync bidirectionnelle: %s", e)
                with _sync_lock:
                    _sync_stats["errors"] += 1
            finally:
                with _sync_lock:
                    _sync_active = False
        _stop_event.wait(interval)
