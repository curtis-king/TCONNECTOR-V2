"""Schéma et découverte des tables Sage 100.

Les fonctions sont définies dans `app.integration.sage.database` et
ré-exportées ici (elles partagent l'état module _TABLE_MAP/_TABLE_COLUMNS).
"""
from app.integration.sage.database import (  # noqa: F401
    discover_tables,
    get_table_map,
    get_domain_config,
    ensure_sfec_columns,
)
