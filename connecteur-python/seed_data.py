"""Passerelle (bridge) vers app.domain.seed.

Conservée à la racine le temps de la migration. Sera supprimée une fois
tous les modules migrés.
"""
from app.domain.seed import (  # noqa: F401
    seed_clients,
    seed_products,
    seed_vendeurs,
)
