#!/usr/bin/env python3
"""Maintenance SQLite : purge de sync_log, checkpoint WAL et VACUUM.

Dry-run par defaut. Exemples :

    python scripts/maintain_db.py
    python scripts/maintain_db.py --apply --archive data/sync_log-archive.csv.gz
    python scripts/maintain_db.py --apply --vacuum --days 90

A executer hors heures ouvrees. Aucune ecriture vers Sage.
"""
import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.storage.maintenance import run_maintenance  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=90,
                        help="Retention de sync_log (defaut: 90 jours)")
    parser.add_argument("--apply", action="store_true",
                        help="Archive puis supprime reellement les lignes")
    parser.add_argument("--vacuum", action="store_true",
                        help="Compacte la base apres la purge (avec --apply)")
    parser.add_argument("--archive", default=None,
                        help="Chemin du CSV gzip d'archive")
    parser.add_argument("--db", default=None,
                        help="Chemin SQLite (defaut: data/tconnector.db)")
    args = parser.parse_args()

    result = run_maintenance(
        days=args.days,
        apply=args.apply,
        vacuum_db=args.vacuum,
        archive_path=args.archive,
        db_path=args.db,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not args.apply:
        print("\nDRY-RUN : aucune ligne supprimee. Relancer avec --apply.")


if __name__ == "__main__":
    main()
