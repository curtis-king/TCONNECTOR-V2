"""Façade de compatibilité de l'adaptateur Sage 100.

Les implémentations vivent désormais dans des modules dédiés :

- `connection` : pool pyodbc, curseurs, test de connexion ;
- `schema` : découverte des tables/colonnes, `get_table_map`, colonnes SFEC ;
- `queries.*` : lectures (factures, lignes, tiers, articles, références) ;
- `values` : conversions de valeurs (devises, nombres) ;
- `errors` / `sql` : taxonomie d'erreurs et validation d'identifiants.

Ce module ne garde que les écritures et marquages SFEC (qui partagent le
verrou `_db_lock` et les helpers d'identifiants) et ré-exporte le reste pour
ne casser aucun import existant.
"""
import logging
from contextlib import contextmanager  # noqa: F401  (compat)

from app.integration.sage.connection import (  # noqa: F401
    _db_lock, _ensure_pool, _pool, _pool_queue, _pool_size,
    _build_conn_string, _checkout, _connect, _connect_with_retry,
    _is_transient, _server_clause, close_pool, get_cursor, get_pool,
    ping_database,
)
from app.integration.sage.errors import (  # noqa: F401
    PermanentSfecWriteError, SageSchemaError,
)
from app.integration.sage.schema import (  # noqa: F401
    _col_expr, _has_col, _refresh_sfec_columns, _TABLE_COLUMNS, _TABLE_MAP,
    credit_doc_types, discover_tables, ensure_sfec_columns,
    get_domain_config, get_table_map, list_all_tables, sales_doc_types,
)
from app.integration.sage.queries.invoices import (  # noqa: F401
    fetch_purchase_invoices, fetch_sales_invoices,
)
from app.integration.sage.queries.document_lines import (  # noqa: F401
    fetch_doc_lines, fetch_doc_lines_batch,
)
from app.integration.sage.queries.certifications import (  # noqa: F401
    fetch_certified_invoices,
)
from app.integration.sage.queries.contacts import (  # noqa: F401
    fetch_contact_by_code, fetch_contacts,
)
from app.integration.sage.queries.articles import fetch_articles  # noqa: F401
from app.integration.sage.queries.references import (  # noqa: F401
    fetch_ledger_accounts, fetch_tax_rates,
)
from app.integration.sage.sql import quote_ident
from app.integration.sage.values import (  # noqa: F401
    _map_devise, _safe_float, get_devise_map,
)

logger = logging.getLogger("t-connector.db")


def _parse_invoice_id(invoice_id):
    parts = invoice_id.split("-")
    if len(parts) < 3:
        raise PermanentSfecWriteError(
            "ID facture invalide: {}".format(invoice_id))
    try:
        return int(parts[0]), int(parts[1]), "-".join(parts[2:])
    except ValueError:
        raise PermanentSfecWriteError(
            "ID facture invalide: {}".format(invoice_id))

def write_sfec_to_invoice(invoice_id, certification_number, signature, qr_code, certification_date):
    _refresh_sfec_columns()
    t = get_table_map()
    raw_tbl = t.get("documents", "")
    if not raw_tbl:
        raise PermanentSfecWriteError("Table documents introuvable")
    try:
        tbl = quote_ident(raw_tbl)
    except SageSchemaError as e:
        raise PermanentSfecWriteError(str(e))

    domaine, type_doc, piece = _parse_invoice_id(invoice_id)

    cert_num_str = str(certification_number)[:60] if certification_number else ""
    sig_str = str(signature)[:256] if signature else ""
    qr_str = str(qr_code) if qr_code else ""
    date_str = str(certification_date)[:30] if certification_date else None

    cert_num50 = cert_num_str[:50]
    cert_num69 = cert_num_str[:69]
    sql = """
        UPDATE {} SET
            SFEC_NUM_CERTIF = ?,
            SFEC_NUM_CERTIF1 = ?,
            SFEC_NUM_CERTIF2 = ?,
            SFEC_SIGNATURE = ?,
            SFEC_QR_CODE = ?,
            SFEC_DATE_CERTIF = ?,
            SFEC_DATE_CERTIF1 = ?,
            SFEC_STATUT = 'CERTIFIE'
        WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
    """.format(tbl)
    params = [cert_num50, cert_num50, cert_num69, sig_str, qr_str, date_str, date_str, domaine, type_doc, piece]

    with get_cursor() as cur:
        cur.execute(sql, params)
        if cur.rowcount == 0:
            raise PermanentSfecWriteError(
                "WRITE FAILED: 0 ligne pour {} (domaine={}, type={}, piece={})".format(
                    invoice_id, domaine, type_doc, piece))

    logger.info("Ecriture SFEC OK: %s (certif=%s)", invoice_id, cert_num_str[:30])
    try:
        from app.storage.db import clear_sfec_anomaly
        clear_sfec_anomaly(piece)
    except Exception as e:
        logger.debug("Suppression anomalie SQLite echouee pour %s: %s", piece, e)
    return dict(invoice_id=invoice_id, certification_number=cert_num_str,
                signature=sig_str, qr_code=qr_str, certification_date=date_str)

