"""Couche de connexion SQL Server (Sage 100).

Point d'accès unique à pyodbc / pool. Les fonctions sont définies dans
`app.integration.sage.database` et ré-exportées ici pour la clarté de la
séparation des responsabilités.
"""
from app.integration.sage.database import (  # noqa: F401
    get_pool,
    close_pool,
    get_cursor,
    ping_database,
)
