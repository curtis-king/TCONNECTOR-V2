"""Impression silencieuse + registre anti-redondance (SQLite)."""
import hashlib
import logging
import os
import subprocess
import time
from datetime import datetime

from app.storage.db import get_cursor

logger = logging.getLogger("t-connector.print_hijack.printer")

_SUMATRA_CANDIDATES = [
    r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
    r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "SumatraPDF", "SumatraPDF.exe"),
]


def compute_sha256(path, chunk=65536):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(chunk), b""):
                h.update(block)
        return h.hexdigest()
    except OSError as e:
        logger.warning("Hash impossible %s: %s", path, e)
        return None


def resolve_sumatra_path(configured):
    if configured and os.path.exists(configured):
        return configured
    for cand in _SUMATRA_CANDIDATES:
        if cand and os.path.exists(cand):
            return cand
    return None


def print_pdf(pdf_path, printer_name, sumatra_path, dry_run=False, timeout_s=60):
    """Imprime un PDF. Retourne (ok, erreur)."""
    if dry_run or not printer_name:
        logger.info("[dry_run] impression simulee: %s -> '%s'",
                    pdf_path, printer_name or "(aucune)")
        return True, ""
    exe = resolve_sumatra_path(sumatra_path)
    if not exe:
        return False, "SumatraPDF introuvable (configurer print_hijack.sumatra_path)"
    cmd = [exe, "-print-to", printer_name, "-silent", pdf_path]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return False, "timeout impression (> {}s)".format(timeout_s)
    except OSError as e:
        return False, "lancement SumatraPDF impossible: {}".format(e)
    if proc.returncode not in (0, None):
        err = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return False, "SumatraPDF code {}: {}".format(proc.returncode, err)
    # SumatraPDF -print-to rend la main avant la fin du spool : petite marge.
    time.sleep(1.0)
    return True, ""


def record_print(file_hash, numero, src_path, out_path, printer, status, error=""):
    try:
        with get_cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO processed_pdf_prints "
                "(file_hash, numero, src_path, out_path, printer, status, error, printed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                (file_hash, numero or "", src_path or "", out_path or "",
                 printer or "", status, (error or "")[:500]),
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("Enregistrement print impossible: %s", e)


def get_print_record(file_hash):
    try:
        with get_cursor() as cur:
            cur.execute(
                "SELECT * FROM processed_pdf_prints WHERE file_hash = ?",
                (file_hash,),
            )
            return cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning("Lecture registre print impossible: %s", e)
        return None


def numero_printed_recently(numero, window_min):
    if not numero or window_min <= 0:
        return False
    try:
        with get_cursor() as cur:
            cur.execute(
                "SELECT 1 FROM processed_pdf_prints "
                "WHERE numero = ? AND status = 'IMPRIME' "
                "AND printed_at >= datetime('now', ?) LIMIT 1",
                (numero, "-{} minutes".format(int(window_min))),
            )
            return cur.fetchone() is not None
    except Exception as e:  # noqa: BLE001
        logger.warning("Verification dedup numero impossible: %s", e)
        return False


def timestamp_tag():
    return datetime.now().strftime("%Y%m%d-%H%M%S")
