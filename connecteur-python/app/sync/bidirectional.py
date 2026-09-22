import logging
import threading
import time
from datetime import datetime
from app.storage.db import get_cursor, log_sync, row_to_dict, rows_to_list
from app.integration.sage.writer import read_invoices_from_sage, sync_sage_invoice_to_sqlite, write_contact_to_sage, write_invoice_to_sage

logger = logging.getLogger("t-connector.sync.bi")

_last_sync = None
_sync_lock = threading.Lock()
_sync_thread = None
_stop_event = threading.Event()
_sync_stats = {"last_sync": None, "pushed": 0, "pulled": 0, "conflicts": 0, "errors": 0}


def get_sync_stats():
    with _sync_lock:
        return dict(_sync_stats)


def push_invoice_to_sage(invoice_id):
    with get_cursor() as cur:
        cur.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,))
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
        cur.execute("SELECT * FROM contacts WHERE synced_sage = 0 ORDER BY code")
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
            SELECT * FROM invoices
            WHERE source IN ('web', 'pos') AND synced_sage = 0 AND statut != 'brouillon'
              AND (source != 'pos' OR (sfec_num_certif IS NOT NULL AND sfec_num_certif != ''))
            ORDER BY date_facture ASC
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


def pull_from_sage():
    sage_invoices = read_invoices_from_sage()
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

        cur.execute("""
            UPDATE invoices SET
                montant_ht = ?, montant_ttc = ?, montant_restant = ?,
                synced_sage = 1, updated_at = datetime('now')
            WHERE id = ?
        """, (
            sage_inv.get("montant_ht", 0),
            sage_inv.get("montant_ttc", 0),
            sage_inv.get("montant_restant", 0),
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
            SELECT * FROM invoices
            WHERE source = 'pos' AND synced_sage = 0
              AND (sfec_num_certif IS NULL OR sfec_num_certif = '' OR LENGTH(sfec_num_certif) > 40)
              AND (sfec_statut NOT IN ('CERTIFIE', 'DEJA_CERTIFIE') OR LENGTH(sfec_num_certif) > 40)
            ORDER BY date_facture ASC
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


def full_sync():
    logger.info("Demarrage sync bidirectionnelle...")
    cert_result = certify_pending_pos_invoices()
    contact_result = push_contacts_to_sage()
    push_result = push_to_sage()
    pull_result = pull_from_sage()

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
    while not _stop_event.is_set():
        try:
            full_sync()
        except Exception as e:
            logger.error("Erreur sync bidirectionnelle: %s", e)
            with _sync_lock:
                _sync_stats["errors"] += 1
        _stop_event.wait(interval)
