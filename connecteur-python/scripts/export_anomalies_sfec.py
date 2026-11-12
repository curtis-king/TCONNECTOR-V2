"""Export CSV des factures SFEC non reconstructibles.

Le script relit les factures de vente dans Sage, reconstruit le payload SFEC
avec les regles du correctif 01 (centimes + taxes additionnelles), puis
exporte uniquement celles qui restent en anomalie :

  - TTC = HT (aucune TVA facturee) ;
  - TTC Sage non reconstructible (taxe inconnue, mix de taux, etc.) ;
  - HT absent/nul.

Les avoirs et les montants negatifs sont exclus : ils relevent de la
proposition 05 (rattachement a la facture d'origine).

Usage (depuis connecteur-python, avec le venv) :
    python scripts/export_anomalies_sfec.py
    python scripts/export_anomalies_sfec.py --out /tmp/anomalies.csv --limit 5000

Le fichier est encode en UTF-8 avec BOM pour Excel.
"""
import argparse
import csv
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import logging

from app.config.manager import load_config
from app.core.logging_setup import setup_logging
from app.integration.sage.database import fetch_doc_lines, fetch_sales_invoices
from app.integration.sfec.endpoints import (
    check_sage_totals,
    preview_sfec_payload,
    validate_sfec_payload,
)

logger = logging.getLogger("t-connector.export_anomalies_sfec")

FIELDS = [
    "numero",
    "date",
    "tiers",
    "montant_ht",
    "montant_ttc",
    "ttc_calcule",
    "ecart",
    "ratio_ttc_ht",
    "taxes_additionnelles",
    "motif",
]


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_cell(value):
    """Neutralise une eventuelle formule Excel en tete de cellule."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _lines_from_sage(invoice):
    """Charge les lignes d'une facture a partir de son identifiant Sage."""
    parts = str(invoice.get("id") or "").split("-")
    if len(parts) < 3:
        return []
    try:
        domaine = int(parts[0])
        type_doc = int(parts[1])
    except (TypeError, ValueError):
        return []
    piece = "-".join(parts[2:])
    try:
        return fetch_doc_lines(domaine, type_doc, piece) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Lignes illisibles pour %s: %s", invoice.get("numero"), exc)
        return []


def build_rows(invoices, line_fetcher=None):
    """Construit les lignes d'anomalies a partir d'une liste de factures.

    `line_fetcher`, si fourni, recoit la facture et renvoie ses lignes. Il
    permet de tester la fonction sans SQL Server.
    """
    rows = []
    for invoice in invoices or []:
        if invoice.get("type_doc") == "avoir":
            continue
        if invoice.get("statut_code") is not None and invoice.get("statut_code") != 2:
            continue
        if _safe_float(invoice.get("montant_ttc"), 0) < 0:
            continue

        full_invoice = dict(invoice)
        lignes = full_invoice.get("lignes")
        if lignes is None and line_fetcher is not None:
            lignes = line_fetcher(full_invoice)
        full_invoice["lignes"] = lignes or []

        try:
            payload = preview_sfec_payload(full_invoice)
            errors = validate_sfec_payload(payload) + check_sage_totals(
                full_invoice, payload)
        except Exception as exc:  # noqa: BLE001
            errors = ["Payload SFEC impossible: {}".format(exc)]
            payload = {}

        if not errors:
            continue

        sage_ttc = _safe_float(full_invoice.get("montant_ttc"), 0)
        sage_ht = _safe_float(full_invoice.get("montant_ht"), 0)
        computed = _safe_float(payload.get("total_amount"), 0)
        taxes = payload.get("additional_taxes") or []
        rows.append({
            "numero": invoice.get("numero", ""),
            "date": invoice.get("date_facture", ""),
            "tiers": invoice.get("nom_tiers") or invoice.get("tiers_nom") or "",
            "montant_ht": sage_ht,
            "montant_ttc": sage_ttc,
            "ttc_calcule": computed,
            "ecart": round(computed - sage_ttc, 2),
            "ratio_ttc_ht": (round(sage_ttc / sage_ht, 4) if sage_ht else ""),
            "taxes_additionnelles": " | ".join(
                "{} {}% = {}".format(
                    t.get("tax_code"), t.get("tax_rate"), t.get("tax_amount"))
                for t in taxes
            ),
            "motif": "; ".join(str(e) for e in errors),
        })
    rows.sort(key=lambda r: (r.get("date", ""), r.get("numero", "")),
              reverse=True)
    return rows


def write_csv(rows, output_path):
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _safe_cell(row.get(key, "")) for key in FIELDS})


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Exporte les factures SFEC en anomalie vers un CSV.")
    parser.add_argument("--out", default="anomalies_sfec.csv",
                        help="chemin du CSV (defaut: anomalies_sfec.csv)")
    parser.add_argument("--limit", type=int, default=1000,
                        help="nombre de factures Sage relues (defaut: 1000)")
    args = parser.parse_args(argv)

    load_config()
    setup_logging(log_level="info")
    invoices = fetch_sales_invoices(limit=max(1, int(args.limit)))
    rows = build_rows(invoices, line_fetcher=_lines_from_sage)
    write_csv(rows, args.out)
    logger.info("Export anomalies SFEC: %d ligne(s) -> %s", len(rows), args.out)
    print("{} anomalie(s) exportee(s) vers {}".format(len(rows), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