def reset_stuck_en_cours(reset_errors=False):
    """Debloque les certifications interrompues.

    - `EN_COURS` : un process precedent a crashé pendant la certification ;
      ce statut doit etre libere, sinon la facture reste bloquee a vie.
    - `ERREUR` : NE DOIT PAS etre remis a NULL en fonctionnement normal.
      Sinon chaque passe rejoue les memes factures a l'infini (audit
      2026-10-21 : 14 866 rejets pour 191 factures, 64 passes en 1 h 30).
      `reset_errors=True` est reserve a une operation d'upgrade explicite
      (script ponctuel), jamais au cycle automatique.
    """
    t = get_table_map()
    if not t["documents"]:
        return
    statuts = ("EN_COURS", "ERREUR") if reset_errors else ("EN_COURS",)
    placeholders = ", ".join(["?"] * len(statuts))
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = NULL
                WHERE SFEC_STATUT IN ({})
                  AND DO_Domaine = 0
                  AND SFEC_DATE_CERTIF IS NULL
                  AND (SFEC_NUM_CERTIF IS NULL OR SFEC_NUM_CERTIF = '')
            """.format(quote_ident(t["documents"]), placeholders), statuts)
            if cur.rowcount > 0:
                logger.info("Statuts %s remis a NULL: %d factures",
                            "EN_COURS/ERREUR" if reset_errors else "EN_COURS",
                            cur.rowcount)
    except Exception:
        pass

def reset_sfec_monitor(piece):
    """Autorise un nouveau passage apres un lien manuel avoir -> parent.

    Ne touche qu'au statut A_SURVEILLER (jamais ERREUR ni CERTIFIE) et
    uniquement pour la piece concernee, tous types confondus (6, 7 et 8).
    """
    t = get_table_map()
    if not t["documents"] or not piece:
        return 0
    doc_types = sales_doc_types()
    if not doc_types:
        return 0
    types_in = ", ".join(str(t) for t in doc_types)
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = NULL
                WHERE DO_Domaine = 0 AND DO_Type IN ({}) AND DO_Piece = ?
                  AND SFEC_STATUT = 'A_SURVEILLER'
                  AND SFEC_DATE_CERTIF IS NULL
                  AND (SFEC_NUM_CERTIF IS NULL OR SFEC_NUM_CERTIF = '')
            """.format(quote_ident(t["documents"]), types_in), piece)
            return cur.rowcount or 0
    except Exception as e:
        logger.debug("reset_sfec_monitor echoue pour %s: %s", piece, e)
        return 0

def mark_certifying(invoice_id):
    t = get_table_map()
    parts = invoice_id.split("-")
    if len(parts) < 3:
        return
    domaine = int(parts[0])
    type_doc = int(parts[1])
    piece = "-".join(parts[2:])
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = 'EN_COURS'
                WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
                  AND (SFEC_STATUT IS NULL OR SFEC_STATUT <> 'CERTIFIE')
            """.format(quote_ident(t["documents"])), domaine, type_doc, piece)
    except Exception as e:
        logger.debug("mark_certifying echoue pour %s: %s", invoice_id, e)

def mark_to_monitor(invoice_id, reason=""):
    parts = str(invoice_id or "").split("-")
    piece = "-".join(parts[2:]) if len(parts) >= 3 else str(invoice_id or "")
    motif = reason or "verification requise"

    # Toujours tracer localement, meme si Sage est indisponible : la page
    # /pending doit conserver le motif pour l'operateur.
    try:
        from app.storage.db import record_sfec_anomaly
        record_sfec_anomaly(piece, motif)
    except Exception as e:
        logger.debug("Enregistrement anomalie SQLite echoue pour %s: %s", piece, e)

    t = get_table_map()
    if len(parts) < 3 or not t.get("documents"):
        return
    domaine = int(parts[0])
    type_doc = int(parts[1])
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = 'A_SURVEILLER'
                WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
                  AND (SFEC_STATUT IS NULL OR SFEC_STATUT <> 'CERTIFIE')
            """.format(quote_ident(t["documents"])), domaine, type_doc, piece)
            if cur.rowcount > 0:
                logger.warning("Facture mise A_SURVEILLER: %s - %s",
                               invoice_id, motif)
    except Exception as e:
        logger.debug("mark_to_monitor echoue pour %s: %s", invoice_id, e)

def mark_certified(invoice_id, note="CERTIFIE"):
    t = get_table_map()
    parts = invoice_id.split("-")
    if len(parts) < 3:
        return
    domaine = int(parts[0])
    type_doc = int(parts[1])
    piece = "-".join(parts[2:])
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = ?
                WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
                  AND (SFEC_STATUT IS NULL OR SFEC_STATUT NOT IN ('CERTIFIE', ?))
            """.format(quote_ident(t["documents"])), note, domaine, type_doc, piece, note)
        logger.info("Facture marquee %s: %s", note, invoice_id)
        if note == "CERTIFIE":
            try:
                from app.storage.db import clear_sfec_anomaly
                clear_sfec_anomaly(piece)
            except Exception as exc:
                logger.debug("Suppression anomalie SQLite echouee pour %s: %s", piece, exc)
    except Exception as e:
        logger.debug("mark_certified echoue pour %s: %s", invoice_id, e)

def mark_certification_failed(invoice_id, error_msg):
    t = get_table_map()
    parts = invoice_id.split("-")
    if len(parts) < 3:
        return
    domaine = int(parts[0])
    type_doc = int(parts[1])
    piece = "-".join(parts[2:])
    try:
        with get_cursor() as cur:
            cur.execute("""
                UPDATE {} SET SFEC_STATUT = 'ERREUR'
                WHERE DO_Domaine = ? AND DO_Type = ? AND DO_Piece = ?
                  AND (SFEC_STATUT IS NULL OR SFEC_STATUT NOT IN ('CERTIFIE', 'DEJA_CERTIFIE'))
            """.format(quote_ident(t["documents"])), domaine, type_doc, piece)
    except Exception as e:
        logger.debug("mark_certification_failed echoue pour %s: %s", invoice_id, e)
    logger.warning("Facture marquee ERREUR certification SFEC: %s - %s", invoice_id, error_msg)
