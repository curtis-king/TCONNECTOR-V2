"""Mention d'exemplaire des factures : "Original" a la 1re impression, "Duplicata" ensuite.

Le journal invoice_print_log (SQLite) compte les impressions par numero de
facture, tous canaux confondus : page d'impression navigateur, PDF
telecharge, impression automatique (print_hijack). Seules les factures
certifiees SFEC sont concernees : une copie non certifiee ne consomme pas
l'Original.
"""
import logging
import threading

from app.storage.db import get_cursor

logger = logging.getLogger("t-connector.print_copies")

ORIGINAL = "Original"
DUPLICATA = "Duplicata"

_lock = threading.Lock()


def print_count(numero):
    """Nombre d'impressions deja enregistrees pour la facture."""
    if not numero:
        return 0
    try:
        with get_cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM invoice_print_log WHERE numero = ?", (numero,))
            return int(cur.fetchone()[0] or 0)
    except Exception as e:
        logger.warning("Lecture journal impressions %s impossible: %s", numero, e)
        return 0


def next_mention(numero):
    """Mention de la prochaine impression, sans l'enregistrer."""
    return ORIGINAL if print_count(numero) == 0 else DUPLICATA


def register_print(numero, canal):
    """Enregistre une impression et renvoie la mention qu'elle porte."""
    if not numero:
        return ""
    with _lock:
        mention = next_mention(numero)
        try:
            with get_cursor() as cur:
                cur.execute(
                    "INSERT INTO invoice_print_log (numero, canal, mention) VALUES (?, ?, ?)",
                    (numero, canal, mention),
                )
        except Exception as e:
            logger.warning("Enregistrement impression %s impossible: %s", numero, e)
    logger.info("Impression facture %s (%s) : %s", numero, canal, mention)
    return mention
