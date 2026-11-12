#!/usr/bin/env python3
"""Audit securite local : comptes de test, secrets, permissions.

Dry-run par defaut. `--apply` :

    - passe config.json et data/sites.json en chmod 600 ;
    - desactive admin@admin.com / admin@test.com s'ils existent ;
    - conserve toujours au moins un administrateur actif.

Aucun compte n'est supprime et aucun secret n'est affiche.
"""
import argparse
import json
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from app.core.security_audit import apply_security_audit, audit_security  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Applique les corrections sûres")
    parser.add_argument("--config", default=None, help="Chemin config.json")
    parser.add_argument("--db", default=None, help="Chemin SQLite")
    args = parser.parse_args()

    result = (apply_security_audit(args.config, args.db)["report"]
              if args.apply else audit_security(args.config, args.db))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if not args.apply:
        print("\nDRY-RUN : aucune modification. Relancer avec --apply.")


if __name__ == "__main__":
    main()
