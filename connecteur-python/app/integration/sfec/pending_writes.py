"""File d'attente des certifications SFEC a ecrire dans Sage.

Une certification obtenue de SFEC ne doit jamais etre perdue parce que
Sage est verrouille ou momentanement injoignable (audit 2026-10-21 :
22 x 409, 8 x "Cet element est en cours d'utilisation"). On la stocke
dans SQLite et on la rejoue a chaque cycle ; l'ecriture Sage est
idempotente puisqu'elle ne touche que les colonnes SFEC.

Deux natures d'echec sont distinguees :

- transitoire (verrou Sage, coupure reseau) : la ligne reste rejouable
  sans limite, une certification valide ne doit jamais etre abandonnee ;
- definitif (`PermanentSfecWriteError` : facture absente, identifiant
  invalide) : la ligne est marquee `blocked`, conservee pour l'audit,
  mais n'est plus rejouee. Sans cela un identifiant mort boucle a vie.
"""
import logging

from app.integration.sage.database import (
    write_sfec_to_invoice, mark_to_monitor, PermanentSfecWriteError,
)
from app.storage.db import get_cursor

logger = logging.getLogger("t-connector.sfec.pending")

MAX_ATTEMPTS = 10

# Erreurs qui condamnent tout le lot : inutile de tenter les 19 autres
# lignes, elles echoueront de la meme facon (Sage injoignable).
_CONNECTION_ERROR_MARKERS = (
    "communication link failure", "login timeout", "hyt00", "08001",
    "server is unavailable", "configuration base de donnees incomplete",
)


def _is_connection_error(exc):
    msg = str(exc).lower()
    return any(marker in msg for marker in _CONNECTION_ERROR_MARKERS)


def _ensure_columns(cur):
    """Ajoute `blocked` aux bases creees avant le correctif 2026-11."""
    cols = {
        row["name"]
        for row in cur.execute("PRAGMA table_info(sfec_pending_writes)").fetchall()
    }
    if "blocked" not in cols:
        cur.execute(
            "ALTER TABLE sfec_pending_writes "
            "ADD COLUMN blocked INTEGER NOT NULL DEFAULT 0"
        )


