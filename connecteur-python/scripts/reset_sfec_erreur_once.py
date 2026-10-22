"""Remet a NULL les SFEC_STATUT='ERREUR' sans certification (one-shot).

A lancer UNE FOIS apres le deploiement du correctif 01 (centimes) : les
factures repassent une fois dans l'auto-certification, puis sont classees
CERTIFIE / A_SURVEILLER. Ne touche jamais aux CERTIFIE.

Usage (depuis connecteur-python, avec le venv) :
    python scripts/reset_sfec_erreur_once.py
"""
import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import logging

from app.config.manager import load_config
from app.core.logging_setup import setup_logging
from app.integration.sage.database import reset_stuck_en_cours

if __name__ == "__main__":
    load_config()
    setup_logging(log_level="info")
    logging.getLogger("t-connector.reset").info(
        "Reset ponctuel des ERREUR SFEC (sans certification)")
    reset_stuck_en_cours(reset_errors=True)
