"""Supprime les articles de demonstration (synced_sage = 0) absents de Sage.

A lancer UNE FOIS apres une synchronisation articles reussie.
Refuse de supprimer un article reference par une ligne de facture.

Usage (depuis connecteur-python, avec le venv) :
    python scripts/purge_seed_products.py           # dry-run
    python scripts/purge_seed_products.py --apply   # suppression
"""
import argparse
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.storage.db import get_cursor

_WHERE = """
    synced_sage = 0
    AND id NOT IN (
        SELECT DISTINCT product_id FROM invoice_lines
        WHERE product_id IS NOT NULL
    )
    AND id NOT IN (
        SELECT DISTINCT product_id FROM pos_ticket_lines
        WHERE product_id IS NOT NULL
    )
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true",
                        help="supprime reellement (sinon dry-run)")
    args = parser.parse_args()

    with get_cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM products WHERE " + _WHERE)
        count = cur.fetchone()["c"]
        print("Articles de demonstration supprimables:", count)
        if not args.apply:
            print("Dry-run : aucune suppression. Relancer avec --apply.")
            return
        cur.execute("DELETE FROM products WHERE " + _WHERE)
        print("Articles de demonstration supprimes:", cur.rowcount)


if __name__ == "__main__":
    main()
