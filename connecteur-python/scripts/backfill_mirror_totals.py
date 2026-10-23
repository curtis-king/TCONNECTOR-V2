"""Recalcule la ventilation fiscale du miroir SQLite local.

Le miroir historique ne contient pas les lignes Sage : on classe les
factures par ratio TTC/HT, sans jamais deviner les ratios inconnus.

  - 1,000  : aucune TVA
  - 1,0525 : TVA 5 % + centimes
  - 1,189  : TVA 18 % + centimes
  - 1,219  : TVA 18 % + centimes + taxe additionnelle 3 %

Les lignes hors de ces ratios, ainsi que celles dont le TTC contient une
taxe additionnelle non stockee (3 %), sont ecrites dans un CSV de suspects.
Elles ne sont pas corrigees si le ratio est inconnu, et le 3 % n'est jamais
absorbe dans `additional_cent_tax`.

Par defaut le script est un DRY-RUN. Aucun UPDATE n'est execute sans
`--apply`. Il ne touche jamais a Sage : uniquement `data/tconnector.db`.

Usage (depuis connecteur-python, avec le venv) :
    python scripts/backfill_mirror_totals.py
    python scripts/backfill_mirror_totals.py --apply
    python scripts/backfill_mirror_totals.py --out /tmp/suspects.csv --limit 5000
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
from app.storage.db import compute_cent_tax, get_cursor, init_database

logger = logging.getLogger("t-connector.backfill_mirror")

RATIOS = {
    1.0: 0.0,
    1.0525: 0.05,
    1.189: 0.18,
    1.219: 0.18,
}


def _safe_float(value, default=0.0):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _match_ratio(ratio):
    if ratio is None:
        return None
    for ref, rate in RATIOS.items():
        if abs(ratio - ref) <= 0.001:
            return rate
    return None


def compute_backfill_values(row):
    """Retourne (valeurs, motif_suspect) pour une ligne du miroir.

    `valeurs` vaut None si le ratio est inconnu : dans ce cas aucune
    ecriture ne doit avoir lieu. Un motif non vide signale une ligne a
    controler, meme si la TVA/les centimes sont connus.
    """
    ht = round(_safe_float(row.get("montant_ht")), 2)
    ttc = round(_safe_float(row.get("montant_ttc")), 2)
    if not ht:
        return None, "HT nul"

    ratio = round(ttc / ht, 6)
    rate = _match_ratio(ratio)
    if rate is None:
        return None, "ratio inconnu: {}".format(ratio)

    tva = round(ht * rate, 2)
    centimes = compute_cent_tax(tva)
    extra = round(ttc - ht - tva - centimes, 2)
    motif = ""
    if abs(extra) > 0.01:
        motif = "taxe additionnelle {} FCFA non stockee".format(extra)

    type_doc = row.get("type_doc") or ""
    try:
        sage_type = int(float(row.get("sage_type") or 6))
    except (TypeError, ValueError):
        sage_type = 6
    if sage_type == 7 or ttc < 0:
        type_doc = "avoir"
    elif not type_doc:
        type_doc = "vente"

    values = {
        "montant_tva": tva,
        "additional_cent_tax": centimes,
        "total_tax_t_amount": tva if rate >= 0.18 else 0.0,
        "total_tax_r_amount": tva if 0 < rate < 0.18 else 0.0,
        "total_exempt_amount": ht if rate == 0 else 0.0,
        "type_doc": type_doc,
    }
    return values, motif


def _fetch_rows(limit=None):
    sql = """
        SELECT id, numero, montant_ht, montant_ttc, sage_type, type_doc
        FROM invoices
        WHERE source = 'sage'
          AND (montant_tva IS NULL OR montant_tva = 0)
          AND ABS(montant_ttc - montant_ht) > 0.01
        ORDER BY date_facture, numero
    """
    params = []
    if limit:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    with get_cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def _write_suspects(path, suspects):
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["numero", "ht", "ttc", "ratio", "motif"])
        writer.writeheader()
        writer.writerows(suspects)


def run(apply=False, out_path="backfill_anomalies.csv", limit=None):
    rows = _fetch_rows(limit=limit)
    suspects = []
    corrections = 0
    updated = 0

    for row in rows:
        values, motif = compute_backfill_values(row)
        if values is None:
            suspects.append({
                "numero": row.get("numero", ""),
                "ht": row.get("montant_ht", 0),
                "ttc": row.get("montant_ttc", 0),
                "ratio": (round(_safe_float(row.get("montant_ttc")) /
                                _safe_float(row.get("montant_ht")), 6)
                          if _safe_float(row.get("montant_ht")) else ""),
                "motif": motif,
            })
            continue

        if motif:
            suspects.append({
                "numero": row.get("numero", ""),
                "ht": row.get("montant_ht", 0),
                "ttc": row.get("montant_ttc", 0),
                "ratio": round(_safe_float(row.get("montant_ttc")) /
                               _safe_float(row.get("montant_ht")), 6),
                "motif": motif,
            })

        corrections += 1
        if apply:
            with get_cursor() as cur:
                cur.execute("""
                    UPDATE invoices SET
                        montant_tva = ?, additional_cent_tax = ?,
                        total_tax_t_amount = ?, total_tax_r_amount = ?,
                        total_exempt_amount = ?, type_doc = ?,
                        updated_at = datetime('now')
                    WHERE id = ?
                """, (
                    values["montant_tva"], values["additional_cent_tax"],
                    values["total_tax_t_amount"], values["total_tax_r_amount"],
                    values["total_exempt_amount"], values["type_doc"],
                    row["id"],
                ))
            updated += 1

    _write_suspects(out_path, suspects)
    return {
        "seen": len(rows),
        "corrected": corrections,
        "updated": updated,
        "suspects": len(suspects),
        "apply": bool(apply),
        "out": out_path,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Recalcule la TVA/les centimes du miroir SQLite.")
    parser.add_argument("--apply", action="store_true",
                        help="ecrit les corrections (sinon dry-run)")
    parser.add_argument("--out", default="backfill_anomalies.csv",
                        help="CSV des suspects (defaut: backfill_anomalies.csv)")
    parser.add_argument("--limit", type=int, default=None,
                        help="limite le nombre de lignes examinees")
    args = parser.parse_args(argv)

    load_config()
    setup_logging(log_level="info")
    init_database()
    result = run(apply=args.apply, out_path=args.out, limit=args.limit)

    print(
        "{mode}: {seen} ligne(s), {corrected} correction(s) calculee(s), "
        "{updated} ecrite(s), {suspects} suspect(s) -> {out}".format(
            mode="APPLY" if result["apply"] else "DRY-RUN",
            **result
        )
    )
    if not result["apply"]:
        print("Aucun UPDATE execute. Relancer avec --apply apres verification.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
