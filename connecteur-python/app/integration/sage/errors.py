"""Taxonomie d'erreurs de l'adaptateur Sage.

Les classes sont introduites progressivement : le code existant continue de
lever `pyodbc.Error` ou `ValueError`, et les marqueurs texte restent reconnus
par `_is_transient()` pendant la transition. Les nouveaux chemins doivent
lever une de ces classes.
"""


class SageError(Exception):
    """Erreur de l'adaptateur Sage (base de la taxonomie)."""


class SageSchemaError(SageError, ValueError):
    """Table ou colonne Sage inutilisable (nom invalide, schéma inattendu)."""
