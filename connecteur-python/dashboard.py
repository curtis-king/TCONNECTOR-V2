"""Passerelle (bridge) vers app.web.dashboard.

Conservée à la racine le temps de la migration. Ré-exporte l'application
Flask `app` construite par dashboard.py.
"""
from app.web.dashboard import (  # noqa: F401
    app,
)