def init_pending_table():
    """Cree la file si besoin (idempotent, appele au demarrage et au flush)."""
    with get_cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sfec_pending_writes (
                invoice_id TEXT PRIMARY KEY,
                certification_number TEXT,
                signature TEXT,
                qr_code TEXT,
                certification_date TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT DEFAULT '',
                blocked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sfec_pending_writes_created
            ON sfec_pending_writes(created_at)
        """)
        _ensure_columns(cur)


def enqueue(invoice_id, certification_number, signature, qr_code,
            certification_date, error=""):
    """Memorise ou met a jour une certification en attente d'ecriture Sage.

    `attempts` n'est volontairement pas remis a zero lors d'un conflit :
    il sert au suivi d'exploitation. Une certification deja obtenue n'est
    jamais abandonnee : seule l'ecriture Sage est bloquee. En revanche une
    nouvelle certification rouvre une ligne precedemment bloquee.
    """
    invoice_id = str(invoice_id or "").strip()
    if not invoice_id:
        raise ValueError("invoice_id obligatoire")
    init_pending_table()
    with get_cursor() as cur:
        cur.execute("""
            INSERT INTO sfec_pending_writes (
                invoice_id, certification_number, signature, qr_code,
                certification_date, last_error
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(invoice_id) DO UPDATE SET
                certification_number = excluded.certification_number,
                signature = excluded.signature,
                qr_code = excluded.qr_code,
                certification_date = excluded.certification_date,
                last_error = excluded.last_error,
                blocked = 0
        """, (
            invoice_id,
            str(certification_number or ""),
            str(signature or ""),
            str(qr_code or ""),
            str(certification_date or ""),
            str(error or "")[:300],
        ))
    logger.info("Certification SFEC mise en file pour %s", invoice_id)


def flush(limit=20, on_success=None):
    """Rejoue les ecritures Sage en attente.

    Retourne le nombre d'ecritures reussies. Une certification SFEC valide
    reste en file meme apres MAX_ATTEMPTS : un verrou Sage ne doit jamais
    faire perdre la certification ni la transformer en anomalie. Une erreur
    definitive marque la ligne `blocked` (plus de rejeu, ligne conservee).
    `on_success(invoice_id, certification_number)` permet au moteur de
    remettre son cache a jour sans importer `app.sync.engine` ici.
    """
    init_pending_table()
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 20

    with get_cursor() as cur:
        cur.execute("""
            SELECT * FROM sfec_pending_writes
            WHERE COALESCE(blocked, 0) = 0
            ORDER BY attempts, created_at, invoice_id
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()

    done = 0
    for index, row in enumerate(rows):
        invoice_id = row["invoice_id"]
        try:
            write_sfec_to_invoice(
                invoice_id=invoice_id,
                certification_number=row["certification_number"],
                signature=row["signature"],
                qr_code=row["qr_code"],
                certification_date=row["certification_date"],
            )
        except PermanentSfecWriteError as e:
            attempts = (row["attempts"] or 0) + 1
            with get_cursor() as cur:
                cur.execute("""
                    UPDATE sfec_pending_writes
                    SET attempts = ?, last_error = ?, blocked = 1
                    WHERE invoice_id = ?
                """, (attempts, str(e)[:300], invoice_id))
            logger.error(
                "Ecriture SFEC abandonnee pour %s (non rejouable, ligne "
                "conservee): %s", invoice_id, e)
            try:
                mark_to_monitor(
                    invoice_id,
                    "Ecriture SFEC impossible: {}".format(str(e)[:180]),
                )
            except Exception as me:  # noqa: BLE001
                logger.debug("Marquage anomalie SFEC %s echoue: %s",
                             invoice_id, me)
            continue
        except Exception as e:  # noqa: BLE001
            attempts = (row["attempts"] or 0) + 1
            with get_cursor() as cur:
                cur.execute("""
                    UPDATE sfec_pending_writes
                    SET attempts = ?, last_error = ?
                    WHERE invoice_id = ?
                """, (attempts, str(e)[:300], invoice_id))
            if attempts == MAX_ATTEMPTS:
                logger.error(
                    "Ecriture SFEC toujours en attente pour %s apres %d "
                    "tentatives ; certification conservee: %s",
                    invoice_id, attempts, e,
                )
            elif attempts > MAX_ATTEMPTS and attempts % MAX_ATTEMPTS == 0:
                logger.error(
                    "Ecriture SFEC toujours en attente pour %s (%d tentatives) "
                    "; certification conservee: %s",
                    invoice_id, attempts, e,
                )
            else:
                logger.warning("Ecriture SFEC differee %s (%d/%d): %s",
                               invoice_id, attempts, MAX_ATTEMPTS, e)
            if _is_connection_error(e):
                # Sage injoignable : les lignes suivantes echoueraient a
                # l'identique. On reprendra au prochain cycle.
                logger.warning(
                    "Sage injoignable - rejeu des ecritures SFEC interrompu "
                    "pour ce cycle (%d ligne(s) restante(s))",
                    len(rows) - index - 1,
                )
                break
            continue

        with get_cursor() as cur:
            cur.execute("DELETE FROM sfec_pending_writes WHERE invoice_id = ?",
                        (invoice_id,))
        done += 1
        if on_success is not None:
            try:
                on_success(invoice_id, row["certification_number"])
            except Exception as e:  # noqa: BLE001
                logger.debug("Callback ecriture SFEC %s echoue: %s", invoice_id, e)

    if done:
        logger.info("Ecritures SFEC en attente rejouees: %d", done)
    return done


def pending_stats():
    """Compteurs d'exploitation de la file (total, bloquees, rejouables)."""
    init_pending_table()
    with get_cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN COALESCE(blocked, 0) = 1 THEN 1 ELSE 0 END)
                       AS blocked
            FROM sfec_pending_writes
        """)
        row = cur.fetchone()
    total = int(row["total"] or 0) if row else 0
    blocked = int(row["blocked"] or 0) if row else 0
    return {
        "total": total,
        "blocked": blocked,
        "retrying": max(0, total - blocked),
    }


def pending_count():
    """Nombre de certifications encore en attente (diagnostic/exploitation)."""
    return pending_stats()["total"]
