"""Chemins applicatif : détection mode frozen (PyInstaller) et répertoires.

Centralise la logique `sys.frozen` dupliquée dans plusieurs modules
(main.py, sqlite_db.py, sfec_endpoints.py, config_manager.py, ...).
"""
import os
import sys

# Racine du package `app` = 3 niveaux au-dessus de ce fichier.
_PACKAGE_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


def is_frozen():
    return bool(getattr(sys, "frozen", False))


def base_dir():
    """Répertoire de base de l'exécutable ou du projet (selon le mode)."""
    if is_frozen():
        return os.path.dirname(sys.executable)
    return _PACKAGE_ROOT


def bundle_dir():
    """Répertoire de bundle PyInstaller (_MEIPASS), None hors mode frozen."""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return None


def data_dir():
    """Répertoire de données runtime (log, base SQLite, cache SFEC)."""
    return os.path.join(base_dir(), "data")
