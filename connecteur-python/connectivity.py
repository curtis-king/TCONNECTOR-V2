"""Passerelle (bridge) vers app.sync.connectivity.

Conservée à la racine le temps de la migration. Sera supprimée une fois
tous les modules migrés.
"""
from app.sync.connectivity import (  # noqa: F401
    check_now,
    get_status,
    is_online,
    wait_for_online,
    start_background_check,
    stop_background_check,
)
