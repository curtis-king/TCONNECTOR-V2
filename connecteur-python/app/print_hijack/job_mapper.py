"""Identification de la facture à partir d'un PDF déposé.

Cascade : (1) motif dans le nom du fichier, (2) extraction du texte de la
1re page (pypdf). Échec -> quarantaine, jamais d'impression au hasard.
"""
import logging
import os
import re

from app.domain.invoices import get_invoice_by_numero

logger = logging.getLogger("t-connector.print_hijack.mapper")

_NUM_RE = re.compile(r"[A-Za-z]{2,4}[-_ ]?\d{2,}")
_TOKEN_RE = re.compile(r"[A-Za-z]{2,4}[-_ ]?\d{2,}|[A-Za-z]{2,4}\d{2,}")


def candidates_from_text(text, limit=8):
    out = []
    seen = set()

    def _add(val):
        v = (val or "").strip()
        if not v:
            return
        for variant in (v, v.upper(), re.sub(r"[-_ ]", "", v).upper()):
            if variant and variant not in seen:
                seen.add(variant)
                out.append(variant)

    for m in _TOKEN_RE.finditer(text or ""):
        _add(m.group(0))
        if len(out) >= limit:
            break
    return out


def candidates_from_filename(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return candidates_from_text(stem)


def candidates_from_pdf(path):
    try:
        from pypdf import PdfReader
    except Exception:  # noqa: BLE001
        return []
    try:
        reader = PdfReader(path)
        if not reader.pages:
            return []
        text = reader.pages[0].extract_text() or ""
    except Exception as e:  # noqa: BLE001
        logger.debug("Extraction texte PDF impossible (%s): %s", path, e)
        return []
    return candidates_from_text(text[:4000])


def _enrich_sage_lines(inv):
    """Lignes Sage pour une facture source='sage' (non stockees en SQLite)."""
    if not inv or inv.get("lignes") or inv.get("source") != "sage":
        return inv
    try:
        from app.integration.sage.database import fetch_doc_lines
        lignes = fetch_doc_lines(
            int(inv.get("sage_domaine") or 0),
            int(inv.get("sage_type") or 6),
            inv.get("numero", ""),
        )
        if lignes:
            inv["lignes"] = lignes
    except Exception as e:  # noqa: BLE001
        logger.debug("Lignes Sage indisponibles pour %s: %s", inv.get("numero"), e)
    return inv


def _lookup(candidates):
    for cand in candidates:
        try:
            inv = get_invoice_by_numero(cand)
        except Exception as e:  # noqa: BLE001
            logger.warning("Lookup facture '%s' impossible: %s", cand, e)
            continue
        if inv:
            return _enrich_sage_lines(inv), cand
    return None, ""


def map_pdf_to_invoice(path):
    """Retourne (invoice_dict, raison). invoice_dict=None si non identifiée."""
    if not os.path.exists(path):
        return None, "fichier absent"

    inv, used = _lookup(candidates_from_filename(path))
    if inv:
        logger.info("PDF %s -> facture %s (nom fichier, '%s')",
                    os.path.basename(path), inv.get("numero"), used)
        return inv, ""

    inv, used = _lookup(candidates_from_pdf(path))
    if inv:
        logger.info("PDF %s -> facture %s (texte PDF, '%s')",
                    os.path.basename(path), inv.get("numero"), used)
        return inv, ""

    return None, "aucun numéro de facture reconnu"
