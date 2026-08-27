import logging
import threading
import time
from datetime import datetime
from sqlite_db import get_cursor, log_sync, row_to_dict, rows_to_list
from sage_writer import read_invoices_from_sage, sync_sage_invoice_to_sqlite, write_invoice_to_sage

logger = logging.getLogger("t-connector.sync.bi")

_last_sync = None
_sync_lock = threading.Lock()
_sync_thread = None
_stop_event = threading.Event()
_sync_stats = {"last_sync": None, "pushed": 0, "pulled": 0, "conflicts": 0, "errors": 0}


def get_sync_stats():
    with _sync_lock:
        return dict(_sync_stats)


def push_to_sage():
    with get_cursor() as cur:
        cur.execute("""
            SELECT * FROM invoices
            WHERE source = 'web' AND synced_sage = 0 AND statut != 'brouillon'
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


def full_sync():
    logger.info("Demarrage sync bidirectionnelle...")
    push_result = push_to_sage()
    pull_result = pull_from_sage()

    with _sync_lock:
        _sync_stats["last_sync"] = datetime.utcnow().isoformat() + "Z"

    total = {
        "pushed": push_result["pushed"],
        "pull_new": pull_result.get("new", 0),
        "pull_updated": pull_result.get("updated", 0),
        "errors": push_result["errors"],
    }
    logger.info("Sync terminee: push=%d, pull_new=%d, pull_updated=%d, errors=%d",
                total["pushed"], total["pull_new"], total["pull_updated"], total["errors"])
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
