"""Fabrique d'application Flask.

Point d'entrée « applicatif » vers l'instance Flask historique
construite par app.web.dashboard. Les routes restent définies dans
app.web.dashboard pour l'instant ; la séparation en blueprints est
prévue comme chantier ultérieur (voir plan-restructuration.md).
"""
from app.web.dashboard import app


def create_app():
    """Renvoie l'instance Flask de l'application T-CONNECTOR."""
    return app