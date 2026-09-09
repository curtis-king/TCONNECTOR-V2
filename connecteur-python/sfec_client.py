"""Passerelle (bridge) vers app.integration.sfec.client.

Conservée à la racine le temps de la migration. Sera supprimée une fois
tous les modules migrés.
"""
from app.integration.sfec.client import (  # noqa: F401
    NetworkOfflineError,
    SfecClient,
)
