"""Passerelle (bridge) vers app.sync.article_sync.

Conservée à la racine le temps de la migration. Sera supprimée une fois
tous les modules migrés.
"""
from app.sync.article_sync import (  # noqa: F401
    sync_articles_from_sage,
)
